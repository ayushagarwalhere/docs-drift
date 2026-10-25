"""`docs-drift github ...` commands: publish PR reviews and tracking issues, register the App.

Tokens are read from GITHUB_TOKEN / GH_TOKEN only, never from command-line
arguments (which are visible in process listings and shell history).
"""

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from docs_drift.errors import Command, Group

if TYPE_CHECKING:  # Typing-only names; the github extra is imported lazily by each command.
    from docs_drift.github.bundle import ReviewBundle
    from docs_drift.github.publish import PublishReport

github_app = typer.Typer(
    help="Publish findings to GitHub: sticky PR reviews with suggestions, tracking issues, and the GitHub App.",
    cls=Group,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    epilog="Example: docs-drift github pr-review --dry-run --pr 1 --base origin/main --repo .",
)

BundleOption = Annotated[
    Path | None, typer.Option("--bundle", help="Publish a precomputed bundle (written by the Action scan step).")
]
SlugOption = Annotated[
    str | None, typer.Option("--github-repository", help="owner/name; defaults to GITHUB_REPOSITORY.")
]
DryRunOption = Annotated[bool, typer.Option("--dry-run", help="Print what would be posted; write nothing.")]
ReviewsOption = Annotated[Path | None, typer.Option("--reviews", help="Review-decision JSON relative to --repo.")]
BaselineOption = Annotated[Path | None, typer.Option("--baseline", help="Reviewed baseline JSON relative to --repo.")]
ConfidenceOption = Annotated[
    float | None, typer.Option("--min-confidence", min=0, max=1, help="Override the gate confidence threshold.")
]
MaxOption = Annotated[int, typer.Option("--max-findings", min=0, help="Allowed qualifying new findings.")]


def _require_extra() -> None:
    if importlib.util.find_spec("httpx") is None:
        raise typer.BadParameter("GitHub publishing needs the github extra: pip install 'docs-drift[github]'.")


