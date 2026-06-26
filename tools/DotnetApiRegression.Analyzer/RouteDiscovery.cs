using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace DotnetApiRegression.Analyzer;

internal static class RouteDiscovery
{
    private static readonly IReadOnlyDictionary<string, string[]> MapMethods =
        new Dictionary<string, string[]>(StringComparer.Ordinal)
        {
            ["MapGet"] = ["GET"],
            ["MapPost"] = ["POST"],
            ["MapPut"] = ["PUT"],
            ["MapDelete"] = ["DELETE"],
            ["MapPatch"] = ["PATCH"],
            ["MapMethods"] = [],
        };

    private static readonly IReadOnlyDictionary<string, string[]> HttpAttributes =
        new Dictionary<string, string[]>(StringComparer.Ordinal)
        {
            ["HttpGetAttribute"] = ["GET"],
            ["HttpPostAttribute"] = ["POST"],
            ["HttpPutAttribute"] = ["PUT"],
            ["HttpDeleteAttribute"] = ["DELETE"],
            ["HttpPatchAttribute"] = ["PATCH"],
            ["HttpHeadAttribute"] = ["HEAD"],
            ["HttpOptionsAttribute"] = ["OPTIONS"],
        };

    public static IReadOnlyList<EndpointDefinition> Discover(
        SyntaxNode root,
        SemanticModel model,
        Func<SyntaxNode, IReadOnlyList<string>> dependencies,
        Action<string> unresolved)
    {
        var endpoints = new List<EndpointDefinition>();
        DiscoverMinimalApis(root, model, endpoints, dependencies, unresolved);
        DiscoverControllers(root, model, endpoints, dependencies);
        return endpoints
            .DistinctBy(endpoint => (
                endpoint.Route,
                string.Join(",", endpoint.Methods),
                endpoint.OperationId,
                endpoint.Kind,
                endpoint.Line))
            .OrderBy(endpoint => endpoint.Route, StringComparer.Ordinal)
            .ThenBy(endpoint => endpoint.Line)
            .ToArray();
    }

    private static void DiscoverMinimalApis(
        SyntaxNode root,
        SemanticModel model,
        List<EndpointDefinition> endpoints,
        Func<SyntaxNode, IReadOnlyList<string>> dependencies,
        Action<string> unresolved)
    {
        foreach (var invocation in root.DescendantNodes().OfType<InvocationExpressionSyntax>())
        {
            var syntaxName = invocation.Expression switch
            {
                MemberAccessExpressionSyntax access => access.Name.Identifier.ValueText,
                IdentifierNameSyntax identifier => identifier.Identifier.ValueText,
                GenericNameSyntax generic => generic.Identifier.ValueText,
                _ => string.Empty,
            };
            if (!MapMethods.ContainsKey(syntaxName))
            {
                continue;
            }

            var method = GetMethod(model, invocation);
            if (method is null)
            {
                unresolved(
                    $"Roslyn could not bind {syntaxName} at line {LineOf(invocation)}");
                continue;
            }

            if (!MapMethods.TryGetValue(method.Name, out var httpMethods) ||
                !IsAspNetEndpointMethod(method))
            {
                unresolved(
                    $"{syntaxName} at line {LineOf(invocation)} resolved to " +
                    $"{method.ContainingType.ToDisplayString()}.{method.Name}, " +
                    "not an ASP.NET endpoint method");
                continue;
            }

            var routeExpression = invocation.ArgumentList.Arguments.FirstOrDefault()?.Expression;
            var route = ConstantString(model, routeExpression);
            if (route is null)
            {
                var line = LineOf(invocation);
                var operationId = FindOperationId(invocation, model);
                if (operationId is not null)
                {
                    endpoints.Add(new(
                        string.Empty,
                        httpMethods,
                        operationId,
                        "minimal-api",
                        line,
                        EndpointDependencies(
                            invocation,
                            method.Name,
                            dependencies)));
                }
                else
                {
                    unresolved(
                        $"Non-constant route without WithName for " +
                        $"{method.Name} at line {line}");
                }
                continue;
            }

            var prefix = ResolveGroupPrefix(
                ReceiverOf(invocation),
                model,
                new HashSet<ISymbol>(SymbolEqualityComparer.Default),
                new HashSet<SyntaxNode>());
            route = JoinRoutes(prefix, route);

            var methods = httpMethods;
            if (method.Name == "MapMethods" && invocation.ArgumentList.Arguments.Count > 1)
            {
                methods = ConstantStringArray(
                    model,
                    invocation.ArgumentList.Arguments[1].Expression);
            }

            endpoints.Add(new(
                route,
                methods,
                FindOperationId(invocation, model),
                "minimal-api",
                LineOf(invocation),
                EndpointDependencies(
                    invocation,
                    method.Name,
                    dependencies)));
        }
    }

