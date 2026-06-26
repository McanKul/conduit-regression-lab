using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace DotnetApiRegression.Analyzer;

internal sealed class SemanticIndexBuilder(string root)
{
    private readonly string _root = Path.GetFullPath(root);
    private readonly Dictionary<string, HashSet<string>> _forwardDependencies =
        new(StringComparer.Ordinal);
    private readonly Dictionary<string, List<EndpointDefinition>> _endpointFiles =
        new(StringComparer.Ordinal);
    private readonly HashSet<string> _processedFiles = new(StringComparer.Ordinal);
    private readonly List<string> _diagnostics = [];
    private int _unresolvedRoutes;

    public async Task<SemanticIndex> BuildAsync(
        Solution solution,
        IReadOnlyList<string> workspaceDiagnostics,
        CancellationToken cancellationToken)
    {
        foreach (var project in solution.Projects
                     .OrderBy(project => project.FilePath, StringComparer.Ordinal))
        {
            if (!project.SupportsCompilation)
            {
                continue;
            }

            var compilation = await project.GetCompilationAsync(cancellationToken);
            if (compilation is null)
            {
                throw new InvalidOperationException(
                    $"Roslyn could not compile {project.FilePath}");
            }

            var errors = compilation.GetDiagnostics(cancellationToken)
                .Where(diagnostic => diagnostic.Severity == DiagnosticSeverity.Error)
                .Take(20)
                .Select(diagnostic => diagnostic.ToString())
                .ToArray();
            if (errors.Length > 0)
            {
                throw new InvalidOperationException(
                    $"Compilation failed for {project.FilePath}:\n" +
                    string.Join("\n", errors));
            }

            foreach (var document in project.Documents
                         .OrderBy(document => document.FilePath, StringComparer.Ordinal))
            {
                cancellationToken.ThrowIfCancellationRequested();
                var relativePath = RelativeSourcePath(document.FilePath);
                if (relativePath is null || !_processedFiles.Add(relativePath))
                {
                    continue;
                }

                var rootNode = await document.GetSyntaxRootAsync(cancellationToken);
                var model = await document.GetSemanticModelAsync(cancellationToken);
                if (rootNode is null || model is null)
                {
                    _diagnostics.Add($"Could not load semantic model: {relativePath}");
                    continue;
                }

                IndexDependencies(relativePath, rootNode, model);
                IndexInheritance(relativePath, rootNode, model);
                IndexEndpoints(relativePath, rootNode, model);
            }
        }

        var reverseDependencies = ReverseDependencies();
        var endpointFiles = _endpointFiles
            .OrderBy(item => item.Key, StringComparer.Ordinal)
            .ToDictionary(
                item => item.Key,
                item => (IReadOnlyList<EndpointDefinition>)item.Value
                    .OrderBy(endpoint => endpoint.Route, StringComparer.Ordinal)
                    .ThenBy(endpoint => endpoint.Line)
                    .ToArray(),
                StringComparer.Ordinal);
        var projects = solution.Projects
            .Select(project => RelativeProjectPath(project.FilePath))
            .Where(path => path is not null)
            .Select(path => path!)
            .Distinct(StringComparer.Ordinal)
            .OrderBy(path => path, StringComparer.Ordinal)
            .ToArray();
        var diagnostics = workspaceDiagnostics
            .Concat(_diagnostics)
            .Distinct(StringComparer.Ordinal)
            .OrderBy(message => message, StringComparer.Ordinal)
            .ToArray();

        return new(
            2,
            "roslyn",
            projects,
            endpointFiles,
            reverseDependencies,
            diagnostics,
            new(
                projects.Length,
                _processedFiles.Count,
                reverseDependencies.Sum(item => item.Value.Count),
                endpointFiles.Sum(item => item.Value.Count),
                _unresolvedRoutes));
    }

    private void IndexDependencies(
        string sourceFile,
        SyntaxNode rootNode,
        SemanticModel model)
    {
        foreach (var name in rootNode.DescendantNodes()
                     .Where(node => node is IdentifierNameSyntax or GenericNameSyntax)
                     .OfType<SimpleNameSyntax>())
        {
            var symbolInfo = model.GetSymbolInfo(name);
            if (symbolInfo.Symbol is not null)
            {
                AddSymbolDependency(sourceFile, symbolInfo.Symbol);
            }
        }
    }

    private void IndexInheritance(
        string sourceFile,
        SyntaxNode rootNode,
        SemanticModel model)
    {
        foreach (var declaration in rootNode.DescendantNodes()
                     .OfType<TypeDeclarationSyntax>())
        {
            if (model.GetDeclaredSymbol(declaration) is not INamedTypeSymbol type)
            {
                continue;
            }

            var relatedTypes = type.AllInterfaces.AsEnumerable();
            if (type.BaseType is not null)
            {
                relatedTypes = relatedTypes.Append(type.BaseType);
            }

            foreach (var relatedType in relatedTypes)
            {
                foreach (var location in relatedType.Locations.Where(
                             location => location.IsInSource))
                {
                    var relatedFile = RelativeSourcePath(
                        location.SourceTree?.FilePath);
                    if (relatedFile is null || relatedFile == sourceFile)
                    {
                        continue;
                    }

                    AddEdge(sourceFile, relatedFile);
                    AddEdge(relatedFile, sourceFile);
                }
            }

            foreach (var location in type.Locations.Where(
                         location => location.IsInSource))
            {
                var partialFile = RelativeSourcePath(location.SourceTree?.FilePath);
                if (partialFile is null || partialFile == sourceFile)
                {
                    continue;
                }

                AddEdge(sourceFile, partialFile);
                AddEdge(partialFile, sourceFile);
            }
        }
    }

