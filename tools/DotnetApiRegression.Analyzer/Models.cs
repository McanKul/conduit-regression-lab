using System.Text.Json.Serialization;

namespace DotnetApiRegression.Analyzer;

internal sealed record EndpointDefinition(
    [property: JsonPropertyName("route")] string Route,
    [property: JsonPropertyName("methods")] IReadOnlyList<string> Methods,
    [property: JsonPropertyName("operation_id")] string? OperationId,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("line")] int Line,
    [property: JsonPropertyName("dependencies")]
    IReadOnlyList<string> Dependencies);

internal sealed record AnalyzerStats(
    [property: JsonPropertyName("projects")] int Projects,
    [property: JsonPropertyName("source_files")] int SourceFiles,
    [property: JsonPropertyName("dependency_edges")] int DependencyEdges,
    [property: JsonPropertyName("endpoints")] int Endpoints,
    [property: JsonPropertyName("unresolved_routes")] int UnresolvedRoutes);

internal sealed record SemanticIndex(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("engine")] string Engine,
    [property: JsonPropertyName("projects")] IReadOnlyList<string> Projects,
    [property: JsonPropertyName("endpoint_files")]
    IReadOnlyDictionary<string, IReadOnlyList<EndpointDefinition>> EndpointFiles,
    [property: JsonPropertyName("reverse_dependencies")]
    IReadOnlyDictionary<string, IReadOnlyList<string>> ReverseDependencies,
    [property: JsonPropertyName("diagnostics")] IReadOnlyList<string> Diagnostics,
    [property: JsonPropertyName("stats")] AnalyzerStats Stats);