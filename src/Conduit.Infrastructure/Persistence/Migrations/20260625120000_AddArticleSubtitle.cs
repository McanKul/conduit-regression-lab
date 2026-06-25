using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Conduit.Infrastructure.Persistence.Migrations;

/// <inheritdoc />
public partial class AddArticleSubtitle : Migration
{
    /// <inheritdoc />
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "Subtitle",
            table: "Articles",
            type: "character varying(255)",
            maxLength: 255,
            nullable: true);
    }

    /// <inheritdoc />
    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "Subtitle",
            table: "Articles");
    }
}
