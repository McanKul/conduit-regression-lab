namespace DotnetApiRegression.Analyzer;

internal sealed record AnalyzerOptions(
    string Root,
    string Input,
    string Output,
    bool Restore)
{
    public static AnalyzerOptions Parse(string[] args)
    {
        string? root = null;
        string? input = null;
        string? output = null;
        var restore = true;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--root":
                    root = RequiredValue(args, ref i);
                    break;
                case "--solution":
                case "--project":
                    input = RequiredValue(args, ref i);
                    break;
                case "--output":
                    output = RequiredValue(args, ref i);
                    break;
                case "--no-restore":
                    restore = false;
                    break;
                default:
                    throw new ArgumentException($"Unknown argument: {args[i]}");
            }
        }

        root ??= Directory.GetCurrentDirectory();
        root = Path.GetFullPath(root);
        input ??= DiscoverInput(root);
        input = Path.GetFullPath(input, root);
        output ??= Path.Combine(root, "specs", "semantic-index.json");
        output = Path.GetFullPath(output, root);

        if (!File.Exists(input))
        {
            throw new FileNotFoundException($"Solution or project not found: {input}");
        }

        return new(root, input, output, restore);
    }

    public static string Usage =>
        """
        Roslyn semantic indexer for ASP.NET Core APIs

        Usage:
          dotnet-api-regression-analyze [--root DIR]
            [--solution FILE | --project FILE] [--output FILE] [--no-restore]

        If no solution or project is supplied, the first .slnx, .sln, or .csproj
        under --root is selected deterministically. NuGet restore runs before
        analysis unless --no-restore is supplied.
        """;

    private static string RequiredValue(string[] args, ref int i)
    {
        if (++i >= args.Length)
        {
            throw new ArgumentException($"Missing value for {args[i - 1]}");
        }

        return args[i];
    }

    private static string DiscoverInput(string root)
    {
        foreach (var pattern in new[] { "*.slnx", "*.sln", "*.csproj" })
        {
            var topLevelMatch = Directory.EnumerateFiles(
                    root,
                    pattern,
                    SearchOption.TopDirectoryOnly)
                .OrderBy(path => path, StringComparer.Ordinal)
                .FirstOrDefault();
            if (topLevelMatch is not null)
            {
                return topLevelMatch;
            }

            var match = Directory.EnumerateFiles(
                    root,
                    pattern,
                    SearchOption.AllDirectories)
                .Where(path => !IsBuildOutput(path))
                .OrderBy(path => path, StringComparer.Ordinal)
                .FirstOrDefault();
            if (match is not null)
            {
                return match;
            }
        }

        throw new FileNotFoundException(
            $"No .slnx, .sln, or .csproj file found under {root}");
    }

    private static bool IsBuildOutput(string path)
    {
        var segments = path.Split(Path.DirectorySeparatorChar);
        return segments.Any(segment =>
            segment.Equals("bin", StringComparison.OrdinalIgnoreCase) ||
            segment.Equals("obj", StringComparison.OrdinalIgnoreCase));
    }
}