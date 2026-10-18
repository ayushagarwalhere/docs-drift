"""Command-line skeleton; scanning and reporting are not implemented yet."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from docs_drift import __version__
from docs_drift.config import Config, ConfigError, load_config

app = typer.Typer(
    help="Find drift between repository documentation and code.", add_completion=False
)


class OutputFormat(StrEnum):
    """Planned report formats."""

    terminal = "terminal"
    json = "json"
    markdown = "markdown"
    sarif = "sarif"
    html = "html"


@dataclass(frozen=True)
class CLIOptions:
    """Per-invocation state, carried by Typer rather than global variables."""

    repo: Path
    config: Config
    output_format: OutputFormat
    verbose: bool
    no_color: bool


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    repo: Annotated[Path, typer.Option(help="Repository to inspect.")] = Path("."),
    config: Annotated[Path | None, typer.Option(help="Explicit TOML configuration file.")] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--format",
            metavar="FORMAT",
            help="Report format: terminal, json, markdown, sarif, or html.",
        ),
    ] = OutputFormat.terminal,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show detailed diagnostics.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Print the version.")
    ] = False,
) -> None:
    """Read global options and configuration before dispatching a command."""
    if no_color:
        ctx.color = False
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help(), color=not no_color)
        return
    root = repo.resolve()
    if not root.is_dir():
        raise typer.BadParameter("Choose an existing repository directory.", param_hint="--repo")
    try:
        settings = load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    ctx.obj = CLIOptions(root, settings, output_format, verbose, no_color)


def _not_built() -> NoReturn:
    typer.echo("not built yet")
    raise typer.Exit(code=2)


@app.command()
def scan() -> None:
    """Scan a repository and print a report."""
    _not_built()


@app.command()
def check() -> None:
    """Scan and exit non-zero on drift, for CI."""
    _not_built()


@app.command()
def explain() -> None:
    """Show the full evidence chain for one finding."""
    _not_built()


@app.command()
def fix() -> None:
    """Suggest documentation patches."""
    _not_built()


@app.command()
def graph() -> None:
    """Export or query the graph."""
    _not_built()


@app.command()
def init() -> None:
    """Write a starter configuration."""
    _not_built()


@app.command()
def serve() -> None:
    """Start the web demo."""
    _not_built()
