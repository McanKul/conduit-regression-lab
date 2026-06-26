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
  the report's `global` flag is set true. `global_impact_strategy=flag` keeps it
  advisory; `all` marks every OpenAPI path as impacted.

Outputs (into --out-dir, default ./specs)
  impacted-paths.txt   one OpenAPI path per line (feeds Schemathesis --include-path)
  impact-report.json   { global, global_triggers, rebaseline_entities, stats,
                         endpoints:[{path, reasons[]}], changed_files }

The preferred source-impact engine consumes a Roslyn/MSBuild semantic index
produced by DotnetApiRegression.Analyzer. A name/reference heuristic remains
available only as an explicit compatibility fallback.
"""
import argparse
import collections
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
    "exclude_dir_names": [".git", ".idea", ".vs", ".vscode",
                          "bin", "obj", "node_modules"],
    "controller_suffix": "Controller",
    "minimal_api_map_regex": r'Map(?:Get|Post|Put|Delete|Patch|Methods)\s*(?:<[^>]*>)?\(\s*"([^"]+)"',
    "path_match_case_insensitive": True,
    "global_impact_strategy": "flag",
    "fallback_all_when_unresolved": False,
    "source_analysis_mode": "prefer-semantic",
    "semantic_max_hops": 32,
    "semantic_unresolved_strategy": "flag",
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

DOTNET_MAP_METHODS = ("Get", "Post", "Put", "Delete", "Patch", "Methods")
CSTRING = r'(?:[@$]{0,2})"([^"]*)"'
ATTRIBUTE_BLOCK = r'\[(?:[^\]"]|"[^"]*")+\]'


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


def norm_path(p, case_insensitive=False):
    """Leading slash; collapse path params to {} so {id} == {orderId}."""
    p = "/" + p.strip("/")
    p = re.sub(r"\{[^}]+\}", "{}", p)
    return p.lower() if case_insensitive else p


def join_routes(*parts):
    """Join ASP.NET route templates without caring where slashes live."""
    chunks = []
    absolute = None
    for part in parts:
        if part is None:
            continue
        part = part.strip()
        if not part or part == "~":
            continue
        if part.startswith("~/"):
            absolute = part[2:]
            chunks = []
        else:
            chunks.append(part.strip("/"))
    if absolute is not None:
        chunks.insert(0, absolute.strip("/"))
    return "/" + "/".join(c for c in chunks if c)


def strip_csharp_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def expand_controller_tokens(template, controller, action=""):
    return (template
            .replace("[controller]", controller)
            .replace("[Controller]", controller)
            .replace("[action]", action)
            .replace("[Action]", action))


def path_suffixes(path):
    segments = [segment for segment in path.strip("/").split("/") if segment]
    for i in range(len(segments)):
        suffix = "/" + "/".join(segments[i:])
        if suffix == "/{}":
            continue
        yield suffix


def build_suffix_index(norm_to_paths):
    suffix_to_paths = collections.defaultdict(set)
    for path, spec_paths_for_norm in norm_to_paths.items():
        for suffix in path_suffixes(path):
            suffix_to_paths[suffix].update(spec_paths_for_norm)
    return suffix_to_paths


def route_hits(routes, norm_to_paths, cfg, suffix_to_paths=None):
    hit = set()
    suffix_to_paths = suffix_to_paths or build_suffix_index(norm_to_paths)
    case_insensitive = cfg.get("path_match_case_insensitive", False)
    for lit in routes:
        ln = norm_path(lit, case_insensitive)
        if ln in norm_to_paths:
            hit |= norm_to_paths[ln]
            continue
        hit |= suffix_to_paths.get(ln, set())
        for suffix in path_suffixes(ln):
            hit |= norm_to_paths.get(suffix, set())
    return hit


def minimal_api_routes(text, cfg):
    """Best-effort route templates from ASP.NET Core Minimal API files.

    Supports the common large-codebase shape:
      var v1 = app.MapGroup("/v1");
      var users = v1.MapGroup("/users");
      users.MapGet("/{id}", ...);

    It intentionally remains heuristic and string-literal based; route constants
    still fall back to tag/source-reference impact instead of pretending we can
    evaluate arbitrary C# without a compiler.
    """
    text = strip_csharp_comments(text)
    routes = set()
    group_prefixes = {"app": "", "api": "", "routes": "", "endpoints": ""}

    group_re = re.compile(
        rf'(?:\b(?:var|RouteGroupBuilder|IEndpointRouteBuilder|WebApplication)\s+)?'
        rf'(?P<var>[A-Za-z_]\w*)\s*=\s*'
        rf'(?P<target>[A-Za-z_]\w*)\s*\.MapGroup\s*\(\s*{CSTRING}',
        re.S,
    )
    for match in group_re.finditer(text):
        prefix = group_prefixes.get(match.group("target"), "")
        group_prefixes[match.group("var")] = join_routes(prefix, match.groups()[-1])

    methods = "|".join(DOTNET_MAP_METHODS)
    call_re = re.compile(
        rf'(?P<target>[A-Za-z_]\w*)\s*\.Map(?:{methods})'
        rf'\s*(?:<[^>]*>)?\s*\(\s*{CSTRING}',
        re.S,
    )
    for match in call_re.finditer(text):
        routes.add(join_routes(group_prefixes.get(match.group("target"), ""), match.groups()[-1]))

    chained_re = re.compile(
        rf'\.MapGroup\s*\(\s*{CSTRING}\s*\)\s*'
        rf'\.Map(?:{methods})\s*(?:<[^>]*>)?\s*\(\s*{CSTRING}',
        re.S,
    )
    for match in chained_re.finditer(text):
        routes.add(join_routes(match.group(1), match.group(2)))

    # Keep the legacy configurable regex as a compatibility backstop for
    # unusual wrappers, but do not add unprefixed duplicates for MapGroup calls.
    if not routes:
        map_re = re.compile(cfg["minimal_api_map_regex"])
        routes.update(map_re.findall(text))
    return routes


def _route_attrs(attrs, names):
    names_re = "|".join(re.escape(n) for n in names)
    attr_re = re.compile(rf'\[\s*(?:{names_re})\s*(?:\(\s*{CSTRING})?', re.S)
    return [m.group(1) or "" for m in attr_re.finditer(attrs)]


def controller_routes(text):
    """Best-effort route templates from MVC/Web API controllers."""
    text = strip_csharp_comments(text)
    routes = set()
    class_re = re.compile(
        rf'^[ \t]*(?P<attrs>(?:(?:{ATTRIBUTE_BLOCK})[ \t]*(?:\r?\n[ \t]*)?)*)'
        r'(?:public|internal|private|protected)?\s*'
        r'(?:sealed\s+|abstract\s+|partial\s+)*'
        r'class\s+(?P<name>[A-Za-z_]\w*Controller)\b',
        re.S | re.M,
    )
    classes = list(class_re.finditer(text))
    if not classes:
        return routes

    action_attr_re = re.compile(
        rf'^[ \t]*(?P<attrs>(?:(?:{ATTRIBUTE_BLOCK})[ \t]*(?:\r?\n[ \t]*)?)+)'
        r'(?:public|internal|private|protected)?\s*'
        r'(?:async\s+)?(?:static\s+)?[\w<>,\[\]\s?]+?\s+'
        r'(?P<action>[A-Za-z_]\w*)\s*\(',
        re.S | re.M,
    )
    action_names = ["HttpGet", "HttpPost", "HttpPut", "HttpDelete",
                    "HttpPatch", "HttpHead", "HttpOptions", "Route"]

    for i, cls in enumerate(classes):
        class_end = classes[i + 1].start() if i + 1 < len(classes) else len(text)
        body = text[cls.end():class_end]
        controller = cls.group("name")[:-len("Controller")]
        prefixes = _route_attrs(cls.group("attrs"), ["Route"]) or [""]

        for action_match in action_attr_re.finditer(body):
            action = action_match.group("action")
            action_templates = _route_attrs(action_match.group("attrs"), action_names)
            if not action_templates:
                continue
            for prefix in prefixes:
                for template in action_templates:
                    routes.add(join_routes(
                        expand_controller_tokens(prefix, controller, action),
                        expand_controller_tokens(template, controller, action),
                    ))
    return routes


def walk_sources(root, cfg):
    files = []
    excluded_names = set(cfg.get("exclude_dir_names", []))
    for dirpath, dirnames, names in os.walk(root):
        kept = []
        for dirname in dirnames:
            if dirname in excluded_names:
                continue
            rel_dir = os.path.relpath(
                os.path.join(dirpath, dirname), root).replace(os.sep, "/")
            if glob_any(rel_dir + "/_", cfg["exclude_globs"]):
                continue
            kept.append(dirname)
        dirnames[:] = kept

        for n in names:
            if not n.endswith(".cs"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
            if glob_any(rel, cfg["exclude_globs"]):
                continue
            files.append(rel)
    return files


def spec_paths(spec_file, cfg=None):
    cfg = cfg or {}
    case_insensitive = cfg.get("path_match_case_insensitive", False)
    data = json.loads(Path(spec_file).read_text())
    paths = data.get("paths", {}) or {}
    info = {}
    for p, item in paths.items():
        tag = None
        operation_ids = set()
        if isinstance(item, dict):
            for method, op in item.items():
                if isinstance(op, dict) and op.get("tags"):
                    tag = op["tags"][0]
                if (method.lower() in {
                        "get", "post", "put", "delete", "patch",
                        "head", "options", "trace"}
                        and isinstance(op, dict) and op.get("operationId")):
                    operation_ids.add(op["operationId"])
        info[p] = {
            "tag": tag,
            "norm": norm_path(p, case_insensitive),
            "operation_ids": operation_ids,
        }
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


def referenced_types(text, cfg):
    names = set(re.findall(r'\b[A-Z]\w+\b', text))
    return {n for n in names
            if len(n) >= cfg["min_type_name_len"] and n not in cfg["type_name_stoplist"]}


def build_reference_index(texts, cfg):
    refs = collections.defaultdict(set)
    declarations, implementations = {}, {}
    for rel, text in texts.items():
        declarations[rel] = declared_types(text, Path(rel).stem, cfg)
        implementations[rel] = implemented_types(text, cfg)
        for nm in referenced_types(text, cfg):
            refs[nm].add(rel)
    return refs, declarations, implementations


def build_index(root, sources, info, cfg):
    """file -> {spec paths it defines}, plus cached file texts."""
    norm_to_paths = {}
    tag_to_paths = collections.defaultdict(set)
    segment_to_paths = collections.defaultdict(set)
    for p, meta in info.items():
        norm_to_paths.setdefault(meta["norm"], set()).add(p)
        if meta["tag"]:
            tag_to_paths[meta["tag"].lower()].add(p)
        for segment in p.strip("/").split("/"):
            if segment and not segment.startswith("{"):
                segment_to_paths[segment.lower()].add(p)
    suffix_to_paths = build_suffix_index(norm_to_paths)
    any_tags = bool(tag_to_paths)
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
                hit |= tag_to_paths.get(cname.lower(), set())
            else:
                hit |= segment_to_paths.get(cname.lower(), set())

        # Minimal API and MVC/Web API attributes: match route literals to spec
        # paths (parameter-name agnostic, optionally case-insensitive).
        hit |= route_hits(minimal_api_routes(text, cfg), norm_to_paths, cfg,
                          suffix_to_paths)
        hit |= route_hits(controller_routes(text), norm_to_paths, cfg,
                          suffix_to_paths)

        if hit:
            file_to_paths[rel] = hit
    return file_to_paths, texts


def load_semantic_index(index_files, info, cfg):
    """Load and merge Roslyn indexes from the base and revision worktrees."""
    norm_to_paths = collections.defaultdict(set)
    operation_to_paths = collections.defaultdict(set)
    for path, meta in info.items():
        norm_to_paths[meta["norm"]].add(path)
        for operation_id in meta.get("operation_ids", set()):
            operation_to_paths[operation_id].add(path)
    suffix_to_paths = build_suffix_index(norm_to_paths)

    file_to_paths = collections.defaultdict(set)
    endpoint_dependencies = collections.defaultdict(set)
    reverse_dependencies = collections.defaultdict(set)
    diagnostics = []
    analyzer_stats = {
        "indexes": 0,
        "source_files": 0,
        "dependency_edges": 0,
        "declared_endpoints": 0,
        "unresolved_routes": 0,
    }

    for index_file in index_files:
        data = json.loads(Path(index_file).read_text())
        if data.get("schema_version") != 2 or data.get("engine") != "roslyn":
            raise ValueError(
                f"{index_file}: unsupported semantic index format")
        analyzer_stats["indexes"] += 1
        stats = data.get("stats", {})
        analyzer_stats["source_files"] = max(
            analyzer_stats["source_files"], stats.get("source_files", 0))
        analyzer_stats["dependency_edges"] += stats.get("dependency_edges", 0)
        analyzer_stats["declared_endpoints"] += stats.get("endpoints", 0)
        analyzer_stats["unresolved_routes"] += stats.get("unresolved_routes", 0)
        diagnostics.extend(data.get("diagnostics", []))

        for rel, endpoints in data.get("endpoint_files", {}).items():
            for endpoint in endpoints:
                matched_paths = set()
                route = endpoint.get("route")
                if route:
                    matched_paths.update(route_hits(
                        {route}, norm_to_paths, cfg, suffix_to_paths))
                operation_id = endpoint.get("operation_id")
                if operation_id:
                    matched_paths.update(
                        operation_to_paths.get(operation_id, set()))
                file_to_paths[rel].update(matched_paths)
                for dependency in endpoint.get("dependencies", []):
                    for path in matched_paths:
                        endpoint_dependencies[dependency].add((rel, path))

        for dependency, dependents in data.get(
                "reverse_dependencies", {}).items():
            reverse_dependencies[dependency].update(dependents)

    return (
        dict(file_to_paths),
        dict(endpoint_dependencies),
        dict(reverse_dependencies),
        sorted(set(diagnostics)),
        analyzer_stats,
    )


def semantic_source_impact(
        changed, file_to_paths, reverse_dependencies, cfg,
        endpoint_dependencies=None):
    """Walk Roslyn's file-level reverse symbol graph to endpoint files."""
    impacted = {}
    endpoint_dependencies = endpoint_dependencies or {}
    has_endpoint_dependencies = bool(endpoint_dependencies)
    max_hops = cfg.get("semantic_max_hops", 32)
    excluded = cfg.get("exclude_globs", [])

    for origin in changed:
        queue = collections.deque([(origin, 0)])
        seen = {origin}
        while queue:
            current, depth = queue.popleft()
            direct_file_change = current == origin
            if direct_file_change or not has_endpoint_dependencies:
                for path in file_to_paths.get(current, ()):
                    reason = (
                        f"endpoint code changed: {origin}"
                        if direct_file_change
                        else f"semantic dependency on {origin} via {current}")
                    impacted.setdefault(path, set()).add(reason)

            for endpoint_file, path in endpoint_dependencies.get(current, ()):
                reason = (
                    f"semantic dependency on {origin} via {endpoint_file}")
                impacted.setdefault(path, set()).add(reason)

            if depth >= max_hops:
                continue
            for dependent in reverse_dependencies.get(current, ()):
                if dependent in seen or glob_any(dependent, excluded):
                    continue
                seen.add(dependent)
                queue.append((dependent, depth + 1))
    return impacted


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

