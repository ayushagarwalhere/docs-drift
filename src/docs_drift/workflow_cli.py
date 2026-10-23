"""Organization workflow commands; all target analysis remains static."""

import json
from pathlib import Path
from typing import Annotated

import typer

from docs_drift.collaboration import fix_branch, pr_comparison, static_scan, support_history, write_pr_report
from docs_drift.errors import Command, Group
from docs_drift.ownership import coverage_report
from docs_drift.review import ReviewKind, load_reviews, record_review, review_status

workflow_app = typer.Typer(cls=Group, pretty_exceptions_enable=False)
review = typer.Typer(help="Record source-scoped human decisions.", cls=Group, no_args_is_help=True)
workflow_app.add_typer(review, name="review")


@workflow_app.command("pr", cls=Command)
def pr(
    ctx: typer.Context,
    base: Annotated[str, typer.Option()],
    report_dir: Annotated[Path, typer.Option()],
    reviews: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Compare a fetched base revision with this checkout and write review artifacts."""
    options = ctx.obj
    full, selected, comparison = pr_comparison(options.repo, options.config, base)
    book = load_reviews(options.repo / reviews) if reviews else None
    payload = write_pr_report(selected, comparison, report_dir, book, full_result=full)
    typer.echo(json.dumps(payload, indent=2))


@workflow_app.command("coverage", cls=Command)
def coverage(ctx: typer.Context, important: Annotated[list[str] | None, typer.Option()] = None) -> None:
    """Report checked documents, unmatched claims and declared CODEOWNERS."""
    options = ctx.obj
    typer.echo(json.dumps(coverage_report(static_scan(options.repo, options.config), important), indent=2))


@workflow_app.command("timeline", cls=Command)
def timeline(ctx: typer.Context, claim_id: str, max_commits: Annotated[int, typer.Option(min=1, max=50)] = 20) -> None:
    """Find a bounded first-parent static support boundary for an exact claim ID."""
    options = ctx.obj
    typer.echo(json.dumps(support_history(options.repo, options.config, claim_id, max_commits), indent=2))


@workflow_app.command("fix-branch", cls=Command)
def proposal(
    ctx: typer.Context,
    claim_id: str,
    branch: Annotated[str, typer.Option()],
    destination: Annotated[Path, typer.Option()],
) -> None:
    """Create a local documentation proposal only after its rule edit passes a rescan."""
    options = ctx.obj
    typer.echo(json.dumps(fix_branch(options.repo, options.config, claim_id, branch, destination.resolve()), indent=2))


@review.command("record", cls=Command)
def record(  # noqa: PLR0913, PLR0917 - Typer callback options.
    ctx: typer.Context,
    claim_id: str,
    decision: Annotated[ReviewKind, typer.Option()],
    reason: Annotated[str, typer.Option()],
    reviewer: Annotated[str, typer.Option()],
    reviews: Annotated[Path, typer.Option()] = Path(".docs-drift-reviews.json"),
) -> None:
    """Store a reasoned decision against this claim and its current source fingerprint."""
    options = ctx.obj
    entry = record_review(
        static_scan(options.repo, options.config), options.repo / reviews, claim_id, decision, reason, reviewer
    )
    typer.echo(entry.model_dump_json(indent=2))


@review.command("list", cls=Command)
def decisions(ctx: typer.Context, reviews: Annotated[Path, typer.Option()] = Path(".docs-drift-reviews.json")) -> None:
    """List active decisions and retained historical decisions that have reopened."""
    options = ctx.obj
    book = load_reviews(options.repo / reviews)
    active = review_status(static_scan(options.repo, options.config), book)
    typer.echo(
        json.dumps(
            {
                "active": {key: entry.model_dump(mode="json") for key, entry in active.items()},
                "stored_decisions": len(book.decisions),
            },
            indent=2,
        )
    )


def register(app: typer.Typer) -> None:
    """Attach the statically declared workflow commands to the main CLI."""
    app.add_typer(workflow_app)
