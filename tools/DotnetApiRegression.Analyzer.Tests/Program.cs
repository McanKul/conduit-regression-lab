using DotnetApiRegression.Analyzer;

using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

const string source =
    """
    using System;
    using Microsoft.AspNetCore.Builder;
    using Microsoft.AspNetCore.Mvc;
    using Microsoft.AspNetCore.Routing;

    namespace Microsoft.AspNetCore.Routing
    {
        public interface IEndpointRouteBuilder { }
    }

    namespace Microsoft.AspNetCore.Builder
    {
        public sealed class EndpointBuilder { }

        public static class EndpointExtensions
        {
            public static IEndpointRouteBuilder MapGroup(
                this IEndpointRouteBuilder app, string route) => app;

            public static EndpointBuilder MapGet(
                this IEndpointRouteBuilder app, string route, Func<int> handler) =>
                new();

            public static EndpointBuilder WithName(
                this EndpointBuilder builder, string name) => builder;
        }
    }

    namespace Microsoft.AspNetCore.Mvc
    {
        public sealed class RouteAttribute(string template) : Attribute;
        public sealed class HttpGetAttribute(string template) : Attribute;
    }

    public sealed class MinimalEndpoints
    {
        public void Map(IEndpointRouteBuilder app)
        {
            const string route = "/orders/{id:int}";
            var v1 = app.MapGroup("/v1");
            v1.MapGet(route, Handle);
        }

        public void MapDynamic(IEndpointRouteBuilder app, string route)
        {
            app.MapGet(route, Handle).WithName("DynamicOrders");
        }

        private static int Handle() => 42;
    }

    [Route("api/[controller]")]
    public sealed class OrdersController
    {
        [HttpGet("{id:int}")]
        public void Find() { }
    }
    """;

var syntaxTree = CSharpSyntaxTree.ParseText(source);
var references = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES"))
    ?.Split(Path.PathSeparator)
    .Select(path => MetadataReference.CreateFromFile(path))
    .ToArray() ?? throw new InvalidOperationException(
        "Trusted platform assemblies are unavailable.");
var compilation = CSharpCompilation.Create(
    "RouteDiscoveryFixture",
    [syntaxTree],
    references,
    new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
var errors = compilation.GetDiagnostics()
    .Where(diagnostic => diagnostic.Severity == DiagnosticSeverity.Error)
    .ToArray();
if (errors.Length > 0)
{
    throw new InvalidOperationException(
        "Fixture compilation failed:\n" + string.Join("\n", errors.AsEnumerable()));
}

var unresolved = new List<string>();
var endpoints = RouteDiscovery.Discover(
    await syntaxTree.GetRootAsync(),
    compilation.GetSemanticModel(syntaxTree),
    _ => ["Handlers.cs"],
    unresolved.Add);

Assert(unresolved.Count == 0, string.Join("\n", unresolved));
Assert(
    endpoints.Any(endpoint =>
        endpoint.Kind == "minimal-api" &&
        endpoint.Route == "/v1/orders/{id:int}" &&
        endpoint.Methods.SequenceEqual(["GET"]) &&
        endpoint.Dependencies.SequenceEqual(["Handlers.cs"])),
    "Minimal API constant/MapGroup route was not resolved.");
Assert(
    endpoints.Any(endpoint =>
        endpoint.Kind == "controller" &&
        endpoint.Route == "/api/Orders/{id:int}" &&
        endpoint.Methods.SequenceEqual(["GET"])),
    "Controller attribute route was not resolved.");
Assert(
    endpoints.Any(endpoint =>
        endpoint.Kind == "minimal-api" &&
        endpoint.Route.Length == 0 &&
        endpoint.OperationId == "DynamicOrders"),
    "Dynamic route was not recovered through WithName/operationId.");

await Console.Out.WriteLineAsync("Roslyn route discovery tests passed.");
return;

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}