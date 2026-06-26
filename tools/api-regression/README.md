# dotnet-api-regression

Impact-based regression orchestration for ASP.NET Core APIs. The CLI combines
OpenAPI contract diffs, a Roslyn semantic dependency index, targeted contract
fuzzing inputs, and normalized golden-response verification.

```sh
dotnet-api-regression --help
dotnet-api-regression analyze --root . --solution MyApp.sln \
  --output specs/semantic-index.json
```

The Roslyn engine is packaged separately as the
`DotnetApiRegression.Analyzer` .NET tool. In a source checkout, the CLI finds
and runs the local analyzer project automatically.
