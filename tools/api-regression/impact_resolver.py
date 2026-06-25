#!/usr/bin/env python3
"""
impact_resolver.py - decide which endpoints a PR actually impacts.

    impact set = spec-diff  U  source-impact  U  migration-impact

Signals
  spec-diff        endpoints whose OpenAPI definition changed. SCHEMA-AWARE:
                   a path is flagged when its own path-item changed OR any
                   component schema it transitively $refs changed. (.NET's
                   OpenAPI emits `$ref` into components, so a response/request
                   DTO field addition shows up in components.schemas, NOT in the
                   path-item - a paths-only diff would silently miss it.)
  source-impact    endpoints whose backing code (or code it transitively
                   depends on) changed -> catches "spec looks the same but
                   behaviour broke". Bridges interface -> implementation: a
                   changed `class Foo : IFoo` seeds `IFoo`, and the endpoint
                   file that injects `IFoo` is reached even though it never
                   names the concrete `Foo` (clean-architecture + DI).
  migration-impact endpoints touching an entity whose EF migration changed

Global triggers
  If a changed file matches a "global trigger" (Program.cs, *DbContext.cs, DI,
  middleware, base controller, .csproj / package versions, auth handlers, ...)
  the report's `global` flag is set true. That is ADVISORY: the PR job stays
  fast (it only tests the concrete union below); the recommended handling is to
  let a nightly job run the full baseline when `global` is true.

Outputs (into --out-dir, default ./specs)
  impacted-paths.txt   one OpenAPI path per line (feeds Schemathesis --include-path)
  impact-report.json   { global, global_triggers, rebaseline_entities,
                         endpoints:[{path, reasons[]}], changed_files }

Pure standard library, no build required. This is the HEURISTIC version: the
source/migration -> endpoint mapping is name/reference based, so it can both
over- and under-match (generics, partial classes, reflection, DI). The
interface is designed so a Roslyn-based precise reverse call-graph can later
replace `source_impact()` without touching the rest of the pipeline.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path


DEFAULTS = {
    "_comment": "Edit global_triggers / exclude_globs to fit your repo.",
    "exclude_globs": [
        "**/obj/**", "**/bin/**",
        "**/*.Designer.cs", "**/*Tests/**", "**/*.Tests/**",
    ],
    "controller_suffix": "Controller",
    "minimal_api_map_regex": r'Map(?:Get|Post|Put|Delete|Patch|Methods)\s*(?:<[^>]*>)?\(\s*"([^"]+)"',
    # `class Foo(...) : IFoo, IBar` -> capture the base list "IFoo, IBar"
    "implements_regex": r'(?:class|record|struct)\s+\w+\s*(?:<[^>]*>)?\s*(?:\([^)]*\))?\s*:\s*([^\{\n]+)',
    "migration_dir_regex": r'(^|/)Migrations/',
    "migration_table_regex": r'(?:name|table)\s*:\s*"([^"]+)"',
    "reference_max_hops": 2,
    "min_type_name_len": 4,
    "type_name_stoplist": ["Program", "Startup", "Options", "Settings",
                           "Constants", "Extensions", "Result", "Response"],
    "global_triggers": [
        r'(^|/)Program\.cs$',
        r'(^|/)Startup\.cs$',
        r'DbContext\.cs$',
        r'(^|/)Directory\.(Build|Packages)\.props$',
        r'\.csproj$',
        r'(^|/)Middlewares?/',
        r'Auth\w*Handler\.cs$',
        r'Base\w*Controller\.cs$',
    ],
}


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text()))
    return cfg


def changed_files(base, head):
    out = sh("git", "diff", "--name-only", f"{base}...{head}")
    return [l.strip() for l in out.splitlines() if l.strip()]


def glob_any(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def match_any(path, patterns):
    return any(re.search(p, path) for p in patterns)


def norm_path(p):
    """Leading slash; collapse path params to {} so {id} == {orderId}."""
    p = "/" + p.strip("/")
    return re.sub(r"\{[^}]+\}", "{}", p)


def walk_sources(root, cfg):
    files = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if not n.endswith(".cs"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
            if glob_any(rel, cfg["exclude_globs"]):
                continue
            files.append(rel)
    return files


def spec_paths(spec_file):
    data = json.loads(Path(spec_file).read_text())
    paths = data.get("paths", {}) or {}
    info = {}
    for p, item in paths.items():
        tag = None
        if isinstance(item, dict):
            for op in item.values():
                if isinstance(op, dict) and op.get("tags"):
                    tag = op["tags"][0]
                    break
        info[p] = {"tag": tag, "norm": norm_path(p)}
    return info


def declared_types(text, stem, cfg):
    names = set(re.findall(r'\b(?:class|interface|record|struct|enum)\s+([A-Z]\w+)', text))
    names.add(stem)
    return {n for n in names
            if len(n) >= cfg["min_type_name_len"] and n not in cfg["type_name_stoplist"]}


def implemented_types(text, cfg):
    """Interfaces / base types a declared type extends (`: IFoo, Bar<T>`).

    These bridge a concrete handler to the endpoint that injects its interface:
    the endpoint never names `CommandArticles`, only `ICommandArticles`.
    """
    out = set()
    for base_list in re.findall(cfg["implements_regex"], text):
        for nm in re.findall(r'[A-Z]\w+', base_list):
            if len(nm) >= cfg["min_type_name_len"] and nm not in cfg["type_name_stoplist"]:
                out.add(nm)
    return out


def build_index(root, sources, info, cfg):
    """file -> {spec paths it defines}, plus cached file texts."""
    norm_to_paths = {}
    for p, meta in info.items():
        norm_to_paths.setdefault(meta["norm"], set()).add(p)
    any_tags = any(meta["tag"] for meta in info.values())
    map_re = re.compile(cfg["minimal_api_map_regex"])
    suffix = cfg["controller_suffix"]

    file_to_paths, texts = {}, {}
    for rel in sources:
        try:
            text = (Path(root) / rel).read_text(errors="ignore")
        except Exception:
            text = ""
        texts[rel] = text
        stem = Path(rel).stem
        hit = set()

        # Controllers: match by default tag (= controller name), else route segment
        if stem.endswith(suffix):
            cname = stem[:-len(suffix)] or stem
            if any_tags:
                for p, meta in info.items():
                    if meta["tag"] and meta["tag"].lower() == cname.lower():
                        hit.add(p)
            else:
                seg = "/" + cname.lower()
                hit.update(p for p in info if seg in p.lower())

        # Minimal API: match route literals to spec paths (param-name agnostic)
        for lit in map_re.findall(text):
            ln = norm_path(lit)
            if ln in norm_to_paths:
                hit |= norm_to_paths[ln]
            else:
                for npath, ps in norm_to_paths.items():
                    if npath.endswith(ln) or ln.endswith(npath):
                        hit |= ps

        if hit:
            file_to_paths[rel] = hit
    return file_to_paths, texts


# ----------------------------- spec-diff (schema-aware) ----------------------

def _collect_refs(node, out):
    """Component schema names directly $ref'd anywhere under `node`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.add(v.rsplit("/", 1)[-1])
            else:
                _collect_refs(v, out)
    elif isinstance(node, list):
        for x in node:
            _collect_refs(x, out)