    private static void DiscoverControllers(
        SyntaxNode root,
        SemanticModel model,
        List<EndpointDefinition> endpoints,
        Func<SyntaxNode, IReadOnlyList<string>> dependencies)
    {
        foreach (var declaration in root.DescendantNodes().OfType<ClassDeclarationSyntax>())
        {
            if (model.GetDeclaredSymbol(declaration) is not INamedTypeSymbol type)
            {
                continue;
            }

            var classRoutes = AttributeTemplates(
                type.GetAttributes(),
                attribute => IsMvcAttribute(attribute, "RouteAttribute"));
            if (classRoutes.Count == 0)
            {
                classRoutes = [string.Empty];
            }

            var controller = type.Name.EndsWith(
                "Controller",
                StringComparison.Ordinal)
                ? type.Name[..^"Controller".Length]
                : type.Name;

            foreach (var method in type.GetMembers().OfType<IMethodSymbol>())
            {
                if (method.MethodKind != MethodKind.Ordinary ||
                    !method.Locations.Any(location =>
                        location.SourceTree == declaration.SyntaxTree))
                {
                    continue;
                }

                var attributes = method.GetAttributes();
                var httpAttributes = attributes
                    .Where(attribute =>
                        IsMvcAttribute(attribute, AttributeName(attribute)) &&
                        HttpAttributes.ContainsKey(AttributeName(attribute)))
                    .ToArray();
                var routeTemplates = AttributeTemplates(
                    attributes,
                    attribute => IsMvcAttribute(attribute, "RouteAttribute"));
                if (httpAttributes.Length == 0 && routeTemplates.Count == 0)
                {
                    continue;
                }

                if (httpAttributes.Length == 0)
                {
                    AddControllerEndpoints(
                        endpoints,
                        classRoutes,
                        routeTemplates,
                        [],
                        controller,
                        method,
                        declaration.SyntaxTree,
                        MethodDependencies(method, declaration.SyntaxTree, dependencies));
                    continue;
                }

                foreach (var attribute in httpAttributes)
                {
                    var attributeTemplate = AttributeTemplate(attribute);
                    IReadOnlyList<string> actionRoutes;
                    if (attributeTemplate is not null)
                    {
                        actionRoutes = [attributeTemplate];
                    }
                    else
                    {
                        actionRoutes = routeTemplates.Count > 0
                            ? routeTemplates
                            : [string.Empty];
                    }
                    AddControllerEndpoints(
                        endpoints,
                        classRoutes,
                        actionRoutes,
                        HttpAttributes[AttributeName(attribute)],
                        controller,
                        method,
                        declaration.SyntaxTree,
                        MethodDependencies(method, declaration.SyntaxTree, dependencies));
                }
            }
        }
    }

