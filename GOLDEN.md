# GOLDEN.md — capturing & re-blessing the golden baseline

`goldens/` holds the **normalized happy-path responses** that the corpus locks
in. It is the behavioural baseline ("absolute truth") and **is committed**. The
PR workflow only ever runs `verify`; capturing is a manual, reviewed act.

## Convention (important)

- Corpus paths are **spec-relative** (`/articles`, `/users/login`, …). The app
  serves under `/api` (`MapGroup("/api")`) while the OpenAPI doc is
  server-relative (`servers:[{url:"/api"}]`). So the **`/api` prefix lives in
  `--base-url`**, and one path string is reused everywhere (corpus = spec =
  impacted = schemathesis include).
- Base URL for capture/verify: `http://<host>:5000/api`.
- Auth scheme is `Token` (RealWorld), not `Bearer`.

## Capture (once, then commit)

The app must be up against a **fresh + seeded** (ephemeral) DB. No local Python
is required — run the pure-stdlib script in a throwaway `python:3.12` container.

```sh
# 1. bring the app up (Docker; builds inside the SDK image, no local .NET needed)
docker compose -f compose.yaml -f compose.app.yaml up -d --build
# 2. fresh + seed the ephemeral DB
docker compose -f compose.yaml -f compose.app.yaml run --rm \
  --entrypoint "dotnet /tools/Conduit.Tools.dll db seed" app
# 3. capture goldens (container shares the compose network -> reaches app:8080)
docker run --rm --network conduit-regression-lab_default \
  -v "$(pwd):/work" -w /work python:3.12-slim \
  python tools/api-regression/golden_runner.py capture \
    --corpus tools/api-regression/corpus.realworld.json \
    --base-url http://app:8080/api \
    --normalize tools/api-regression/normalize.config.json \
    --out goldens
# 4. commit
git add goldens && git commit -m "test: golden baseline"
```

> Running the API on the host instead? Use `--base-url http://localhost:5000/api`.

## Re-bless (intentional behaviour change)

When a PR **intentionally** changes a response, the golden must be re-captured
**in the same PR** so the diff is reviewed like any other code change:

```sh
docker run --rm --network conduit-regression-lab_default \
  -v "$(pwd):/work" -w /work python:3.12-slim \
  python tools/api-regression/golden_runner.py capture \
    --corpus tools/api-regression/corpus.realworld.json \
    --base-url http://app:8080/api \
    --normalize tools/api-regression/normalize.config.json \
    --out goldens
git add goldens && git commit -m "test: re-bless goldens (<why>)"
```

The PR diff on `goldens/*.json` is the human-readable record of what changed.

## What is masked (and why it stays deterministic)

`normalize.config.json` recursively masks volatile keys before a golden is
stored or compared: `token` (JWT iat/exp), `slug`/`id` (DB-derived),
`createdAt`/`updatedAt` (timestamps), `traceId` (per-request ProblemDetails).
Everything else — titles, usernames, counts, booleans, the server-sorted
`tagList` — is deterministic and kept as real signal. A re-run on a fresh DB
therefore produces byte-identical bodies.

## verify (what CI does)

```sh
python tools/api-regression/golden_runner.py verify \
  --corpus tools/api-regression/corpus.realworld.json \
  --base-url http://localhost:5000/api \
  --normalize tools/api-regression/normalize.config.json \
  --goldens goldens \
  --gate-paths specs/impacted-paths.txt \
  --report golden-report.json
```

A diff is **fatal only** when its endpoint is in `--gate-paths` (the impacted
set). Diffs on non-impacted endpoints are warnings. Exit code is non-zero iff
there is at least one fatal diff.
