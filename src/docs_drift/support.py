"""Single source of truth for conservative public ecosystem support levels."""

COVERAGE_MAP = {
    "routes:python": "partial",
    "routes:javascript": "partial",
    "cli_command:python": "partial",
    "cli_command:javascript": "partial",
    **{f"all:{language}": "none" for language in ("java", "go", "rust", "ruby", "php", "csharp")},
}
ECOSYSTEMS = (
    ("Python", "AST declarations", "partial"),
    ("Python", "argparse", "partial"),
    ("Python", "Click", "partial"),
    ("Python", "Typer", "partial"),
    ("Python", "FastAPI", "partial"),
    ("Python", "Flask", "partial"),
    ("Python", "Django / DRF", "none"),
    ("JavaScript / TypeScript", "AST declarations", "partial"),
    ("JavaScript / TypeScript", "Commander", "partial"),
    ("JavaScript / TypeScript", "yargs", "none"),
    ("JavaScript / TypeScript", "Express", "partial"),
    ("JavaScript / TypeScript", "Next.js / NestJS routes", "none"),
    ("JSON / YAML / TOML / INI", "literal settings", "partial"),
    ("Go / Rust", "language-specific configuration", "none"),
)


def support_table() -> str:
    rows = ["| Language | Framework / surface | Support |", "| --- | --- | --- |"]
    rows.extend(f"| {language} | {framework} | {level} |" for language, framework, level in ECOSYSTEMS)
    return "\n".join(rows)
