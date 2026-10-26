"""`docs-drift contribute`: issue drafts and a docs health card for contributors."""

from pathlib import Path
from typing import Annotated

import typer

from docs_drift.errors import Command, Group

contribute_app = typer.Typer(cls=Group, pretty_exceptions_enable=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@contribute_app.command("contribute", cls=Command)
def contribute(
    ctx: typer.Context,
    out_dir: Annotated[
        Path | None, typer.Option("--out-dir", help="Write one Markdown issue draft per file into this folder.")
    ] = None,
    summary: Annotated[
        bool, typer.Option("--summary", help="Print a docs health card for CONTRIBUTING.md instead of issue drafts.")
    ] = False,
    min_confidence: Annotated[
        float | None, typer.Option("--min-confidence", min=0.0, max=1.0, help="Minimum finding confidence.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Maximum number of issue drafts.")] = 50,
) -> None:
    """Turn well-evidenced drift into beginner-friendly issue drafts with ready patches."""
    from docs_drift.cli import CLIOptions, OutputFormat  # noqa: PLC0415 - the CLI module registers this command.
    from docs_drift.contribute import (  # noqa: PLC0415 - load only for this command.
        build_contributions,
        digest_markdown,
        health_card,
        health_card_markdown,
        issue_filename,
        issue_markdown,
    )
    from docs_drift.scanning import run_scan  # noqa: PLC0415

    options: CLIOptions = ctx.ensure_object(CLIOptions)
    if options.output_format not in {OutputFormat.terminal, OutputFormat.markdown, OutputFormat.json}:
        raise typer.BadParameter("Choose markdown, json or the default terminal format.", param_hint="--format")
    as_json = options.output_format == OutputFormat.json
    scan = run_scan(options.repo, options.config)
    report = build_contributions(scan, min_confidence=min_confidence, limit=limit)
    if summary:
        card = health_card(scan, report)
        text = card.model_dump_json(indent=2) + "\n" if as_json else health_card_markdown(card)
    elif out_dir is not None:
        destination = out_dir
        names = []
        for number, issue in enumerate(report.issues, 1):
            name = issue_filename(number, issue)
            _write(destination / name, issue_markdown(issue))
            names.append(name)
        if as_json:
            _write(destination / "issues.json", report.model_dump_json(indent=2) + "\n")
        text = f"Wrote {len(names)} issue drafts to {destination}.\n" + "".join(f"  {name}\n" for name in names)
    else:
        text = report.model_dump_json(indent=2) + "\n" if as_json else digest_markdown(report)
    if options.output and not out_dir:
        _write(options.output, text)
    elif not options.quiet:
        typer.echo(text, nl=False)


def register(app: typer.Typer) -> None:
    """Attach the contributor command to the main CLI."""
    app.add_typer(contribute_app)
