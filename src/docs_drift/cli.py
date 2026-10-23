"""Consistent command-line entry points for scanning, reporting and fixes."""

import asyncio
import json
import math
import sys
import time
import tomllib
from dataclasses import dataclass, replace
from enum import StrEnum
from io import TextIOWrapper
from pathlib import Path
from typing import Annotated

import tomli_w
import typer
from rapidfuzz import process
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from docs_drift import __version__
from docs_drift.baseline import load_baseline, new_findings, save_baseline
from docs_drift.config import Config, ConfigError, HistoryConfig, load_config
from docs_drift.docs.defaults import normalize_value
from docs_drift.docs.pipeline import scan_claims
from docs_drift.errors import Command, Group
from docs_drift.fix.edits import StaleDocumentError, apply_file, unified_patch
from docs_drift.fix.model import add_model_fixes
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.history import GitHistory
from docs_drift.index import build_index, clear_cache
from docs_drift.index.base import CodeIndex
from docs_drift.llm.client import ClientError
from docs_drift.llm.pricing import format_usage
from docs_drift.llm.verify import verify_prose
from docs_drift.match import match_claims
from docs_drift.models import Claim, ClaimKind, Evidence, Finding, ScanResult, SymbolKind, Verdict
from docs_drift.report import RenderOptions, render

app = typer.Typer(
    help="Find drift between repository documentation and code.", cls=Group, pretty_exceptions_enable=False
)
cache_app = typer.Typer(help="Manage local index cache.", cls=Group, add_completion=False)
baseline_app = typer.Typer(help="Manage the finding baseline.", cls=Group, no_args_is_help=True)
_GENERIC_ENV_TOKENS = {"app", "env", "var", "api", "service"}


def _same_env_concept(old: str, new: str) -> bool:
    """Require a shared specific token; generic prefixes alone do not prove a rename."""
    old_parts = {part.casefold() for part in old.split("_")} - _GENERIC_ENV_TOKENS
    new_parts = {part.casefold() for part in new.split("_")} - _GENERIC_ENV_TOKENS
    return bool(old_parts & new_parts)


class OutputFormat(StrEnum):
    """Supported report formats."""

    terminal = "terminal"
    json = "json"
    markdown = "markdown"
    sarif = "sarif"
    html = "html"
    junit = "junit"
    github = "github"


@dataclass(frozen=True)
class CLIOptions:
    """Per-invocation state, carried by Typer rather than global variables."""

    repo: Path
    config: Config
    output_format: OutputFormat
    verbose: bool
    no_color: bool
    quiet: bool = False
    output: Path | None = None
    output_json: Path | None = None
    output_sarif: Path | None = None
    show_authors: bool = False


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
            help="Report format: terminal, json, markdown, sarif, html, junit, or github.",
        ),
    ] = OutputFormat.terminal,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed diagnostics.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress routine stdout output.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show the traceback when an operation fails.")] = False,
    output: Annotated[Path | None, typer.Option(help="Write the selected report to this file.")] = None,
    output_json: Annotated[Path | None, typer.Option(help="Also write the full JSON report.")] = None,
    output_sarif: Annotated[Path | None, typer.Option(help="Also write a SARIF report.")] = None,
    show_authors: Annotated[bool, typer.Option(help="Include author names in HTML reports.")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Print the version.")
    ] = False,
) -> None:
    """Read global options and configuration before dispatching a command."""
    if isinstance(sys.stdout, TextIOWrapper) and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    if no_color:
        ctx.color = False
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help(), color=not no_color)
        return
    destinations = [path.resolve() for path in (output, output_json, output_sarif) if path is not None]
    if len(set(destinations)) != len(destinations):
        raise typer.BadParameter("Choose a different path for each report destination.", param_hint="--output")
    root = repo.resolve()
    if not root.is_dir():
        raise typer.BadParameter("Choose an existing repository directory.", param_hint="--repo")
    try:
        settings = Config() if ctx.meta.get("help_only") else load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    ctx.obj = CLIOptions(
        root, settings, output_format, verbose, no_color, quiet, output, output_json, output_sarif, show_authors
    )


