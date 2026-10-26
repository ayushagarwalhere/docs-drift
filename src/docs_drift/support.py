"""Single source of truth for conservative public ecosystem support levels."""

COVERAGE_MAP = {
    "routes:python": "partial",
    "routes:javascript": "partial",
    "cli_command:python": "partial",
    "cli_command:javascript": "partial",
    # Go/Rust: only literal CLI and environment declarations are indexed; every other
    # claim kind about Go/Rust source stays unverifiable through the all:<language> entry.
    **{
        f"{kind}:{language}": "partial"
        for kind in ("cli_flag", "cli_command", "env_var")
        for language in ("go", "rust")
    },
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
    ("Go", "Cobra / pflag / flag / urfave/cli flags, commands and env vars", "partial"),
    ("Rust", "clap (derive and builder) / structopt / argh flags, commands and env vars", "partial"),
    ("Go / Rust", "functions, types, routes and other declarations", "none"),
)


def support_table() -> str:
    rows = ["| Language | Framework / surface | Support |", "| --- | --- | --- |"]
    rows.extend(f"| {language} | {framework} | {level} |" for language, framework, level in ECOSYSTEMS)
    return "\n".join(rows)