def source_impact(changed, file_to_paths, texts, cfg,
                  refs_by_type=None, declarations=None, implementations=None):
    impacted = {}
    refs_by_type = refs_by_type or {}
    declarations = declarations or {}
    implementations = implementations or {}

    # 1) changed endpoint-defining files -> their endpoints (direct)
    for f in changed:
        for p in file_to_paths.get(f, ()):
            impacted.setdefault(p, set()).add(f"endpoint code changed: {f}")

    # 2) changed shared files -> reverse-reference walk to endpoint-defining
    #    files. Seed with types DECLARED in the file AND interfaces/base types
    #    it IMPLEMENTS (so impl -> interface -> injecting endpoint connects).
    frontier = []
    for f in changed:
        seeds = set(declarations.get(f, set()))
        if not seeds and f in texts:
            seeds = declared_types(texts.get(f, ""), Path(f).stem, cfg)
        seeds |= set(implementations.get(f, set()))
        if not implementations.get(f) and f in texts:
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
            refs = refs_by_type.get(nm)
            if refs is None:
                word = re.compile(r'\b' + re.escape(nm) + r'\b')
                refs = {rel for rel, text in texts.items() if word.search(text)}
            for rel in refs:
                if rel == origin:
                    continue
                if rel in file_to_paths:
                    for p in file_to_paths[rel]:
                        impacted.setdefault(p, set()).add(f"depends on {nm} (via {origin})")
                else:
                    declared = declarations.get(rel)
                    if declared is None:
                        declared = declared_types(texts.get(rel, ""), Path(rel).stem, cfg)
                    for nm2 in declared:
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
    ap.add_argument(
        "--semantic-index",
        action="append",
        default=[],
        help="Roslyn index for the revision worktree (repeatable)")
    ap.add_argument(
        "--base-semantic-index",
        action="append",
        default=[],
        help=(
            "Roslyn index for the base worktree; repeatable and covers "
            "deleted/moved files"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out-dir", default="specs")
    a = ap.parse_args()

    cfg = load_config(a.config)
    changed = changed_files(a.base, a.head)
    src_changed = [f for f in changed
                   if f.endswith(".cs") and not glob_any(f, cfg["exclude_globs"])]
    globals_hit = [
        f for f in changed
        if not glob_any(f, cfg["exclude_globs"])
        and match_any(f, cfg["global_triggers"])
    ]

    sources = []
    info = spec_paths(a.spec, cfg)
    analysis_mode = cfg.get("source_analysis_mode", "prefer-semantic")
    if analysis_mode not in {"semantic", "prefer-semantic", "heuristic"}:
        raise ValueError(
            "source_analysis_mode must be 'semantic', 'prefer-semantic', "
            "or 'heuristic'")

    semantic_files = [*a.base_semantic_index, *a.semantic_index]
    use_semantic = bool(semantic_files) and analysis_mode != "heuristic"
    if analysis_mode == "semantic" and not semantic_files:
        raise ValueError(
            "source_analysis_mode=semantic requires --semantic-index")

    semantic_diagnostics = []
    analyzer_stats = {}
    if use_semantic:
        (
            file_to_paths,
            endpoint_dependencies,
            reverse_dependencies,
            semantic_diagnostics,
            analyzer_stats,
        ) = load_semantic_index(semantic_files, info, cfg)
        texts = {}
        refs_by_type, declarations, implementations = {}, {}, {}
    else:
        endpoint_dependencies = {}
        sources = walk_sources(a.root, cfg)
        file_to_paths, texts = build_index(a.root, sources, info, cfg)
        refs_by_type, declarations, implementations = build_reference_index(
            texts, cfg)

    impacted = {}

    def merge(d):
        for p, rs in d.items():
            impacted.setdefault(p, set()).update(rs)

    spec_imp = spec_diff(a.base_spec, a.spec)
    source_imp = {}
    source_imp_by_file = {}
    for source_file in src_changed:
        if use_semantic:
            file_impact = semantic_source_impact(
                [source_file], file_to_paths, reverse_dependencies, cfg,
                endpoint_dependencies)
        else:
            file_impact = source_impact(
                [source_file], file_to_paths, texts, cfg,
                refs_by_type, declarations, implementations)
        source_imp_by_file[source_file] = file_impact
        for path, reasons in file_impact.items():
            source_imp.setdefault(path, set()).update(reasons)

    # Migration parsing needs only changed migration and endpoint files. Avoid
    # reading an entire large solution when Roslyn already supplied the graph.
    if use_semantic:
        needed_texts = set(file_to_paths)
        needed_texts.update(
            path for path in changed
            if re.search(cfg["migration_dir_regex"], path))
        for rel in needed_texts:
            try:
                texts[rel] = (Path(a.root) / rel).read_text(errors="ignore")
            except Exception:
                texts[rel] = ""
    mig_imp, entities = migration_impact(changed, file_to_paths, texts, cfg)
    merge(spec_imp)
    merge(source_imp)
    merge(mig_imp)

    global_strategy = cfg.get("global_impact_strategy", "flag")
    if global_strategy not in {"flag", "all"}:
        raise ValueError("global_impact_strategy must be 'flag' or 'all'")
    if globals_hit and global_strategy == "all":
        for p in info:
            impacted.setdefault(p, set()).add(
                "global trigger changed: " + ", ".join(globals_hit))

    unresolved_routes = analyzer_stats.get("unresolved_routes", 0)
    unresolved_strategy = cfg.get("semantic_unresolved_strategy", "flag")
    if unresolved_strategy not in {"flag", "all"}:
        raise ValueError(
            "semantic_unresolved_strategy must be 'flag' or 'all'")
    if use_semantic and unresolved_routes and unresolved_strategy == "all":
        for p in info:
            impacted.setdefault(p, set()).add(
                f"Roslyn could not resolve {unresolved_routes} endpoint route(s)")

    unresolved_source_files = [
        source_file for source_file in src_changed
        if not source_imp_by_file[source_file]
        and source_file not in globals_hit
        and not (
            mig_imp and re.search(
                cfg["migration_dir_regex"], source_file)
        )
    ]
    unresolved_changed = bool(unresolved_source_files)
    if (unresolved_changed
            and cfg.get("fallback_all_when_unresolved", False)):
        unresolved_summary = ", ".join(unresolved_source_files[:10])
        if len(unresolved_source_files) > 10:
            unresolved_summary += (
                f" (+{len(unresolved_source_files) - 10} more)")
        for p in info:
            impacted.setdefault(p, set()).add(
                "source changed but resolver could not map: " +
                unresolved_summary)

    os.makedirs(a.out_dir, exist_ok=True)
    paths = sorted(impacted)
    Path(a.out_dir, "impacted-paths.txt").write_text(
        "\n".join(paths) + ("\n" if paths else ""))
    report = {
        "global": bool(globals_hit),
        "global_impact_strategy": global_strategy,
        "global_triggers": globals_hit,
        "source_analysis": "roslyn" if use_semantic else "heuristic",
        "semantic_diagnostics": semantic_diagnostics,
        "semantic_unresolved_strategy": unresolved_strategy,
        "unresolved_changed_sources": unresolved_changed,
        "unresolved_source_files": unresolved_source_files,
        "rebaseline_entities": sorted(entities),
        "endpoints": [{"path": p, "reasons": sorted(impacted[p])} for p in paths],
        "changed_files": src_changed,
        "stats": {
            "spec_paths": len(info),
            "source_files": (
                analyzer_stats.get("source_files", 0)
                if use_semantic else len(sources)),
            "endpoint_files": len(file_to_paths),
            "reference_terms": len(refs_by_type),
            "semantic": analyzer_stats,
        },
    }
    Path(a.out_dir, "impact-report.json").write_text(json.dumps(report, indent=2))

    print(f"{len(paths)} endpoint(s) impacted | global={bool(globals_hit)} "
          f"| rebaseline entities={sorted(entities)}")
    for p in paths:
        print(f"  {p}  <-  {'; '.join(sorted(impacted[p]))}")


if __name__ == "__main__":
    main()