    private void IndexEndpoints(
        string sourceFile,
        SyntaxNode rootNode,
        SemanticModel model)
    {
        var endpoints = RouteDiscovery.Discover(
            rootNode,
            model,
            node => ReferencedSourceFiles(node, model),
            message =>
            {
                _unresolvedRoutes++;
                _diagnostics.Add($"{sourceFile}: {message}");
            });
        if (endpoints.Count > 0)
        {
            _endpointFiles[sourceFile] = endpoints.ToList();
        }
    }

    private IReadOnlyList<string> ReferencedSourceFiles(
        SyntaxNode node,
        SemanticModel model)
    {
        var files = new HashSet<string>(StringComparer.Ordinal);
        foreach (var name in node.DescendantNodesAndSelf()
                     .Where(item => item is IdentifierNameSyntax or GenericNameSyntax)
                     .OfType<SimpleNameSyntax>())
        {
            var symbol = model.GetSymbolInfo(name).Symbol;
            if (symbol is IAliasSymbol alias)
            {
                symbol = alias.Target;
            }
            if (symbol is not (INamedTypeSymbol or IMethodSymbol or IPropertySymbol
                or IFieldSymbol or IEventSymbol))
            {
                continue;
            }

            foreach (var location in symbol.Locations.Where(
                         location => location.IsInSource))
            {
                var path = RelativeSourcePath(location.SourceTree?.FilePath);
                if (path is not null)
                {
                    files.Add(path);
                }
            }

            var containingType = symbol is IMethodSymbol method
                ? method.ContainingType
                : symbol.ContainingType;
            if (containingType is null)
            {
                continue;
            }
            foreach (var location in containingType.Locations.Where(
                         location => location.IsInSource))
            {
                var path = RelativeSourcePath(location.SourceTree?.FilePath);
                if (path is not null)
                {
                    files.Add(path);
                }
            }
        }

        return files.OrderBy(path => path, StringComparer.Ordinal).ToArray();
    }

    private void AddSymbolDependency(string sourceFile, ISymbol symbol)
    {
        if (symbol is IAliasSymbol alias)
        {
            symbol = alias.Target;
        }

        if (symbol is not (INamedTypeSymbol or IMethodSymbol or IPropertySymbol
            or IFieldSymbol or IEventSymbol))
        {
            return;
        }

        foreach (var location in symbol.Locations.Where(location => location.IsInSource))
        {
            var dependencyFile = RelativeSourcePath(location.SourceTree?.FilePath);
            if (dependencyFile is not null && dependencyFile != sourceFile)
            {
                AddEdge(sourceFile, dependencyFile);
            }
        }

        if (symbol is IMethodSymbol method)
        {
            AddSymbolDependency(sourceFile, method.ContainingType);
        }
        else if (symbol.ContainingType is not null)
        {
            AddSymbolDependency(sourceFile, symbol.ContainingType);
        }
    }

    private void AddEdge(string sourceFile, string dependencyFile)
    {
        if (!_forwardDependencies.TryGetValue(sourceFile, out var dependencies))
        {
            dependencies = new(StringComparer.Ordinal);
            _forwardDependencies[sourceFile] = dependencies;
        }

        dependencies.Add(dependencyFile);
    }

    private IReadOnlyDictionary<string, IReadOnlyList<string>> ReverseDependencies()
    {
        var reverse = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
        foreach (var (sourceFile, dependencies) in _forwardDependencies)
        {
            foreach (var dependency in dependencies)
            {
                if (!reverse.TryGetValue(dependency, out var dependents))
                {
                    dependents = new(StringComparer.Ordinal);
                    reverse[dependency] = dependents;
                }

                dependents.Add(sourceFile);
            }
        }

        return reverse
            .OrderBy(item => item.Key, StringComparer.Ordinal)
            .ToDictionary(
                item => item.Key,
                item => (IReadOnlyList<string>)item.Value
                    .OrderBy(path => path, StringComparer.Ordinal)
                    .ToArray(),
                StringComparer.Ordinal);
    }

    private string? RelativeSourcePath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return null;
        }

        var fullPath = Path.GetFullPath(path);
        var relative = Path.GetRelativePath(_root, fullPath);
        if (relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) ||
            Path.IsPathRooted(relative) ||
            IsBuildOutput(relative))
        {
            return null;
        }

        return relative.Replace(Path.DirectorySeparatorChar, '/');
    }

    private string? RelativeProjectPath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return null;
        }

        var relative = Path.GetRelativePath(_root, Path.GetFullPath(path));
        return relative.Replace(Path.DirectorySeparatorChar, '/');
    }

    private static bool IsBuildOutput(string relativePath)
    {
        return relativePath.Split(
                [Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar],
                StringSplitOptions.RemoveEmptyEntries)
            .Any(segment =>
                segment.Equals("bin", StringComparison.OrdinalIgnoreCase) ||
                segment.Equals("obj", StringComparison.OrdinalIgnoreCase));
    }
}