def _model_layer(result: ScanResult, enabled: bool, budget: float | None) -> ScanResult:
    config = result.config_used
    if not enabled and not config.llm.enabled:
        return result
    if budget is not None and (not math.isfinite(budget) or budget < 0):
        raise typer.BadParameter("Use a finite, non-negative USD amount.", param_hint="--llm-budget")
    llm_config = config.llm.model_copy(update={"enabled": True, **({"budget": budget} if budget is not None else {})})
    result = result.model_copy(update={"config_used": config.model_copy(update={"llm": llm_config})})
    marker = result.repo_path / ".docs-drift" / "llm-privacy-v1"
    if not marker.exists():
        typer.echo(
            "Model privacy notice: live/record mode sends selected doc sentences and up to 6 redacted source "
            "snippets of 60 lines each through OpenRouter to the configured model provider. Replay stays local. "
            "Recordings store request hashes and validated responses. See docs/SECURITY.md.",
            err=True,
        )
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("notice-v1\n", encoding="utf-8")
        except OSError:
            pass
    try:
        return asyncio.run(verify_prose(result))
    except (ClientError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--llm") from exc


def _full_findings(
    claims: list[Claim],
    index: CodeIndex,
    repo: Path,
    *,
    initial: list[Finding] | None = None,
    history_config: HistoryConfig | None = None,
) -> tuple[list[Finding], list[str]]:
    """Match claims and enrich drift findings with text-only Git history evidence."""
    findings = match_claims(claims, index, repo) if initial is None else initial
    history = GitHistory(repo, history_config)
    if any(f.is_drift or f.verdict == Verdict.unverifiable for f in findings):
        history.prepare()
    enriched: list[Finding] = []
    history_kinds = {
        ClaimKind.cli_flag: SymbolKind.cli_flag,
        ClaimKind.cli_command: SymbolKind.cli_command,
        ClaimKind.env_var: SymbolKind.env_var,
        ClaimKind.endpoint: SymbolKind.route,
        ClaimKind.config_key: SymbolKind.config_key,
        ClaimKind.package_script: SymbolKind.package_script,
        ClaimKind.make_target: SymbolKind.make_target,
    }
    for original in findings:
        default_without_subject = (
            original.claim.kind == ClaimKind.default_value
            and original.verdict == Verdict.unverifiable
            and not any(
                symbol.name == original.claim.value or original.claim.value in symbol.aliases
                for symbol in index.symbols
            )
        )
        if not original.is_drift and not default_without_subject:
            enriched.append(original)
            continue
        current = original
        if current.verdict == Verdict.default_changed:
            commit, excerpt = history.default_change(current)
            if commit:
                current = current.model_copy(
                    update={
                        "introduced_by": commit,
                        "evidence": [
                            *current.evidence,
                            Evidence(
                                file=current.matched_symbol.file if current.matched_symbol else current.claim.doc_path,
                                line=current.matched_symbol.line if current.matched_symbol else 1,
                                text=excerpt,
                            ),
                        ],
                    }
                )
            enriched.append(current)
            continue
        replacements = [current.suggestion] if current.suggestion else []
        if current.claim.kind in history_kinds:
            replacements.extend(symbol.name for symbol in index.by_kind.get(history_kinds[current.claim.kind], []))
        if current.claim.kind == ClaimKind.default_value:
            replacements.extend(symbol.name for symbol in index.by_kind.get(SymbolKind.config_key, []))
        old_history_name = current.claim.value
        if current.claim.kind == ClaimKind.endpoint:
            old_history_name = current.claim.raw_text.strip("`").split(maxsplit=1)[-1]
            replacements = [name.split(maxsplit=1)[-1] for name in replacements]
            replacements.extend(
                symbol.raw_path or symbol.name.split(maxsplit=1)[-1]
                for symbol in index.by_kind.get(SymbolKind.route, [])
            )
        match = history.search(old_history_name, tuple(replacements))
        if match.rename:
            replacement_name = match.rename.new_name.strip('"')
            replacement_symbol = next(
                (
                    symbol
                    for symbol in index.symbols
                    if (
                        symbol.name == replacement_name
                        or symbol.name.endswith(" " + replacement_name)
                        or symbol.raw_path == replacement_name
                    )
                    and symbol.file == match.rename.file
                ),
                None,
            )
            verdict: Verdict = current.verdict
            suggestion = current.suggestion
            reasons = list(current.reasons)
            if default_without_subject and replacement_symbol and replacement_symbol.default is not None:
                expected = normalize_value(current.claim.expected_default or "", subject=current.claim.value)
                actual = normalize_value(replacement_symbol.default, subject=replacement_name)
                equality = expected.equals(actual)
                verdict = Verdict.default_changed if equality is False else Verdict.renamed
                suggestion = (
                    f"{replacement_name} (default {replacement_symbol.default})"
                    if equality is False
                    else replacement_name
                )
                reasons.append(
                    f"The documented setting was renamed to {replacement_name} and its code default is "
                    f"{replacement_symbol.default}."
                )
            elif current.verdict in {Verdict.missing, Verdict.unverifiable} and (
                current.claim.kind != ClaimKind.env_var or _same_env_concept(current.claim.value, replacement_name)
            ):
                verdict = Verdict.renamed
                suggestion = replacement_name
                reasons.append(f"Git history confirms this name changed to {replacement_name}.")
            current = current.model_copy(
                update={
                    "verdict": verdict,
                    "suggestion": suggestion,
                    "matched_symbol": replacement_symbol or current.matched_symbol,
                    "confidence": max(current.confidence, 0.92),
                    "evidence": [
                        *current.evidence,
                        Evidence(
                            file=match.rename.file or current.claim.doc_path,
                            line=1,
                            text=match.rename.hunk_excerpt,
                        ),
                    ],
                    "reasons": [*reasons, f"Git history changed this name in {match.rename.commit.short_sha}."],
                }
            )
        if match.commit:
            current = current.model_copy(update={"introduced_by": match.commit})
        enriched.append(current)
    return enriched, history.warnings


@app.command(cls=Command)
def scan(
    ctx: typer.Context,
    claims_only: Annotated[
        bool, typer.Option("--claims-only", help="Run extraction only and list documented claims.")
    ] = False,
    show_suppressed: Annotated[
        bool, typer.Option("--show-suppressed", help="Include claims hidden by inline suppressions.")
    ] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Include OK findings.")] = False,
    show_low_confidence: Annotated[bool, typer.Option("--show-low-confidence", help="Include weak findings.")] = False,
    llm: Annotated[bool, typer.Option("--llm", help="Assess anchored prose through OpenRouter.")] = False,
    llm_budget: Annotated[float | None, typer.Option("--llm-budget")] = None,
    docs_repo: Annotated[
        Path | None, typer.Option("--docs-repo", help="Read documentation from a separate repository.")
    ] = None,
    package: Annotated[str | None, typer.Option("--package", help="Scan only the named workspace package.")] = None,
) -> None:
    """Scan a repository and print a report."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    if claims_only:
        result = scan_claims(docs_repo or options.repo, options.config)
        if options.output_format != OutputFormat.terminal:
            _print_findings(result, options, show_all=True, show_low_confidence=True)
        else:
            _print_claims(result, options, show_suppressed)
        return
    import sys  # noqa: PLC0415 - optional dependency or shared pipeline import.

    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    result = run_scan(
        options.repo,
        options.config,
        docs_repo=docs_repo,
        package=package,
        progress=sys.stderr.isatty() and not options.quiet and not options.no_color,
    )
    result = _model_layer(result, llm, llm_budget)
    _print_findings(result, options, show_all=show_all, show_low_confidence=show_low_confidence)


def _print_claims(result: ScanResult, options: CLIOptions, show_suppressed: bool) -> None:
    console = Console(
        force_terminal=sys.stdout.isatty() and not options.no_color, markup=False, no_color=options.no_color, width=180
    )
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


def _print_findings(
    result: ScanResult,
    options: CLIOptions,
    *,
    show_all: bool,
    show_low_confidence: bool,
) -> None:
    import shutil  # noqa: PLC0415 - optional dependency or shared pipeline import.
    import sys  # noqa: PLC0415 - optional dependency or shared pipeline import.

    tty = sys.stdout.isatty() and not options.output
    render_options = RenderOptions(
        color=tty and not options.no_color,
        hyperlinks=tty and not options.no_color,
        width=shutil.get_terminal_size((100, 24)).columns,
        verbose=options.verbose,
        show_all=show_all,
        show_low_confidence=show_low_confidence,
        show_authors=options.show_authors,
    )
    destinations = [
        (options.output_format.value, options.output),
        ("json", options.output_json),
        ("sarif", options.output_sarif),
    ]
    for number, (format_name, destination) in enumerate(destinations):
        if number and destination is None:
            continue
        text = render(result, format_name, render_options)
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8", newline="\n")
        elif not options.quiet:
            typer.echo(text, nl=False, color=render_options.color)


@app.command(cls=Command)
def check(
    ctx: typer.Context,
    fail_on: Annotated[str, typer.Option("--fail-on", help="Exit 1 on drift, missing, or any finding.")] = "drift",
    min_confidence: Annotated[float | None, typer.Option("--min-confidence")] = None,
    max_findings: Annotated[int | None, typer.Option("--max-findings")] = None,
    baseline: Annotated[
        Path | None, typer.Option("--baseline", help="Fail only for drifts absent from this baseline.")
    ] = None,
    reviews: Annotated[Path | None, typer.Option(help="Source-scoped human review decisions.")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Print nothing unless the check fails.")] = False,
    llm: Annotated[bool, typer.Option("--llm")] = False,
    llm_budget: Annotated[float | None, typer.Option("--llm-budget")] = None,
) -> None:
    """Run a full scan; exit 1 for configured findings, or 2 for usage errors."""
    if fail_on not in {"drift", "missing", "any"}:
        raise typer.BadParameter("Choose drift, missing, or any.", param_hint="--fail-on")
    if min_confidence is not None and not 0 <= min_confidence <= 1:
        raise typer.BadParameter("Choose a confidence between 0 and 1.", param_hint="--min-confidence")
    if max_findings is not None and max_findings < 0:
        raise typer.BadParameter("Choose a non-negative count.", param_hint="--max-findings")
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    result = _model_layer(run_scan(options.repo, options.config), llm, llm_budget)
    history_warnings: list[str] = []
    findings = result.findings
    baselined = 0
    findings = result.findings
    if baseline is not None:
        baseline_path = baseline if baseline.is_absolute() else options.repo / baseline
        try:
            original_count = len(findings)
            findings = new_findings(findings, load_baseline(baseline_path))
            baselined = original_count - len(findings)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--baseline") from exc
    threshold = options.config.min_confidence if min_confidence is None else min_confidence
    from docs_drift.review import load_reviews, suppressed_ids  # noqa: PLC0415

    suppressed = suppressed_ids(result, load_reviews(options.repo / reviews)) if reviews else set()
    if suppressed:
        history_warnings.append(
            f"{len(suppressed)} source-scoped reviewed findings excluded from the gate; retained below."
        )
    qualifying = [item for item in findings if item.confidence >= threshold and item.claim.claim_id not in suppressed]
    if fail_on == "drift":
        failures = [item for item in qualifying if item.is_drift]
    elif fail_on == "missing":
        failures = [item for item in qualifying if item.verdict in {Verdict.missing, Verdict.invalid_command}]
    else:
        failures = [item for item in qualifying if item.verdict != Verdict.ok]
    if max_findings is not None:
        failures = failures if len(failures) > max_findings else []
    if not (quiet or options.quiet) or failures or options.output or options.output_json or options.output_sarif:
        _print_findings(
            result.model_copy(
                update={
                    "baselined": baselined,
                    "findings": findings,
                    "warnings": [*result.warnings, *history_warnings],
                }
            ),
            replace(options, quiet=False) if failures else options,
            show_all=fail_on == "any",
            show_low_confidence=True,
        )
    if failures:
        raise typer.Exit(code=1)


@app.command(cls=Command)
def explain(
    ctx: typer.Context,
    finding_id_prefix: Annotated[str, typer.Argument(metavar="FINDING_ID")],
    llm: Annotated[bool, typer.Option("--llm")] = False,
) -> None:
    """Show documentation, source, reasons, confidence and history evidence for one finding."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    scan = _model_layer(run_scan(options.repo, options.config), llm, None)
    findings = scan.findings
    matches = [item for item in findings if item.claim.claim_id.startswith(finding_id_prefix)]
    if len(matches) != 1:
        raise typer.BadParameter("Use a prefix that identifies exactly one finding.", param_hint="FINDING_ID")
    finding = matches[0]
    if options.output_format != OutputFormat.terminal:
        _print_findings(
            scan.model_copy(update={"findings": [finding]}), options, show_all=True, show_low_confidence=True
        )
        return
    payload = {
        "id": finding.claim.claim_id,
        "claim": finding.claim.model_dump(mode="json"),
        "verdict": finding.verdict.value,
        "suggestion": finding.suggestion,
        "confidence": finding.confidence,
        "decided_by": finding.decided_by.value,
        "replayed": finding.replayed,
        "reasons": finding.reasons,
        "evidence": [item.model_dump(mode="json") for item in finding.evidence],
        "matched_symbol": finding.matched_symbol.model_dump(mode="json") if finding.matched_symbol else None,
        "introduced_by": finding.introduced_by.model_dump(mode="json") if finding.introduced_by else None,
    }
    Console(
        force_terminal=sys.stdout.isatty() and not options.no_color, markup=False, no_color=options.no_color, width=140
    ).print(Panel.fit(json.dumps(payload, indent=2), title="Finding evidence"))


@baseline_app.command("update", cls=Command)
def baseline_update(
    ctx: typer.Context,
    path: Path = Path(".docs-drift-baseline.json"),
    llm: Annotated[bool, typer.Option("--llm")] = False,
) -> None:
    """Replace the baseline with current drift findings; fixed findings are pruned."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    from docs_drift.scanning import run_scan  # noqa: PLC0415

    scan = _model_layer(run_scan(options.repo, options.config), llm, None)
    findings = scan.findings
    target = path if path.is_absolute() else options.repo / path
    save_baseline(target, findings)
    typer.echo(f"Saved {sum(item.is_drift for item in findings)} findings to {target}.")


@app.command("ignore", cls=Command)
def ignore_claim(ctx: typer.Context, claim_id_prefix: Annotated[str, typer.Argument(metavar="CLAIM_ID")]) -> None:
    """Add a stable claim ID to the repository ignore list."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    scan = scan_claims(options.repo, options.config)
    matches = [claim for claim in scan.claims if claim.claim_id.startswith(claim_id_prefix)]
    if len(matches) != 1:
        raise typer.BadParameter("Use a prefix that identifies exactly one claim.", param_hint="CLAIM_ID")
    path = options.repo / ".docs-drift.toml"
    data: dict[str, object] = {}
    if not path.exists():
        pyproject = options.repo / "pyproject.toml"
        if pyproject.is_file():
            try:
                source = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
                tool = source.get("tool", {})
                if isinstance(tool, dict) and isinstance(tool.get("docs-drift"), dict):
                    data = tool["docs-drift"]
            except tomllib.TOMLDecodeError as exc:
                raise typer.BadParameter(f"Cannot read pyproject.toml: {exc}.") from exc
    else:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise typer.BadParameter(f"Cannot update {path}: {exc}.") from exc
    ignored = data.get("ignore", [])
    if not isinstance(ignored, list) or not all(isinstance(item, str) for item in ignored):
        raise typer.BadParameter("The ignore setting must be a list of strings.")
    if matches[0].claim_id not in ignored:
        data["ignore"] = [*ignored, matches[0].claim_id]
        path.write_text(tomli_w.dumps(data), encoding="utf-8", newline="\n")
    typer.echo(f"Ignored {matches[0].claim_id} in {path}.")


@app.command(cls=Command)
def fix(  # noqa: PLR0912 - preview, confirmation, and stale-scan retry paths.
    ctx: typer.Context,
    write: Annotated[bool, typer.Option("--write", help="Apply edits after confirmation per file.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip --write confirmations.")] = False,
    patch: Annotated[Path | None, typer.Option("--patch", help="Write a git-applyable patch.")] = None,
    llm: Annotated[bool, typer.Option("--llm")] = False,
    llm_budget: Annotated[float | None, typer.Option("--llm-budget")] = None,
) -> None:
    """Preview exact documentation edits; only --write applies them."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    for attempt in range(2):
        from docs_drift.scanning import run_scan  # noqa: PLC0415

        scan_result = run_scan(options.repo, options.config)
        index = build_index(options.repo, options.config)
        scan_result = _model_layer(scan_result, llm, llm_budget)
        try:
            plan = plan_rule_fixes(scan_result)
            if llm or options.config.llm.enabled:
                try:
                    plan = asyncio.run(add_model_fixes(scan_result, index, plan))
                except (ClientError, ValueError) as exc:
                    raise typer.BadParameter(str(exc), param_hint="--llm") from exc
            diff = unified_patch(options.repo, plan)
            if options.output_format == OutputFormat.json:
                typer.echo(plan.model_dump_json(indent=2))
            else:
                console = Console(
                    force_terminal=sys.stdout.isatty() and not options.no_color,
                    markup=False,
                    no_color=options.no_color,
                    highlight=False,
                )
                for line in diff.splitlines(keepends=True):
                    style = "green" if line.startswith("+") else "red" if line.startswith("-") else None
                    console.print(line, style=style, end="", markup=False)
                for note in plan.suggestions:
                    console.print(f"Review: {note}", markup=False)
                fixed = len({edit.claim_id for edit in plan.edits})
                total = sum(item.is_drift for item in scan_result.findings)
                console.print(f"Fixes: {fixed} auto-fixable, {max(0, total - fixed)} suggestion-only findings.")
                if scan_result.llm_usage:
                    console.print(format_usage(scan_result.llm_usage))
                if any(edit.decided_by == "llm" for edit in plan.edits):
                    console.print(
                        "Model-assisted sentence edits passed static validation; review them before applying."
                    )
                if any(item.replayed for item in scan_result.findings) or any(edit.replayed for edit in plan.edits):
                    console.print("Model verdicts replayed from recordings.")
            if patch:
                patch.write_bytes(diff.encode("utf-8"))
            if write:
                for relative in sorted({edit.file for edit in plan.edits}):
                    if yes or typer.confirm(f"Apply proposed edits to {relative}?", default=False):
                        apply_file(options.repo, plan, relative)
            return
        except StaleDocumentError as exc:
            if attempt:
                raise typer.BadParameter(str(exc)) from exc
            typer.echo(f"{exc} Re-scanning before presenting fresh edits.", err=True)


@app.command(cls=Command)
def graph(ctx: typer.Context) -> None:
    """Export a scan with the evidence graph in HTML or JSON."""
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    _print_findings(run_scan(options.repo, options.config), options, show_all=True, show_low_confidence=True)


@app.command("insights", cls=Command)
def insights_command(ctx: typer.Context) -> None:
    """Summarize findings by verdict, document and workspace package."""
    from docs_drift.analysis import insights  # noqa: PLC0415 - optional command path.
    from docs_drift.scanning import run_scan  # noqa: PLC0415

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    text = json.dumps(insights(run_scan(options.repo, options.config)), indent=2, sort_keys=True) + "\n"
    if options.output:
        options.output.write_text(text, encoding="utf-8")
    elif not options.quiet:
        typer.echo(text, nl=False)


@app.command("impact", cls=Command)
def impact_command(
    ctx: typer.Context,
    base: Annotated[str, typer.Option(help="Base revision, for example HEAD~1.")],
    head: Annotated[str, typer.Option(help="Current checkout revision.")] = "HEAD",
) -> None:
    """Show current findings connected to documentation or source changed since a revision."""
    from docs_drift.analysis import impact  # noqa: PLC0415 - optional command path.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    _print_findings(impact(options.repo, options.config, base, head), options, show_all=True, show_low_confidence=True)


@app.command(cls=Command)
def init(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Accept detected defaults without prompting.")] = False,
) -> None:
    """Detect documentation and write a commented repository configuration."""
    from docs_drift.setup import initialize  # noqa: PLC0415 - optional dependency or shared pipeline import.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    initialize(options.repo, yes=yes, quiet=options.quiet)


@app.command(cls=Command)
def serve(
    ctx: typer.Context, port: Annotated[int, typer.Option(help="Local HTTP port.", min=1, max=65535)] = 8000
) -> None:
    """Serve a local HTML report in the browser."""
    from functools import partial  # noqa: PLC0415 - optional dependency or shared pipeline import.
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415 - optional dependency or shared pipeline import.

    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    directory = options.repo / ".docs-drift" / "report"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(render(run_scan(options.repo, options.config), "html"), encoding="utf-8")
    typer.echo(f"Report available at http://127.0.0.1:{port}; press Ctrl+C to stop.")
    with ThreadingHTTPServer(
        ("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=str(directory))
    ) as server:
        server.serve_forever()


@app.command("index", cls=Command)
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
    if json_output or options.output_format == OutputFormat.json:
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
        Console(
            force_terminal=sys.stdout.isatty() and not options.no_color, markup=False, no_color=options.no_color
        ).print(command_tree)
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
        Console(
            force_terminal=sys.stdout.isatty() and not options.no_color,
            markup=False,
            no_color=options.no_color,
            width=180,
        ).print(table)
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
    Console(
        force_terminal=sys.stdout.isatty() and not options.no_color, markup=False, no_color=options.no_color, width=180
    ).print(table)
    typer.echo("Symbols: " + ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())))
    typer.echo("Frameworks: " + (", ".join(frameworks) or "none detected"))
    typer.echo(f"Cache: hits={result.cache_hits}, misses={result.cache_misses}; elapsed={elapsed:.3f}s")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)


@cache_app.command("clear", cls=Command)
def clear_index_cache(ctx: typer.Context) -> None:
    """Remove this repository's cached static index."""
    options: CLIOptions = ctx.ensure_object(CLIOptions)
    typer.echo("Index cache cleared." if clear_cache(options.repo) else "No index cache found.")


@app.command(cls=Command)
def portfolio(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option("--manifest", help="TOML repository manifest, relative to --repo.")],
    report_dir: Annotated[Path, typer.Option("--report-dir", help="Report directory outside all scanned checkouts.")],
    report_only: Annotated[
        bool, typer.Option("--report-only", help="Collect reports without failing on drift.")
    ] = False,
) -> None:
    """Scan up to 100 local checkouts and generate an organization dashboard."""
    from docs_drift.portfolio import scan_portfolio  # noqa: PLC0415 - load only for portfolio scans.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    manifest_path = manifest if manifest.is_absolute() else options.repo / manifest
    destination = report_dir if report_dir.is_absolute() else options.repo / report_dir
    try:
        result = scan_portfolio(options.repo, manifest_path, destination, report_only=report_only)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--manifest / --report-dir") from exc
    for repository in result.repositories:
        counts = f"; {repository.gate.new_drift} new drift" if repository.gate else ""
        typer.echo(f"{repository.name}: {repository.status}{counts}")
    typer.echo(f"Dashboard: {destination.resolve() / 'index.html'}")
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


app.add_typer(cache_app, name="cache")
app.add_typer(baseline_app, name="baseline")


@app.command(cls=Command)
def watch(ctx: typer.Context) -> None:
    """Recheck changed documents and source files with immediate feedback."""
    from docs_drift.watch import watch_repository  # noqa: PLC0415 - optional dependency or shared pipeline import.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    watch_repository(options.repo, options.config, quiet=options.quiet)


@app.command(cls=Command)
def lsp(ctx: typer.Context) -> None:
    """Start the language server over standard input and output."""
    from docs_drift.lsp import start  # noqa: PLC0415 - optional dependency or shared pipeline import.

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    start(options.repo, options.config)


from docs_drift.workflow_cli import register as register_workflows  # noqa: E402

register_workflows(app)
