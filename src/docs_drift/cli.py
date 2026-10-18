"""Command-line skeleton; scanning and reporting are not implemented yet."""

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rapidfuzz import process
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from docs_drift import __version__
from docs_drift.config import Config, ConfigError, load_config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.index import build_index, clear_cache

app = typer.Typer(help="Find drift between repository documentation and code.", add_completion=False)
cache_app = typer.Typer(help="Manage local index cache.", add_completion=False)


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
def scan(
    ctx: typer.Context,
    claims_only: Annotated[
        bool, typer.Option("--claims-only", help="Run extraction only and list documented claims.")
    ] = False,
    show_suppressed: Annotated[
        bool, typer.Option("--show-suppressed", help="Include claims hidden by inline suppressions.")
    ] = False,
) -> None:
    """Scan a repository and print a report."""
    if not claims_only:
        _not_built()
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    result = scan_claims(options.repo, options.config)
    console = Console(no_color=options.no_color, width=180)
    table = Table(title="Documentation claims")
    for heading in (
        "doc:line",
        "section",
        "kind",
        "value",
        "qualifier",
        "expected default",
        "source",
        "confidence",
    ):
        table.add_column(heading)
    docs = {doc.path for doc in result.docs}
    for path in sorted(docs):
        parsed_claims = [claim for claim in result.claims if claim.doc_path == path]
        section_by_id = {section.section_id: section for section in result.sections if section.doc_path == path}
        parsed_claims.sort(key=lambda claim: (section_by_id[claim.section_id].heading_path, claim.line, claim.column))
        for claim in parsed_claims:
            table.add_row(
                f"{claim.doc_path}:{claim.line}",
                " / ".join(section_by_id[claim.section_id].heading_path),
                claim.kind.value,
                claim.value,
                claim.qualifier or "",
                claim.expected_default or "",
                ", ".join(source.value for source in claim.extraction_sources),
                f"{claim.extraction_confidence:.2f}",
            )
    console.print(table)
    if show_suppressed and result.suppressed:
        suppressed_table = Table(title="Suppressed claims")
        suppressed_table.add_column("location")
        suppressed_table.add_column("claim")
        suppressed_table.add_column("reason")
        for item in result.suppressed:
            suppressed_table.add_row(f"{item.claim.doc_path}:{item.claim.line}", item.claim.value, item.reason)
        console.print(suppressed_table)
    by_kind: dict[str, int] = {}
    for claim in result.claims:
        by_kind[claim.kind.value] = by_kind.get(claim.kind.value, 0) + 1
    console.print("Claims: " + ", ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items())))
    console.print(f"Suppressed: {len(result.suppressed)} | historical ignored: {result.historical_ignored}")
    for warning in result.warnings:
        console.print(f"Warning: {warning}", style="yellow")
    if options.verbose:
        console.print(
            "Timings: " + ", ".join(f"{name}={seconds:.4f}s" for name, seconds in sorted(result.timings.items()))
        )


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


@app.command("index")
def index_command(  # noqa: PLR0912
    ctx: typer.Context,
    list_kind: Annotated[str | None, typer.Option("--list", help="List symbols of this kind.")] = None,
    find: Annotated[str | None, typer.Option("--find", help="Fuzzy-search indexed symbols.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Write the complete index as JSON.")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Skip reading and writing the index cache.")] = False,
    tree: Annotated[bool, typer.Option("--tree", help="Show indexed commands and flags as a tree.")] = False,
) -> None:
    """Build or refresh the static source index."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    started = time.perf_counter()
    result = build_index(options.repo, options.config, no_cache=no_cache)
    elapsed = time.perf_counter() - started
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "symbols": [item.model_dump(mode="json") for item in result.symbols],
                    "coverage": result.coverage,
                    "warnings": result.warnings,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if tree:
        command_tree = Tree(options.repo.resolve().name)
        command_symbols = [item for item in result.symbols if item.kind.value == "cli_command"]
        flag_symbols = [item for item in result.symbols if item.kind.value == "cli_flag"]
        branches: dict[str, Tree] = {}
        for command_symbol in command_symbols:
            if command_symbol.name.startswith("npx:"):
                continue
            branch = command_tree.add(command_symbol.name)
            branches[command_symbol.name] = branch
        for flag_symbol in flag_symbols:
            matching = next(
                (
                    name
                    for name in sorted(branches, key=len, reverse=True)
                    if f".{name}." in f".{flag_symbol.qualified_name}."
                ),
                None,
            )
            if matching is not None:
                branches[matching].add(flag_symbol.name)
        Console(no_color=options.no_color).print(command_tree)
        return
    if list_kind:
        matches = [item for item in result.symbols if item.kind.value == list_kind]
        table = Table(title=f"Symbols: {list_kind}")
        for heading in ("name", "qualified name", "file:line", "default", "framework"):
            table.add_column(heading)
        for item in matches:
            table.add_row(
                item.name, item.qualified_name, f"{item.file}:{item.line}", item.default or "", item.framework or ""
            )
        Console(no_color=options.no_color, width=180).print(table)
        return
    if find:
        choices = {f"{item.name}  {item.qualified_name}  {item.file}:{item.line}": item for item in result.symbols}
        fuzzy_matches = process.extract(find, list(choices), limit=10, score_cutoff=35)
        for choice, score, _ in fuzzy_matches:
            typer.echo(f"{score:5.1f}  {choice}")
        return
    kinds: dict[str, int] = {}
    frameworks = sorted({item.framework for item in result.symbols if item.framework})
    table = Table(title="Code index")
    table.add_column("language/files")
    table.add_column("count", justify="right")
    for language, count in sorted(result.files_by_language.items()):
        table.add_row(language, str(count))
    for item in result.symbols:
        kinds[item.kind.value] = kinds.get(item.kind.value, 0) + 1
    Console(no_color=options.no_color, width=180).print(table)
    typer.echo("Symbols: " + ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())))
    typer.echo("Frameworks: " + (", ".join(frameworks) or "none detected"))
    typer.echo(f"Cache: hits={result.cache_hits}, misses={result.cache_misses}; elapsed={elapsed:.3f}s")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)


@cache_app.command("clear")
def clear_index_cache(ctx: typer.Context) -> None:
    """Remove this repository's cached static index."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    typer.echo("Index cache cleared." if clear_cache(options.repo) else "No index cache found.")


app.add_typer(cache_app, name="cache")