def _schema_closure(name, schemas, seen):
    if name in seen or name not in schemas:
        return
    seen.add(name)
    refs = set()
    _collect_refs(schemas[name], refs)
    for r in refs:
        _schema_closure(r, schemas, seen)


def _path_signature(path_item, schemas):
    """Canonical string = path-item JSON + JSON of every schema it transitively
    $refs. Two signatures differ iff the path-item OR any referenced schema
    differs - which is exactly when the endpoint's contract changed."""
    refs = set()
    _collect_refs(path_item, refs)
    closure = set()
    for r in refs:
        _schema_closure(r, schemas, closure)
    payload = {
        "path": path_item,
        "schemas": {n: schemas.get(n) for n in sorted(closure)},
    }
    return json.dumps(payload, sort_keys=True)


def _schemas_of(spec):
    return (spec.get("components", {}) or {}).get("schemas", {}) or {}


def spec_diff(base_spec, rev_spec):
    impacted = {}
    try:
        base = json.loads(Path(base_spec).read_text())
    except Exception:
        base = {}
    rev = json.loads(Path(rev_spec).read_text())
    base_paths = base.get("paths", {}) or {}
    rev_paths = rev.get("paths", {}) or {}
    base_schemas = _schemas_of(base)
    rev_schemas = _schemas_of(rev)
    for p, item in rev_paths.items():
        if p not in base_paths:
            impacted.setdefault(p, set()).add("spec: endpoint added")
        elif _path_signature(item, rev_schemas) != _path_signature(base_paths[p], base_schemas):
            impacted.setdefault(p, set()).add("spec: definition or schema changed")
    return impacted


# ----------------------------- source-impact ---------------------------------

