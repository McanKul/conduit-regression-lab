using System.Diagnostics;
using System.Text.Json;

using DotnetApiRegression.Analyzer;

using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.MSBuild;

try
{
    if (args.Any(argument => argument is "-h" or "--help"))
    {
        await Console.Out.WriteLineAsync(AnalyzerOptions.Usage);
        return 0;
    }

    var options = AnalyzerOptions.Parse(args);
    if (options.Restore)
    {
        await Console.Error.WriteLineAsync($"Restoring {options.Input}");
        using var restore = Process.Start(new ProcessStartInfo
        {
            FileName = "dotnet",
            UseShellExecute = false,
            ArgumentList =
            {
                "restore",
                options.Input,
                "--nologo",
            },
        }) ?? throw new InvalidOperationException("Could not start dotnet restore.");
        await restore.WaitForExitAsync();
        if (restore.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"dotnet restore failed with exit code {restore.ExitCode}");
        }
    }

    MSBuildLocator.RegisterDefaults();

    var workspaceDiagnostics = new List<string>();
    using var workspace = MSBuildWorkspace.Create();
    workspace.RegisterWorkspaceFailedHandler(eventArgs =>
    {
        workspaceDiagnostics.Add(
            $"{eventArgs.Diagnostic.Kind}: {eventArgs.Diagnostic.Message}");
    });

    await Console.Error.WriteLineAsync($"Loading {options.Input}");
    Solution solution;
    if (Path.GetExtension(options.Input)
        .Equals(".csproj", StringComparison.OrdinalIgnoreCase))
    {
        var project = await workspace.OpenProjectAsync(options.Input);
        solution = project.Solution;
    }
    else
    {
        solution = await workspace.OpenSolutionAsync(options.Input);
    }

    var index = await new SemanticIndexBuilder(options.Root)
        .BuildAsync(solution, workspaceDiagnostics, CancellationToken.None);

    Directory.CreateDirectory(
        Path.GetDirectoryName(options.Output) ??
        throw new InvalidOperationException("Output has no parent directory."));
    await using var stream = File.Create(options.Output);
    await JsonSerializer.SerializeAsync(
        stream,
        index,
        new JsonSerializerOptions { WriteIndented = true });
    await stream.WriteAsync("\n"u8.ToArray());

    await Console.Out.WriteLineAsync(
        $"Roslyn index: {index.Stats.Endpoints} endpoints, " +
        $"{index.Stats.SourceFiles} files, " +
        $"{index.Stats.DependencyEdges} dependency edges");
    if (index.Stats.UnresolvedRoutes > 0)
    {
        await Console.Error.WriteLineAsync(
            $"Warning: {index.Stats.UnresolvedRoutes} endpoint routes were not compile-time constants.");
    }

    return 0;
}
catch (Exception exception)
{
    await Console.Error.WriteLineAsync($"error: {exception.Message}");
    return 1;
}