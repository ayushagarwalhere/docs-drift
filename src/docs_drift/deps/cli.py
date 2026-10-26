"""`docs-drift deps`: dependency drift between documentation, manifests and source imports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from docs_drift.errors import Command, Group

deps_app = typer.Typer(cls=Group, pretty_exceptions_enable=False)


@deps_app.command("deps", cls=Command)
def deps(  # noqa: PLR0913, PLR0917 - Typer callback options.
    ctx: typer.Context,
    base: Annotated[
        str | None, typer.Option("--base", help="Report only drift and issues absent at this fetched revision.")
    ] = None,
    baseline: Annotated[
        Path | None, typer.Option("--baseline", help="Hide drift and issues recorded in this dependency baseline.")
    ] = None,
    write_baseline: Annotated[
        Path | None, typer.Option("--write-baseline", help="Record the current results as a dependency baseline.")
    ] = None,
    check: Annotated[bool, typer.Option("--check", help="Exit 1 when qualifying drift or issues remain.")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Include OK claims and low-confidence issues.")] = False,
) -> None:
    """Check documented dependencies and runtimes against manifests, and imports against declarations."""
    from docs_drift.deps.report import (  # noqa: PLC0415 - command-only imports keep CLI start-up fast.
        apply_baseline,
        load_deps_baseline,
        render_json,
        render_markdown,
        render_terminal,
        run_deps,
        save_deps_baseline,
        visible,
    )

    options = ctx.obj
    renderers = {"terminal": render_terminal, "json": render_json, "markdown": render_markdown}
    if options.output_format.value not in renderers:
        raise typer.BadParameter("deps supports terminal, json and markdown.", param_hint="--format")
    report = run_deps(options.repo, options.config, base=base)
    if write_baseline is not None:
        save_deps_baseline(write_baseline if write_baseline.is_absolute() else options.repo / write_baseline, report)
    if baseline is not None:
        path = baseline if baseline.is_absolute() else options.repo / baseline
        try:
            report = apply_baseline(report, load_deps_baseline(path))
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--baseline") from exc
    shown = visible(report, options.config.min_confidence, show_all)
    if not options.quiet or options.output:
        typer.echo(renderers[options.output_format.value](shown), nl=False)
    qualifying = [f for f in shown.findings if f.is_drift and f.confidence >= options.config.min_confidence]
    issues = [i for i in shown.issues if i.confidence >= options.config.min_confidence]
    if check and (qualifying or issues):
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.add_typer(deps_app)
