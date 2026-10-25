"""Publish a ReviewBundle to a pull request (sticky summary, inline suggestions, check run) or an issue.

All write calls go through ``_Writer`` so dry runs record exactly what would be
sent, and a read-only token (fork PRs, Dependabot) stops publishing with a clear
reason instead of failing the job: the drift gate is enforced separately.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from docs_drift.github import render
from docs_drift.github.bundle import ReviewBundle
from docs_drift.github.client import GitHubClient, GitHubError
from docs_drift.github.diff import diff_map
from docs_drift.models import Finding

LOG = logging.getLogger("docs_drift.github")
CHECK_ANNOTATION_BATCH = 50
READ_ONLY_HINT = (
    "The token cannot write to this repository (fork pull requests and Dependabot runs receive a read-only "
    "GITHUB_TOKEN). Skipping GitHub comments; the drift gate and annotations still apply."
)
CONCLUSIONS = {"passed": "success", "failed": "failure", "report_only": "neutral"}
CHECK_RUN_HINT = (
    "Could not create the docs-drift Check Run. Check Runs need a GitHub App token: the Actions GITHUB_TOKEN "
    "with `checks: write` works, personal access tokens never do. The review and summary were still published."
)


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int
    head_sha: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def base(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"


@dataclass
class PublishOptions:
    inline: bool = True
    suggestions: bool = True
    check_run: bool = False
    check_name: str = "docs-drift"
    artifact_url: str | None = None
    server_url: str = "https://github.com"
    dry_run: bool = False
    max_inline: int = 30
    issue_label: str = "docs-drift"
    # The login whose comments we own (App ``slug[bot]``, ``github-actions[bot]`` or the PAT user).
    # Markers on anyone else's comments are ignored and never edited. None: trust Bot accounts only.
    bot_login: str | None = None


@dataclass
class PublishAction:
    kind: str
    target: str
    body: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishReport:
    actions: list[PublishAction] = field(default_factory=list)
    skipped: str | None = None
    inline_ids: set[str] = field(default_factory=set)
    check_run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "actions": [
                {"kind": a.kind, "target": a.target, **({"detail": a.detail} if a.detail else {})} for a in self.actions
            ],
        }


class ReadOnlyToken(Exception):
    """Raised internally to stop further writes after the first permission failure."""


@dataclass
class _Writer:
    client: GitHubClient
    report: PublishReport
    dry_run: bool

    def call(
        self, kind: str, method: str, path: str, body: dict[str, Any] | None = None, *, text: str | None = None
    ) -> Any:
        self.report.actions.append(PublishAction(kind, path, text, {"method": method}))
        if self.dry_run:
            return {}
        try:
            if method == "POST":
                return self.client.post(path, body or {})
            if method == "PATCH":
                return self.client.patch(path, body or {})
            self.client.delete(path)
            return {}
        except GitHubError as exc:
            if exc.forbidden:
                raise ReadOnlyToken from exc
            raise


def _authored_by_bot(item: dict[str, Any]) -> bool:
    user = item.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def _ours(item: dict[str, Any], login: str | None) -> bool:
    """Only our own comments are trusted for dedupe and edited; a pasted marker is just text."""
    if login is not None:
        return str((item.get("user") or {}).get("login", "")).lower() == login.lower()
    return _authored_by_bot(item)


def resolve_identity(client: GitHubClient, in_actions: bool) -> str | None:
    """The login a token posts as: ``GET /user`` for user tokens, ``github-actions[bot]`` for GITHUB_TOKEN."""
    try:
        user = client.get("/user")
        if isinstance(user, dict) and user.get("login"):
            return str(user["login"])
    except GitHubError:
        pass
    return "github-actions[bot]" if in_actions else None


def _find_sticky(comments: list[dict[str, Any]], login: str | None) -> dict[str, Any] | None:
    return next((c for c in comments if render.SUMMARY_MARKER in str(c.get("body") or "") and _ours(c, login)), None)


def _groups(findings: list[Finding]) -> dict[tuple[str, int], list[Finding]]:
    grouped: dict[tuple[str, int], list[Finding]] = {}
    for finding in sorted(findings, key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id)):
        grouped.setdefault((finding.claim.doc_path, finding.claim.line), []).append(finding)
    return grouped


def plan_inline(
    bundle: ReviewBundle, commentable: dict[str, set[int]] | None, options: PublishOptions
) -> tuple[list[render.InlineComment], set[str]]:
    """Return every inline comment for findings on diff lines (uncapped) plus every current group key.

    ``commentable=None`` means the diff is unknown (offline dry run): all lines qualify.
    """
    suggestions = {(s.path, s.line): s for s in bundle.suggestions} if options.suggestions else {}
    comments, keys = [], set()
    for (path, line), findings in _groups([*bundle.findings, *bundle.touched]).items():
        suggestion = suggestions.get((path, line))
        if suggestion and not set(suggestion.claim_ids) <= {f.claim.claim_id for f in findings}:
            suggestion = None
        comment = render.inline_comment(findings, suggestion)
        keys.add(comment.key)
        if commentable is None or line in commentable.get(path, set()):
            comments.append(comment)
    return comments, keys


def _existing_inline(
    items: list[dict[str, Any]], login: str | None
) -> dict[str, tuple[int, str, str | None, int | None]]:
    """``key -> (id, digest, path, line)`` for our unresolved comments; ``line`` is None once outdated."""
    seen: dict[str, tuple[int, str, str | None, int | None]] = {}
    for item in items:
        marker = render.parse_inline_marker(str(item.get("body") or ""))
        if marker and marker[2] != "resolved" and _ours(item, login):
            line = item.get("line")
            seen.setdefault(
                marker[0], (int(item["id"]), marker[1], item.get("path"), line if isinstance(line, int) else None)
            )
    return seen


def publish_pull_request(  # noqa: PLR0912, PLR0915 - one ordered, fail-soft publishing transaction.
    client: GitHubClient, pr: PullRequestRef, bundle: ReviewBundle, options: PublishOptions
) -> PublishReport:
    report = PublishReport()
    writer = _Writer(client, report, options.dry_run)
    offline = options.dry_run and not client.authenticated
    login = options.bot_login
    if bundle.head_sha != pr.head_sha:
        LOG.warning("Scanned %s but the PR head is %s; inline comments may fall back.", bundle.head_sha, pr.head_sha)
    try:
        commentable = None if offline else diff_map(client.paginate(f"{pr.base}/pulls/{pr.number}/files"))
        existing_inline = [] if offline else list(client.paginate(f"{pr.base}/pulls/{pr.number}/comments"))
        issue_comments = [] if offline else list(client.paginate(f"{pr.base}/issues/{pr.number}/comments"))
    except GitHubError as exc:
        if exc.forbidden:
            report.skipped = "The token cannot read this pull request; check the workflow permissions."
            return report
        raise
    try:
        planned, keys = plan_inline(bundle, commentable, options) if options.inline else ([], set())
        seen = _existing_inline(existing_inline, login)

        def live(c: render.InlineComment) -> bool:
            # Current only if identical text is still attached to the same line (not outdated or moved).
            found = seen.get(c.key)
            return found is not None and found[1] == c.digest and found[2] == c.path and found[3] == c.line

        posted: list[render.InlineComment] = [c for c in planned if live(c)]
        candidates = [c for c in planned if not live(c)]
        # Cap only new comments, after dedupe, so a reordering never retires a still-valid comment.
        fresh = candidates[: max(0, options.max_inline - len(posted))]
        if fresh:
            try:
                writer.call(
                    "create-review",
                    "POST",
                    f"{pr.base}/pulls/{pr.number}/reviews",
                    {
                        "commit_id": pr.head_sha,
                        "event": "COMMENT",
                        "body": render.review_body(len(fresh)),
                        "comments": [c.payload() for c in fresh],
                    },
                    text="\n\n---\n\n".join(c.marked_body for c in fresh),
                )
                posted.extend(fresh)
            except GitHubError as exc:
                if exc.status != 422:  # noqa: PLR2004 - GitHub validation failure.
                    raise
                LOG.warning("GitHub rejected inline comments (%s); listing them in the summary.", exc.api_message)
                report.actions.append(PublishAction("inline-fallback", f"{pr.base}/pulls/{pr.number}/reviews"))
        posted_keys = {c.key for c in posted}
        # Comments we wanted to (re)post but could not (422 or the cap) keep their earlier version.
        unposted = {c.key for c in candidates} - posted_keys
        for key, (comment_id, _digest, _path, _line) in seen.items():
            if key in posted_keys and any(c.key == key and live(c) for c in posted):
                continue
            if key in unposted:
                continue
            try:
                writer.call(
                    "resolve-comment",
                    "PATCH",
                    f"{pr.base}/pulls/comments/{comment_id}",
                    {"body": render.resolved_inline(key, pr.head_sha, superseded=key in keys)},
                )
            except ReadOnlyToken:
                LOG.info("Could not mark review comment %s as resolved.", comment_id)
        report.inline_ids = {claim for c in posted for claim in c.claim_ids}
        body = render.summary_comment(
            bundle,
            slug=pr.slug,
            server_url=options.server_url,
            head_sha=pr.head_sha,
            inline_ids=report.inline_ids,
            artifact_url=options.artifact_url,
        )
        sticky = _find_sticky(issue_comments, login)
        if sticky is None and not bundle.clean:
            writer.call("create-summary", "POST", f"{pr.base}/issues/{pr.number}/comments", {"body": body}, text=body)
        elif sticky is not None and str(sticky.get("body") or "") != body:
            writer.call(
                "update-summary", "PATCH", f"{pr.base}/issues/comments/{sticky['id']}", {"body": body}, text=body
            )
        else:
            report.actions.append(PublishAction("summary-unchanged", f"{pr.base}/issues/{pr.number}/comments"))
    except ReadOnlyToken:
        report.skipped = READ_ONLY_HINT
        LOG.warning(READ_ONLY_HINT)
        return report
    if options.check_run:
        # Optional and isolated: a token that cannot write checks must not cost the review or summary.
        try:
            _publish_check_run(writer, client, pr, bundle, options)
        except (ReadOnlyToken, GitHubError) as exc:
            cause = exc.__cause__ if isinstance(exc, ReadOnlyToken) else exc
            LOG.warning("%s (%s)", CHECK_RUN_HINT, getattr(cause, "status", "error"))
            report.actions.append(PublishAction("check-run-skipped", f"{pr.base}/check-runs", CHECK_RUN_HINT))
    return report


def _publish_check_run(
    writer: _Writer, client: GitHubClient, pr: PullRequestRef, bundle: ReviewBundle, options: PublishOptions
) -> None:
    rows = render.annotations(bundle)
    output: dict[str, Any] = {**render.check_run_output(bundle), "annotations": rows[:CHECK_ANNOTATION_BATCH]}
    created = writer.call(
        "create-check-run",
        "POST",
        f"{pr.base}/check-runs",
        {
            "name": options.check_name,
            "head_sha": pr.head_sha,
            "status": "completed",
            "conclusion": CONCLUSIONS[bundle.gate.status],
            "output": output,
            **({"details_url": options.artifact_url} if options.artifact_url else {}),
        },
    )
    check_id = created.get("id") if isinstance(created, dict) else None
    writer.report.check_run_id = int(check_id) if check_id else None
    for start in range(CHECK_ANNOTATION_BATCH, len(rows), CHECK_ANNOTATION_BATCH):
        writer.call(
            "update-check-run",
            "PATCH",
            f"{pr.base}/check-runs/{check_id or 'pending'}",
            {
                "output": {
                    **render.check_run_output(bundle),
                    "annotations": rows[start : start + CHECK_ANNOTATION_BATCH],
                }
            },
        )


def conclude_check_run(  # noqa: PLR0913 - check run identity plus outcome.
    client: GitHubClient,
    base: str,
    check_id: int,
    bundle: ReviewBundle | None,
    failure: str | None = None,
    *,
    title: str = "docs-drift could not scan this pull request",
) -> None:
    """Complete an App-created in-progress check run (with batched annotations)."""
    if bundle is None:
        client.patch(
            f"{base}/check-runs/{check_id}",
            {
                "status": "completed",
                "conclusion": "neutral",
                "output": {"title": title, "summary": failure or "Error."},
            },
        )
        return
    rows = render.annotations(bundle)
    for index, start in enumerate(range(0, max(len(rows), 1), CHECK_ANNOTATION_BATCH)):
        body: dict[str, Any] = {
            "output": {**render.check_run_output(bundle), "annotations": rows[start : start + CHECK_ANNOTATION_BATCH]}
        }
        if index == 0:
            body.update(status="completed", conclusion=CONCLUSIONS[bundle.gate.status])
        client.patch(f"{base}/check-runs/{check_id}", body)


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def base(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"


def _find_issue(
    client: GitHubClient, repo: RepositoryRef, label: str, login: str | None = None
) -> dict[str, Any] | None:
    issues = [
        item
        for item in client.paginate(f"{repo.base}/issues", state="all", labels=label, sort="updated", direction="desc")
        if "pull_request" not in item and render.ISSUE_MARKER in str(item.get("body") or "") and _ours(item, login)
    ]
    return next((i for i in issues if i.get("state") == "open"), issues[0] if issues else None)


def _ensure_label(writer: _Writer, client: GitHubClient, repo: RepositoryRef, label: str) -> None:
    if writer.dry_run:
        return
    try:
        client.get(f"{repo.base}/labels/{label}")
    except GitHubError as exc:
        if exc.status != 404:  # noqa: PLR2004 - HTTP status.
            raise
        try:
            writer.call(
                "create-label",
                "POST",
                f"{repo.base}/labels",
                {"name": label, "color": "c5def5", "description": "Documentation drift found by docs-drift"},
            )
        except GitHubError as error:
            if error.status != 422:  # noqa: PLR2004 - label already exists (race).
                raise


def publish_issue(  # noqa: PLR0913 - keyword-only rendering context.
    client: GitHubClient,
    repo: RepositoryRef,
    bundle: ReviewBundle,
    options: PublishOptions,
    *,
    run_url: str | None = None,
    find: Callable[[GitHubClient, RepositoryRef, str, str | None], dict[str, Any] | None] = _find_issue,
) -> PublishReport:
    """Keep exactly one tracking issue: create, update in place, close at zero, reopen on return."""
    report = PublishReport()
    writer = _Writer(client, report, options.dry_run)
    offline = options.dry_run and not client.authenticated
    label = options.issue_label
    try:
        issue = None if offline else find(client, repo, label, options.bot_login)
        title = render.issue_title(len(bundle.findings))
        body = render.issue_body(bundle, slug=repo.slug, server_url=options.server_url, run_url=run_url)
        if bundle.clean:
            if issue and issue.get("state") == "open":
                number = issue["number"]
                writer.call(
                    "comment-issue",
                    "POST",
                    f"{repo.base}/issues/{number}/comments",
                    {"body": render.resolved_issue_comment(bundle.head_sha)},
                )
                writer.call(
                    "close-issue",
                    "PATCH",
                    f"{repo.base}/issues/{number}",
                    {"state": "closed", "state_reason": "completed"},
                )
            else:
                report.actions.append(PublishAction("issue-unchanged", f"{repo.base}/issues"))
            return report
        if issue is None:
            _ensure_label(writer, client, repo, label)
            writer.call(
                "create-issue",
                "POST",
                f"{repo.base}/issues",
                {"title": title, "body": body, "labels": [label]},
                text=body,
            )
            return report
        number = issue["number"]
        update: dict[str, Any] = {}
        if issue.get("state") != "open":
            update["state"] = "open"
            writer.call(
                "comment-issue",
                "POST",
                f"{repo.base}/issues/{number}/comments",
                {"body": render.reopened_issue_comment(bundle.head_sha, len(bundle.findings))},
            )
        if issue.get("body") != body or issue.get("title") != title:
            update.update(title=title, body=body)
        if update:
            writer.call(
                "reopen-issue" if "state" in update else "update-issue",
                "PATCH",
                f"{repo.base}/issues/{number}",
                update,
                text=body,
            )
        else:
            report.actions.append(PublishAction("issue-unchanged", f"{repo.base}/issues/{number}"))
    except ReadOnlyToken:
        report.skipped = READ_ONLY_HINT
        LOG.warning(READ_ONLY_HINT)
    except GitHubError as exc:
        if not exc.forbidden:
            raise
        report.skipped = READ_ONLY_HINT
    return report
