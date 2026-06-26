#!/usr/bin/env python3
"""Capture an OpenAPI JSON document from runtime, source, or dotnet build."""

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


EXCLUDED_DIRS = {".git", ".idea", ".vs", ".vscode", "bin", "obj"}
DEFAULT_NAMES = ("openapi.json", "swagger.json")


def valid_openapi_json(payload):
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return False
    return (
        isinstance(document, dict)
        and isinstance(document.get("paths"), dict)
        and ("openapi" in document or "swagger" in document)
    )


def write_document(payload, output):
    if not valid_openapi_json(payload):
        raise ValueError("candidate is not an OpenAPI JSON document")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)


def fetch_runtime(urls, wait_seconds, request_timeout):
    if not urls:
        return None, None
    remaining = list(urls)
    deadline = time.monotonic() + wait_seconds
    while True:
        unavailable = set()
        for url in remaining:
            try:
                with urllib.request.urlopen(
                        url, timeout=request_timeout) as response:
                    payload = response.read()
                if valid_openapi_json(payload):
                    return payload, url
            except urllib.error.HTTPError as error:
                if 400 <= error.code < 500 and error.code not in {408, 429}:
                    unavailable.add(url)
            except (OSError, urllib.error.URLError):
                pass
        remaining = [url for url in remaining if url not in unavailable]
        if not remaining:
            return None, None
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def static_candidates(files, search_roots, output):
    output = Path(output).resolve()
    yielded = set()

    for candidate in files:
        path = Path(candidate).resolve()
        if path.is_file() and path != output and path not in yielded:
            yielded.add(path)
            yield path

    for root_value in search_roots:
        root = Path(root_value).resolve()
        if not root.exists():
            continue
        output_parent = output.parent
        for dirpath, dirnames, names in os.walk(root):
            current = Path(dirpath).resolve()
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in EXCLUDED_DIRS
                and (current / name).resolve() != output_parent
            )
            for name in sorted(names):
                lower = name.lower()
                if (lower not in DEFAULT_NAMES
                        and not lower.startswith(("openapi.", "swagger."))):
                    continue
                path = current / name
                if path.resolve() != output and path not in yielded:
                    yielded.add(path)
                    yield path


def find_static_document(files, search_roots, output):
    for candidate in static_candidates(files, search_roots, output):
        try:
            payload = candidate.read_bytes()
        except OSError:
            continue
        if valid_openapi_json(payload):
            return payload, str(candidate)
    return None, None


def generate_from_projects(projects, environment_name):
    failures = []
    for project_value in projects:
        project = Path(project_value).resolve()
        if not project.is_file():
            failures.append(f"{project}: project not found")
            continue

        with tempfile.TemporaryDirectory(prefix="dotnet-openapi-") as tmp:
            # A fresh cache path forces GenerateOpenApiDocuments even when the
            # application assembly itself is already up to date.
            command = [
                "dotnet",
                "build",
                str(project),
                "--nologo",
                "-p:OpenApiGenerateDocuments=true",
                f"-p:OpenApiDocumentsDirectory={tmp}",
                f"-p:_OpenApiDocumentsCache={Path(tmp) / 'openapi.cache'}",
            ]
            environment = dict(os.environ)
            environment["ASPNETCORE_ENVIRONMENT"] = environment_name
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                failures.append(
                    f"{project}: dotnet build failed\n{detail[-2000:]}")
                continue

            for candidate in sorted(Path(tmp).glob("*.json")):
                payload = candidate.read_bytes()
                if valid_openapi_json(payload):
                    return payload, f"build:{project}", failures
            failures.append(
                f"{project}: build succeeded but emitted no OpenAPI JSON")
    return None, None, failures


def capture(args):
    payload, source = fetch_runtime(
        args.url, args.wait_seconds, args.request_timeout)
    if payload is None:
        payload, source = find_static_document(
            args.file, args.search_root, args.output)

    failures = []
    if payload is None and not args.no_build:
        payload, source, failures = generate_from_projects(
            args.project, args.environment)

    if payload is None:
        attempted = []
        if args.url:
            attempted.append("runtime URLs: " + ", ".join(args.url))
        if args.file or args.search_root:
            attempted.append("static OpenAPI JSON")
        if args.project and not args.no_build:
            attempted.append("ASP.NET build-time generation")
        detail = "\n".join(failures)
        raise RuntimeError(
            "OpenAPI document not found; attempted " +
            (", ".join(attempted) or "no sources") +
            (f"\n{detail}" if detail else ""))

    write_document(payload, args.output)
    print(f"OpenAPI captured from {source} -> {args.output}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Capture OpenAPI JSON from a running app, a checked-in file, "
            "or ASP.NET build-time generation."))
    parser.add_argument("--output", required=True)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--search-root", action="append", default=[])
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--request-timeout", type=float, default=5)
    parser.add_argument("--environment", default="OpenApi")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    try:
        return capture(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
