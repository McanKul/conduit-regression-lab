#!/usr/bin/env python3
"""
golden_runner.py - lock & verify normalized happy-path responses.

The running application is the ONLY source of truth - there are no
hand-written assertions.

  capture   Replay the corpus once against a known-good app and store each
            NORMALIZED response under goldens/<id>.json. This is the baseline
            ("absolute truth"); run it once, commit goldens/.
  verify    Replay the corpus, normalize, and diff against the committed
            goldens. A diff is FATAL only when the request's endpoint is in the
            impacted set (--gate-paths); diffs on non-impacted endpoints are
            reported as warnings. Same status + same normalized body => pass.

Determinism: the corpus generates its own data (register -> create -> read), so
it does not depend on random seed data. Volatile fields (token, slug, id,
timestamps, traceId) are masked via --normalize, so a re-run on a fresh DB
yields a byte-identical body.

Pure standard library (urllib). No pip install required.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------- value extraction ------------------------------

def extract(obj, expr):
    """Tiny JSONPath: '.user.token', '.article.slug', '.items[0].id'."""
    cur = obj
    for key, idx in re.findall(r'\.([A-Za-z0-9_]+)|\[(\d+)\]', expr):
        if key:
            cur = cur.get(key) if isinstance(cur, dict) else None
        else:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and len(cur) > i else None
        if cur is None:
            break
    return cur


def fill(template, variables):
    """Replace {name} in a string from captured variables."""
    return re.sub(r'\{([A-Za-z0-9_]+)\}',
                  lambda m: str(variables.get(m.group(1), m.group(0))),
                  template)


def mask(node, keys, value):
    """Recursively replace any dict value whose key is in `keys`."""
    if isinstance(node, dict):
        return {k: (value if k in keys else mask(v, keys, value))
                for k, v in node.items()}
    if isinstance(node, list):
        return [mask(x, keys, value) for x in node]
    return node


# ----------------------------- http ------------------------------------------

def http(method, url, body, headers):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, raw


def run_corpus(corpus, base_url, scheme):
    """Replay the flow, threading captured vars. Returns [(req, status, body)]."""
    variables = {}
    results = []
    for rq in corpus["requests"]:
        path = fill(rq["path"], variables)
        url = base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if rq.get("auth"):
            token = variables.get("token")
            if token:
                headers["Authorization"] = f"{scheme} {token}"
        status, raw = http(rq["method"], url, rq.get("body"), headers)
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            body = raw
        for var, expr in (rq.get("capture") or {}).items():
            if isinstance(body, (dict, list)):
                val = extract(body, expr)
                if val is not None:
                    variables[var] = val
        results.append((rq, status, body))
    return results


# ----------------------------- gating ----------------------------------------

def gate_norm(path, strip_prefix):
    if strip_prefix and path.startswith(strip_prefix):
        path = path[len(strip_prefix):]
    path = "/" + path.strip("/")
    return re.sub(r'\{[^}]+\}', '{}', path)


def load_gate(gate_file, strip_prefix):
    if not gate_file or not Path(gate_file).exists():
        return None  # None => gate everything (no impact filter supplied)
    lines = [l.strip() for l in Path(gate_file).read_text().splitlines() if l.strip()]
    return {gate_norm(l, strip_prefix) for l in lines}


# ----------------------------- commands --------------------------------------

def cmd_capture(args, corpus, norm_cfg):
    scheme = args.auth_scheme or corpus.get("auth_scheme", "Token")
    keys = set(norm_cfg.get("mask_keys", []))
    mval = norm_cfg.get("mask_value", "<masked>")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for rq, status, body in run_corpus(corpus, args.base_url, scheme):
        golden = {
            "id": rq["id"],
            "method": rq["method"],
            "path": rq["path"],
            "status": status,
            "body": mask(body, keys, mval),
        }
        (out / f"{rq['id']}.json").write_text(
            json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  captured {rq['id']:<28} {rq['method']:<6} {rq['path']}  -> {status}")
    print(f"captured {len(corpus['requests'])} golden(s) into {out}/")


def cmd_verify(args, corpus, norm_cfg):
    scheme = args.auth_scheme or corpus.get("auth_scheme", "Token")
    keys = set(norm_cfg.get("mask_keys", []))
    mval = norm_cfg.get("mask_value", "<masked>")
    strip = norm_cfg.get("gate_strip_prefix", "")
    gate = load_gate(args.gate_paths, strip)
    goldens = Path(args.goldens)

    diffs, fatal, warnings, missing = [], [], [], []
    for rq, status, body in run_corpus(corpus, args.base_url, scheme):
        gfile = goldens / f"{rq['id']}.json"
        if not gfile.exists():
            missing.append(rq["id"])
            continue
        golden = load_json(gfile)
        actual_body = mask(body, keys, mval)
        status_changed = golden.get("status") != status
        body_changed = json.dumps(golden.get("body"), sort_keys=True) != \
            json.dumps(actual_body, sort_keys=True)
        if not status_changed and not body_changed:
            continue
        impacted = gate is None or gate_norm(rq["path"], strip) in gate
        entry = {
            "id": rq["id"],
            "path": rq["path"],
            "impacted": impacted,
            "status_changed": status_changed,
            "expected_status": golden.get("status"),
            "actual_status": status,
            "body_changed": body_changed,
        }
        diffs.append(entry)
        (fatal if impacted else warnings).append(entry)

    report = {
        "base_url": args.base_url,
        "total": len(corpus["requests"]),
        "missing_goldens": missing,
        "diffs": diffs,
        "warnings": warnings,
        "fatal": fatal,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for w in warnings:
        print(f"  WARN  {w['id']:<28} {w['path']}  (not impacted; status "
              f"{w['expected_status']}->{w['actual_status']}, body_changed={w['body_changed']})")
    for f in fatal:
        print(f"  FAIL  {f['id']:<28} {f['path']}  (impacted; status "
              f"{f['expected_status']}->{f['actual_status']}, body_changed={f['body_changed']})")
    if missing:
        print(f"  note: {len(missing)} corpus request(s) have no golden yet: {missing}")
    print(f"golden verify: {len(diffs)} diff(s), {len(fatal)} fatal, "
          f"{len(warnings)} warning(s)")
    return 1 if fatal else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("capture", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--corpus", required=True)
        p.add_argument("--base-url", required=True,
                       help="e.g. http://localhost:5000/api (carries the /api prefix)")
        p.add_argument("--normalize", required=True)
        p.add_argument("--auth-scheme", default=None,
                       help="overrides corpus.auth_scheme (default 'Token')")
        if name == "capture":
            p.add_argument("--out", default="goldens")
        else:
            p.add_argument("--goldens", default="goldens")
            p.add_argument("--gate-paths", default=None,
                           help="impacted-paths.txt; diffs outside it are warnings")
            p.add_argument("--report", default=None)

    args = ap.parse_args()
    corpus = load_json(args.corpus)
    norm_cfg = load_json(args.normalize)

    if args.cmd == "capture":
        cmd_capture(args, corpus, norm_cfg)
        return 0
    return cmd_verify(args, corpus, norm_cfg)


if __name__ == "__main__":
    sys.exit(main())