def _logging() -> None:
    logger = logging.getLogger("docs_drift.github")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("docs-drift: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def _inside(repo: Path, relative: Path, label: str) -> Path:
    path = (repo / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(repo.resolve()):
        raise typer.BadParameter(f"{label} must be relative to --repo and stay inside it.")
    return path


def _bundle(  # noqa: PLR0913, PLR0917 - mirrors the Action inputs.
    ctx: typer.Context,
    bundle: Path | None,
    base: str | None,
    reviews: Path | None,
    baseline: Path | None,
    min_confidence: float | None,
    max_findings: int,
) -> "ReviewBundle":
    from docs_drift.baseline import load_baseline  # noqa: PLC0415 - keep `--help` fast.
    from docs_drift.github.bundle import build_bundle, load_bundle  # noqa: PLC0415
    from docs_drift.policy import GatePolicy  # noqa: PLC0415
    from docs_drift.review import load_reviews  # noqa: PLC0415

    if bundle is not None:
        return load_bundle(bundle)
    options = ctx.obj
    policy = GatePolicy(
        min_confidence=options.config.min_confidence if min_confidence is None else min_confidence,
        max_findings=max_findings,
    )
    return build_bundle(
        options.repo,
        options.config,
        policy,
        base=base,
        baseline=load_baseline(_inside(options.repo, baseline, "--baseline")) if baseline else None,
        reviews=load_reviews(_inside(options.repo, reviews, "--reviews")) if reviews else None,
    )


def _emit(report: "PublishReport", dry_run: bool) -> None:
    if dry_run:
        typer.echo("docs-drift dry run: nothing was posted.\n")
        for action in report.actions:
            typer.echo(f"=== {action.kind}: {action.detail.get('method', '')} {action.target}".rstrip())
            if action.body:
                typer.echo(action.body)
            typer.echo("")
        if report.skipped:
            typer.echo(f"Skipped: {report.skipped}")
        return
    typer.echo(json.dumps(report.as_dict(), indent=2))


def _owner_repo(slug: str | None) -> tuple[str, str]:
    from docs_drift.github.render import valid_slug  # noqa: PLC0415

    if not slug or not valid_slug(slug):
        raise typer.BadParameter("Pass --github-repository owner/name or set GITHUB_REPOSITORY.")
    owner, name = slug.split("/", 1)
    return owner, name


@github_app.command("pr-review", cls=Command)
def pr_review(  # noqa: PLR0913, PLR0917 - Typer callback options.
    ctx: typer.Context,
    base: Annotated[
        str | None, typer.Option("--base", help="Fetched base revision; defaults to the PR base SHA from the event.")
    ] = None,
    bundle: BundleOption = None,
    github_repository: SlugOption = None,
    pr: Annotated[int | None, typer.Option("--pr", min=1, help="Pull request number; defaults to the event.")] = None,
    head_sha: Annotated[
        str | None, typer.Option("--head-sha", help="PR head commit; defaults to the event, then the scan.")
    ] = None,
    dry_run: DryRunOption = False,
    check_run: Annotated[bool, typer.Option("--check-run", help="Also create a Check Run (checks: write).")] = False,
    inline: Annotated[bool, typer.Option("--inline/--no-inline", help="Post inline review comments.")] = True,
    suggestions: Annotated[
        bool, typer.Option("--suggestions/--no-suggestions", help="Attach suggested changes when certain.")
    ] = True,
    artifact_url: Annotated[str | None, typer.Option("--artifact-url", help="Link to the full report.")] = None,
    reviews: ReviewsOption = None,
    baseline: BaselineOption = None,
    min_confidence: ConfidenceOption = None,
    max_findings: MaxOption = 0,
) -> None:
    """Post or update the sticky PR summary, inline suggestions and an optional check run."""
    _require_extra()
    from docs_drift.github.client import GitHubClient  # noqa: PLC0415 - optional github extra.
    from docs_drift.github.context import actions_context  # noqa: PLC0415
    from docs_drift.github.publish import (  # noqa: PLC0415
        PublishOptions,
        PullRequestRef,
        publish_pull_request,
        resolve_identity,
    )

    _logging()
    env = actions_context()
    number = pr or env.pr_number
    if number is None:
        raise typer.BadParameter("Pass --pr or run on a pull_request event.", param_hint="--pr")
    owner, name = _owner_repo(github_repository or env.repository)
    if env.read_only_fork and not dry_run:
        typer.echo(
            "docs-drift: fork pull request detected; GitHub grants it only a read-only token. Skipping comments. "
            "The drift gate still applies. See docs/github-integration.md for the fork-safe two-workflow option.",
            err=True,
        )
        return
    if not env.token and not dry_run:
        raise typer.BadParameter("Set GITHUB_TOKEN (or GH_TOKEN) with pull-requests: write, or use --dry-run.")
    result = _bundle(ctx, bundle, base or env.base_sha, reviews, baseline, min_confidence, max_findings)
    if not (bundle or base or env.base_sha):
        typer.echo("docs-drift: no base revision; reporting all qualifying drift in the checkout.", err=True)
    ref = PullRequestRef(owner, name, number, head_sha or env.head_sha or result.head_sha)
    options = PublishOptions(
        inline=inline,
        suggestions=suggestions,
        check_run=check_run,
        artifact_url=artifact_url or env.run_url,
        server_url=env.server_url,
        dry_run=dry_run,
    )
    with GitHubClient(token=env.token, api_url=env.api_url) as client:
        if client.authenticated:
            options.bot_login = resolve_identity(client, in_actions=env.in_actions)
        report = publish_pull_request(client, ref, result, options)
    _emit(report, dry_run)


@github_app.command("issue", cls=Command)
def issue(  # noqa: PLR0913, PLR0917 - Typer callback options.
    ctx: typer.Context,
    bundle: BundleOption = None,
    github_repository: SlugOption = None,
    label: Annotated[str, typer.Option("--label", help="Label that identifies the tracking issue.")] = "docs-drift",
    dry_run: DryRunOption = False,
    reviews: ReviewsOption = None,
    baseline: BaselineOption = None,
    min_confidence: ConfidenceOption = None,
    max_findings: MaxOption = 0,
) -> None:
    """Create, update, close or reopen the single tracking issue for scheduled scans."""
    _require_extra()
    from docs_drift.github.client import GitHubClient  # noqa: PLC0415 - optional github extra.
    from docs_drift.github.context import actions_context  # noqa: PLC0415
    from docs_drift.github.publish import (  # noqa: PLC0415
        PublishOptions,
        RepositoryRef,
        publish_issue,
        resolve_identity,
    )

    _logging()
    env = actions_context()
    owner, name = _owner_repo(github_repository or env.repository)
    if not label.strip() or len(label) > 50 or any(c in label for c in ",\n\r"):  # noqa: PLR2004 - GitHub limit.
        raise typer.BadParameter("Use a single label name of at most 50 characters.", param_hint="--label")
    if not env.token and not dry_run:
        raise typer.BadParameter("Set GITHUB_TOKEN (or GH_TOKEN) with issues: write, or use --dry-run.")
    result = _bundle(ctx, bundle, None, reviews, baseline, min_confidence, max_findings)
    options = PublishOptions(issue_label=label, server_url=env.server_url, dry_run=dry_run)
    with GitHubClient(token=env.token, api_url=env.api_url) as client:
        if client.authenticated:
            options.bot_login = resolve_identity(client, in_actions=env.in_actions)
        report = publish_issue(client, RepositoryRef(owner, name), result, options, run_url=env.run_url)
    _emit(report, dry_run)


@github_app.command("app-manifest", cls=Command)
def app_manifest(
    url: Annotated[str, typer.Option("--url", help="Public HTTPS base URL of your deployment.")] = (
        "https://13-211-179-95.sslip.io"
    ),
    org: Annotated[str | None, typer.Option("--org", help="Register under this organization.")] = None,
    serve: Annotated[bool, typer.Option("--serve", help="Run the local two-click manifest flow.")] = False,
    port: Annotated[int, typer.Option("--port", min=1024, max=65535, help="Local port for --serve.")] = 8765,
) -> None:
    """Print the GitHub App manifest for a deployment, or serve the registration flow locally."""
    from docs_drift.github.manifest import build_manifest, serve_manifest_flow  # noqa: PLC0415

    try:
        manifest = build_manifest(url, redirect_url=f"http://127.0.0.1:{port}/callback" if serve else None)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--url") from None
    if not serve:
        typer.echo(json.dumps(manifest, indent=2))
        return
    serve_manifest_flow(manifest, org=org, port=port)


@github_app.command("app-exchange", cls=Command)
def app_exchange(
    code: Annotated[str, typer.Argument(help="The one-time code GitHub appended to the redirect URL.")],
    env_file: Annotated[
        str, typer.Option("--env-file", help="Server environment file the container reads.")
    ] = "/etc/docs-drift.env",
) -> None:
    """Exchange a manifest code for App credentials and print the server env-file lines (nothing is saved)."""
    from docs_drift.github.manifest import exchange_code, secrets_commands  # noqa: PLC0415

    try:
        credentials = exchange_code(code)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(secrets_commands(credentials, env_file))


def register(app: typer.Typer) -> None:
    app.add_typer(github_app, name="github")