    private static void AddControllerEndpoints(
        List<EndpointDefinition> endpoints,
        IReadOnlyList<string> classRoutes,
        IReadOnlyList<string> actionRoutes,
        IReadOnlyList<string> methods,
        string controller,
        IMethodSymbol method,
        SyntaxTree sourceTree,
        IReadOnlyList<string> dependencies)
    {
        foreach (var classRoute in classRoutes)
        {
            foreach (var actionRoute in actionRoutes)
            {
                var route = JoinRoutes(
                    ExpandTokens(classRoute, controller, method.Name),
                    ExpandTokens(actionRoute, controller, method.Name));
                endpoints.Add(new(
                    route,
                    methods,
                    method.Name,
                    "controller",
                    LineOf(method.Locations.First(location =>
                        location.SourceTree == sourceTree)),
                    dependencies));
            }
        }
    }

    private static IReadOnlyList<string> EndpointDependencies(
        InvocationExpressionSyntax invocation,
        string methodName,
        Func<SyntaxNode, IReadOnlyList<string>> dependencies)
    {
        var statement = invocation.AncestorsAndSelf()
            .OfType<StatementSyntax>()
            .FirstOrDefault();
        if (statement is not null)
        {
            return dependencies(statement);
        }

        var handlerIndex = methodName == "MapMethods" ? 2 : 1;
        if (invocation.ArgumentList.Arguments.Count <= handlerIndex)
        {
            return [];
        }

        return dependencies(
            invocation.ArgumentList.Arguments[handlerIndex].Expression);
    }

    private static IReadOnlyList<string> MethodDependencies(
        IMethodSymbol method,
        SyntaxTree sourceTree,
        Func<SyntaxNode, IReadOnlyList<string>> dependencies)
    {
        var syntax = method.DeclaringSyntaxReferences
            .FirstOrDefault(reference => reference.SyntaxTree == sourceTree)
            ?.GetSyntax();
        return syntax is null ? [] : dependencies(syntax);
    }

    private static string ResolveGroupPrefix(
        ExpressionSyntax? receiver,
        SemanticModel model,
        HashSet<ISymbol> seenSymbols,
        HashSet<SyntaxNode> seenNodes)
    {
        if (receiver is null || !seenNodes.Add(receiver))
        {
            return string.Empty;
        }

        if (receiver is ParenthesizedExpressionSyntax parenthesized)
        {
            return ResolveGroupPrefix(
                parenthesized.Expression,
                model,
                seenSymbols,
                seenNodes);
        }

        if (receiver is InvocationExpressionSyntax invocation)
        {
            var method = GetMethod(model, invocation);
            if (method?.Name == "MapGroup" && IsAspNetEndpointMethod(method))
            {
                var segment = ConstantString(
                    model,
                    invocation.ArgumentList.Arguments.FirstOrDefault()?.Expression);
                return JoinRoutes(
                    ResolveGroupPrefix(
                        ReceiverOf(invocation),
                        model,
                        seenSymbols,
                        seenNodes),
                    segment ?? string.Empty);
            }

            return string.Empty;
        }

        var symbol = model.GetSymbolInfo(receiver).Symbol;
        if (symbol is null || !seenSymbols.Add(symbol))
        {
            return string.Empty;
        }

        foreach (var syntaxReference in symbol.DeclaringSyntaxReferences)
        {
            var syntax = syntaxReference.GetSyntax();
            var initializer = syntax switch
            {
                VariableDeclaratorSyntax variable => variable.Initializer?.Value,
                PropertyDeclarationSyntax property => property.Initializer?.Value ??
                                                      property.ExpressionBody?.Expression,
                _ => null,
            };
            if (initializer is not null)
            {
                return ResolveGroupPrefix(
                    initializer,
                    model,
                    seenSymbols,
                    seenNodes);
            }
        }

        return string.Empty;
    }

    private static IMethodSymbol? GetMethod(
        SemanticModel model,
        InvocationExpressionSyntax invocation)
    {
        var info = model.GetSymbolInfo(invocation);
        return info.Symbol as IMethodSymbol;
    }

