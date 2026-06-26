#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path

import golden_runner
import impact_resolver
import openapi_capture


VERSION = "0.2.0"


def usage():
    return """dotnet-api-regression

Usage:
  dotnet-api-regression analyze [Roslyn analyzer options]
  dotnet-api-regression impact [impact-resolver options]
  dotnet-api-regression openapi capture [capture options]
  dotnet-api-regression golden capture [golden options]
  dotnet-api-regression golden verify [golden options]
  dotnet-api-regression --version

Run a command with --help to see its full option list.
"""


def run_analyzer(arguments):
    configured = os.environ.get("DOTNET_API_REGRESSION_ANALYZER")
    if configured:
        return subprocess.run(
            [configured, *arguments],
            check=False,
        ).returncode

    candidates = [
        Path.cwd() / "tools" / "DotnetApiRegression.Analyzer"
        / "DotnetApiRegression.Analyzer.csproj",
        Path(__file__).resolve().parent.parent
        / "DotnetApiRegression.Analyzer"
        / "DotnetApiRegression.Analyzer.csproj",
    ]
    project = next((path for path in candidates if path.exists()), None)
    if project is not None:
        return subprocess.run(
            ["dotnet", "run", "--project", str(project), "--", *arguments],
            check=False,
        ).returncode

    installed = shutil.which("dotnet-api-regression-analyze")
    if installed:
        return subprocess.run(
            [installed, *arguments],
            check=False,
        ).returncode

    print(
        "Roslyn analyzer not found. Install the "
        "DotnetApiRegression.Analyzer dotnet tool or set "
        "DOTNET_API_REGRESSION_ANALYZER.",
        file=sys.stderr,
    )
    return 2


def main():
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print(usage())
        return 0
    if sys.argv[1] in {"-V", "--version"}:
        print(VERSION)
        return 0

    command = sys.argv[1]
    if command == "analyze":
        return run_analyzer(sys.argv[2:])
    if command == "impact":
        sys.argv = [f"{sys.argv[0]} impact", *sys.argv[2:]]
        return impact_resolver.main() or 0
    if command == "openapi":
        if len(sys.argv) < 3 or sys.argv[2] != "capture":
            print("openapi requires capture\n", file=sys.stderr)
            print(usage(), file=sys.stderr)
            return 2
        sys.argv = [f"{sys.argv[0]} openapi capture", *sys.argv[3:]]
        return openapi_capture.main()
    if command == "golden":
        if len(sys.argv) < 3:
            print("golden requires capture or verify\n", file=sys.stderr)
            print(usage(), file=sys.stderr)
            return 2
        sys.argv = [f"{sys.argv[0]} golden", *sys.argv[2:]]
        return golden_runner.main()

    print(f"unknown command: {command}\n", file=sys.stderr)
    print(usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