def source_impact(changed, file_to_paths, texts, cfg):
    impacted = {}

    # 1) changed endpoint-defining files -> their endpoints (direct)
    for f in changed:
        for p in file_to_paths.get(f, ()):
            impacted.setdefault(p, set()).add(f"endpoint code changed: {f}")

    # 2) changed shared files -> reverse-reference walk to endpoint-defining
    #    files. Seed with types DECLARED in the file AND interfaces/base types
    #    it IMPLEMENTS (so impl -> interface -> injecting endpoint connects).
    frontier = []
    for f in changed:
        seeds = declared_types(texts.get(f, ""), Path(f).stem, cfg)
        seeds |= implemented_types(texts.get(f, ""), cfg)
        for nm in seeds:
            frontier.append((nm, f))

    seen = set()
    for _ in range(cfg["reference_max_hops"]):
        if not frontier:
            break
        nxt = []
        for nm, origin in frontier:
            if nm in seen:
                continue
            seen.add(nm)
            word = re.compile(r'\b' + re.escape(nm) + r'\b')
            for rel, text in texts.items():
                if rel == origin or not word.search(text):
                    continue
                if rel in file_to_paths:
                    for p in file_to_paths[rel]:
                        impacted.setdefault(p, set()).add(f"depends on {nm} (via {origin})")
                else:
                    for nm2 in declared_types(text, Path(rel).stem, cfg):
                        nxt.append((nm2, rel))
        frontier = nxt
    return impacted


def migration_impact(changed, file_to_paths, texts, cfg):
    mig_re = re.compile(cfg["migration_dir_regex"])
    tbl_re = re.compile(cfg["migration_table_regex"])
    impacted, entities, tables = {}, set(), set()

    for f in changed:
        if not mig_re.search(f) or f.endswith("ModelSnapshot.cs"):
            continue
        text = texts.get(f)
        if text is None:
            try:
                text = Path(f).read_text(errors="ignore")
            except Exception:
                text = ""
        tables.update(tbl_re.findall(text))

    for tb in tables:
        cands = {tb}
        if tb.endswith("ies"):
            cands.add(tb[:-3] + "y")
        elif tb.endswith("s"):
            cands.add(tb[:-1])
        for ent in cands:
            entities.add(ent)
            word = re.compile(r'\b' + re.escape(ent) + r'\b')
            for rel, text in texts.items():
                if rel in file_to_paths and word.search(text):
                    for p in file_to_paths[rel]:
                        impacted.setdefault(p, set()).add(f"migration changed table {tb}")
    return impacted, entities


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="base git ref (e.g. origin/main)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--spec", required=True, help="revision (PR) OpenAPI spec")
    ap.add_argument("--base-spec", required=True, help="base branch OpenAPI spec")
    ap.add_argument("--config", default=None)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out-dir", default="specs")
    a = ap.parse_args()

    cfg = load_config(a.config)
    changed = changed_files(a.base, a.head)
    src_changed = [f for f in changed
                   if f.endswith(".cs") and not glob_any(f, cfg["exclude_globs"])]
    globals_hit = [f for f in changed if match_any(f, cfg["global_triggers"])]

    sources = walk_sources(a.root, cfg)
    info = spec_paths(a.spec)
    file_to_paths, texts = build_index(a.root, sources, info, cfg)

    impacted = {}

    def merge(d):
        for p, rs in d.items():
            impacted.setdefault(p, set()).update(rs)

    merge(spec_diff(a.base_spec, a.spec))
    merge(source_impact(src_changed, file_to_paths, texts, cfg))
    mig_imp, entities = migration_impact(changed, file_to_paths, texts, cfg)
    merge(mig_imp)

    os.makedirs(a.out_dir, exist_ok=True)
    paths = sorted(impacted)
    Path(a.out_dir, "impacted-paths.txt").write_text(
        "\n".join(paths) + ("\n" if paths else ""))
    report = {
        "global": bool(globals_hit),
        "global_triggers": globals_hit,
        "rebaseline_entities": sorted(entities),
        "endpoints": [{"path": p, "reasons": sorted(impacted[p])} for p in paths],
        "changed_files": src_changed,
    }
    Path(a.out_dir, "impact-report.json").write_text(json.dumps(report, indent=2))

    print(f"{len(paths)} endpoint(s) impacted | global={bool(globals_hit)} "
          f"| rebaseline entities={sorted(entities)}")
    for p in paths:
        print(f"  {p}  <-  {'; '.join(sorted(impacted[p]))}")


if __name__ == "__main__":
    main()