    private static bool IsAspNetEndpointMethod(IMethodSymbol method)
    {
        var definition = method.ReducedFrom ?? method;
        return definition.ContainingNamespace.ToDisplayString()
                   .StartsWith("Microsoft.AspNetCore", StringComparison.Ordinal) &&
               definition.Parameters.Any(parameter =>
                   parameter.Type.Name is "IEndpointRouteBuilder" or "RouteGroupBuilder");
    }

    private static ExpressionSyntax? ReceiverOf(InvocationExpressionSyntax invocation)
    {
        return invocation.Expression is MemberAccessExpressionSyntax memberAccess
            ? memberAccess.Expression
            : null;
    }

    private static string? ConstantString(
        SemanticModel model,
        ExpressionSyntax? expression)
    {
        if (expression is null)
        {
            return null;
        }

        var value = model.GetConstantValue(expression);
        return value.HasValue ? value.Value as string : null;
    }

    private static string[] ConstantStringArray(
        SemanticModel model,
        ExpressionSyntax expression)
    {
        return expression.DescendantNodesAndSelf()
            .OfType<ExpressionSyntax>()
            .Select(item => ConstantString(model, item))
            .Where(value => value is not null)
            .Select(value => value!)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(value => value, StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }

    private static string? FindOperationId(
        InvocationExpressionSyntax endpoint,
        SemanticModel model)
    {
        foreach (var invocation in endpoint.Ancestors()
                     .OfType<InvocationExpressionSyntax>())
        {
            if (invocation.Expression is not MemberAccessExpressionSyntax access ||
                access.Name.Identifier.ValueText != "WithName")
            {
                continue;
            }

            return ConstantString(
                model,
                invocation.ArgumentList.Arguments.FirstOrDefault()?.Expression);
        }

        return null;
    }

    private static List<string> AttributeTemplates(
        IEnumerable<AttributeData> attributes,
        Func<AttributeData, bool> predicate)
    {
        return attributes
            .Where(predicate)
            .Select(attribute => AttributeTemplate(attribute) ?? string.Empty)
            .Distinct(StringComparer.Ordinal)
            .ToList();
    }

    private static string? AttributeTemplate(AttributeData attribute)
    {
        if (attribute.ConstructorArguments.Length > 0 &&
            attribute.ConstructorArguments[0].Value is string constructorValue)
        {
            return constructorValue;
        }

        return attribute.NamedArguments.FirstOrDefault(
            argument => argument.Key == "Template").Value.Value as string;
    }

    private static string AttributeName(AttributeData attribute)
    {
        return attribute.AttributeClass?.Name ?? string.Empty;
    }

    private static bool IsMvcAttribute(
        AttributeData attribute,
        string expectedName)
    {
        return AttributeName(attribute) == expectedName &&
               (attribute.AttributeClass?.ContainingNamespace.ToDisplayString()
                    .StartsWith(
                        "Microsoft.AspNetCore.Mvc",
                        StringComparison.Ordinal) ?? false);
    }

    private static string ExpandTokens(
        string route,
        string controller,
        string action)
    {
        return route
            .Replace("[controller]", controller, StringComparison.OrdinalIgnoreCase)
            .Replace("[action]", action, StringComparison.OrdinalIgnoreCase);
    }

    private static string JoinRoutes(params string[] routes)
    {
        var segments = new List<string>();
        foreach (var route in routes)
        {
            if (string.IsNullOrWhiteSpace(route))
            {
                continue;
            }

            if (route.StartsWith("~/", StringComparison.Ordinal))
            {
                segments.Clear();
                segments.Add(route[2..].Trim('/'));
                continue;
            }

            segments.Add(route.Trim('/'));
        }

        return "/" + string.Join(
            "/",
            segments.Where(segment => segment.Length > 0));
    }

    private static int LineOf(SyntaxNode node)
    {
        return node.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
    }

    private static int LineOf(Location location)
    {
        return location.GetLineSpan().StartLinePosition.Line + 1;
    }
}