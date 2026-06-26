# DotnetApiRegression.Analyzer

Roslyn/MSBuild analyzer for impact-based ASP.NET Core regression tests. It
loads a solution or project, discovers Minimal API and controller endpoints,
and writes a deterministic JSON reverse-dependency index.

```sh
dotnet-api-regression-analyze \
  --root . \
  --solution MyApp.sln \
  --output specs/semantic-index.json
```

The companion `dotnet-api-regression` Python package combines this index with
git and OpenAPI diffs. See the repository's `KULLANIM.md` for the full CI flow.
