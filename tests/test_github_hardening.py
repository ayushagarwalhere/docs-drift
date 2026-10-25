"""Regression tests for the independent review of the GitHub integration (one test per confirmed defect)."""

import base64
import hashlib
import hmac
import json
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docs_drift.config import Config
from docs_drift.github import render
from docs_drift.github.app_config import GitHubAppConfig, load_private_key
from docs_drift.github.app_server import (
    RESTART_TITLE,
    AppProcessor,
    JobState,
    PullRequestJob,
    WebhookQueue,
    create_router,
)
from docs_drift.github.auth import InstallationTokens
from docs_drift.github.bundle import ReviewBundle, build_bundle
from docs_drift.github.client import GitHubClient, GitHubError, RetryPolicy
from docs_drift.github.diff import changed_lines
from docs_drift.github.publish import PublishOptions, PullRequestRef, publish_pull_request
from docs_drift.policy import GatePolicy
from docs_drift.web.app import create_app
from docs_drift.web.git import JobError
from docs_drift.web.settings import Settings
from tests.fixtures.build_fixtures import build_fixtures
from tests.github_fake import BOT, HUMAN, FakeGitHub, git, pr_files

SECRET = "webhook-secret-for-tests-only"
OURS = "docs-drift[bot]"


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(scope="module")
def pr_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    root = tmp_path_factory.mktemp("hardening")
    build_fixtures(root)
    repo = root / "py-cli"
    base = git(repo, "rev-parse", "HEAD~4").strip()
    readme = (repo / "README.md").read_text(encoding="utf-8")
    (repo / "README.md").write_bytes(
        readme.replace("(default 8000).", "(default 8000) for the HTTP listener.").encode()
    )
    return repo, base, commit(repo, "Clarify serve")


@pytest.fixture(scope="module")
def bundle(pr_repo: tuple[Path, str, str]) -> ReviewBundle:
    repo, base, _ = pr_repo
    return build_bundle(repo, Config(), GatePolicy(), base=base)


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def client_for(fake: FakeGitHub, **kwargs: Any) -> GitHubClient:
    return GitHubClient(token="test-token", transport=fake.transport(), sleep=lambda _: None, **kwargs)


def ref(head: str) -> PullRequestRef:
    return PullRequestRef("acme", "py-cli", 7, head)


# 1. A failed optional Check Run must not abort the review and summary.
def test_forbidden_check_run_is_isolated_from_review_and_summary(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    fake.checks_forbidden = True
    report = publish_pull_request(client_for(fake), ref(head), bundle, PublishOptions(check_run=True))
    assert report.skipped is None
    assert fake.reviews and fake.issue_comments[7]
    kinds = [a.kind for a in report.actions]
    assert kinds[-1] == "check-run-skipped" and "create-summary" in kinds
    assert "personal access tokens" in (report.actions[-1].body or "")


# 2. Unexpected scan failures conclude the check run instead of leaving it in progress.
def test_unexpected_scan_failure_concludes_the_check_run(private_key: str, tmp_path: Path) -> None:
    fake = FakeGitHub()
    config = GitHubAppConfig(app_id="1", private_key=private_key, webhook_secret=SECRET)
    tokens = InstallationTokens("1", private_key, GitHubClient(transport=fake.transport()))
    processor = AppProcessor(config, tokens, tmp_path, transport=fake.transport())

    def explode(job: PullRequestJob, head: str, base_sha: str) -> ReviewBundle:
        raise RuntimeError("unexpected")

    processor.scan = explode  # type: ignore[method-assign]
    queue = WebhookQueue(processor, workers=1)
    queue.submit(PullRequestJob(1, "acme/py-cli", 7, "a" * 40, "b" * 40))
    assert queue.wait_idle(20)
    queue.close()
    (run,) = fake.check_runs
    assert run["status"] == "completed" and run["conclusion"] == "neutral"
    assert run["output"]["title"] == "docs-drift could not finish" and queue.failed == 1
    assert queue.tracked == 0  # 16. Finished PRs are forgotten.


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def deliver(client: TestClient, body: bytes, delivery: str, event: str = "pull_request") -> Any:
    headers = {"X-Hub-Signature-256": sign(body), "X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    return client.post("/github/webhook", content=body, headers=headers)


def event(number: int) -> bytes:
    pr = {"number": number, "state": "open", "draft": False, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
    payload = {"action": "opened", "installation": {"id": 9}, "repository": {"full_name": "acme/x"}, "pull_request": pr}
    return json.dumps(payload).encode()


# 3. Rejected deliveries (503 queue full, 400 bad JSON) can be redelivered with the same ID.
def test_redelivery_after_queue_full_or_bad_payload_is_accepted(private_key: str, tmp_path: Path) -> None:
    release = threading.Event()
    seen: list[int] = []

    def processor(job: PullRequestJob, state: Callable[[], bool]) -> None:
        release.wait(10)
        seen.append(job.number)

    config = GitHubAppConfig(
        app_id="1", private_key=private_key, webhook_secret=SECRET, max_pending=1, max_concurrent=1
    )
    router = create_router(config, work_dir=tmp_path / "work", processor=processor)
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False), github_router=router)
    with TestClient(app) as client:
        assert deliver(client, b"{not json", "bad-1").status_code == 400
        assert deliver(client, event(1), "bad-1").status_code == 202  # Same ID, now valid: accepted.
        for _ in range(100):  # Wait until job 1 is running so the backlog holds exactly one.
            if router.queue.depth == 1 and not router.queue._pending:  # type: ignore[attr-defined]
                break
            threading.Event().wait(0.05)
        assert deliver(client, event(2), "d-2").status_code == 202
        assert deliver(client, event(3), "d-3").status_code == 503
        release.set()
        assert router.queue.wait_idle(10)  # type: ignore[attr-defined]
        assert deliver(client, event(3), "d-3").status_code == 202  # GitHub's redelivery reuses the ID.
        assert deliver(client, event(3), "d-3").status_code == 409
        assert router.queue.wait_idle(10)  # type: ignore[attr-defined]
    assert sorted(seen) == [1, 2, 3]


# 4. Outdated or moved comments are not treated as current.
def test_outdated_inline_comment_is_reposted(pr_repo: tuple[Path, str, str], bundle: ReviewBundle) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    client = client_for(fake)
    publish_pull_request(client, ref(head), bundle, PublishOptions(bot_login=OURS))
    first = len(fake.review_comments)
    fake.review_comments[0]["line"] = None  # GitHub nulls `line` when the commented line changed.
    report = publish_pull_request(client, ref(head), bundle, PublishOptions(bot_login=OURS))
    assert len(fake.reviews) == 2 and len(fake.review_comments) == first + 1
    assert "resolve-comment" in [a.kind for a in report.actions]
    assert "state=resolved" in fake.review_comments[0]["body"]


# 5. Secondary rate limits without Retry-After wait a minute; waits beyond the bound fail fast.
def test_secondary_rate_limit_waits_a_minute_and_long_waits_fail_fast() -> None:
    fake = FakeGitHub()
    fake.forced = [httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"})]
    slept: list[float] = []
    client = GitHubClient(token="t", transport=fake.transport(), sleep=slept.append)
    assert client.get("/repos/acme/py-cli/pulls/1/files") == []
    assert slept == [60.0]
    fake.forced = [
        httpx.Response(429, json={"message": "x"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"})
    ]
    slow = GitHubClient(token="t", transport=fake.transport(), sleep=slept.append, clock=lambda: 1000.0)
    with pytest.raises(GitHubError, match="retry after 4000s"):
        slow.get("/repos/acme/py-cli/pulls/1/files")
    assert slept == [60.0]


# 6. Non-idempotent POSTs are not repeated after a 5xx or a lost response.
def test_post_is_not_retried_after_server_errors_but_is_after_connect_errors() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1 and request.method == "POST" and request.url.path.endswith("/connect"):
            raise httpx.ConnectError("refused", request=request)
        if request.url.path.endswith("/read"):
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(502 if request.url.path.endswith("/fail") else 201, json={"ok": True})

    client = GitHubClient(
        token="t", transport=httpx.MockTransport(handler), sleep=lambda _: None, retry=RetryPolicy(attempts=3, jitter=0)
    )
    with pytest.raises(GitHubError) as raised:
        client.post("/repos/a/b/issues/1/fail", {})
    assert raised.value.status == 502 and calls == ["POST"]
    calls.clear()
    with pytest.raises(GitHubError):
        client.post("/repos/a/b/issues/1/read", {})
    assert calls == ["POST"]
    calls.clear()
    assert client.post("/repos/a/b/issues/1/connect", {}) == {"ok": True} and calls == ["POST", "POST"]
    calls.clear()
    with pytest.raises(GitHubError):
        client.get("/repos/a/b/fail")
    assert calls == ["GET"] * 3


# 7. Paths with spaces/non-ASCII and added lines that look like headers.
def test_changed_lines_handles_unusual_paths_and_header_like_content(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "docs dir").mkdir()
    (repo / "docs dir" / "naïve guide.md").write_text("one\ntwo\n", encoding="utf-8")
    base = commit(repo, "base")
    (repo / "docs dir" / "naïve guide.md").write_text("one\n++ not a header\ntwo\n+++ b/fake.md\n", encoding="utf-8")
    commit(repo, "edit")
    assert changed_lines(repo, base) == {"docs dir/naïve guide.md": {2, 4}}


# 8. Lines changed only on the base branch are not attributed to the PR (three-dot diff).
def test_changed_lines_uses_the_merge_base_when_the_base_moves(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    commit(repo, "start")
    git(repo, "checkout", "-qb", "feature")
    text = (repo / "README.md").read_text(encoding="utf-8").replace("line 2\n", "line 2 (PR)\n")
    (repo / "README.md").write_text(text, encoding="utf-8")
    head = commit(repo, "pr edit")
    git(repo, "checkout", "-q", "main")
    text = (repo / "README.md").read_text(encoding="utf-8").replace("line 9\n", "line 9 (main)\n")
    (repo / "README.md").write_text(text, encoding="utf-8")
    moved_base = commit(repo, "base moved")
    git(repo, "checkout", "-q", "feature")
    assert changed_lines(repo, moved_base, head) == {"README.md": {2}}
    assert {f["filename"] for f in pr_files(repo, moved_base, head)} == {"README.md"}
    assert "line 9" not in pr_files(repo, moved_base, head)[0]["patch"].split("@@")[2]


# 9. A scan in a subdirectory publishes repository-root paths.
def test_subdirectory_scan_prefixes_github_paths(pr_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    source, _, _ = pr_repo
    mono = tmp_path / "mono"
    (mono / "pkg").mkdir(parents=True)
    git(mono, "init", "-q")
    for name in ("README.md", "cli.py"):
        shutil.copy(source / name, mono / "pkg" / name)
    commit(mono, "monorepo")
    result = build_bundle(mono / "pkg", Config(), GatePolicy(min_confidence=0))
    assert result.path_prefix == "pkg/" and result.findings
    assert all(f.claim.doc_path.startswith("pkg/README.md") for f in result.findings)
    body = render.summary_comment(result, slug="acme/mono")
    assert "/blob/" in body and "/pkg/README.md#L" in body
    assert all(a["path"] == "pkg/README.md" for a in render.annotations(result))


# 10. Shutdown is not supersession: queued jobs are reported and in-flight checks concluded.
def test_shutdown_reports_dropped_jobs_and_is_distinct_from_supersession() -> None:
    started, release = threading.Event(), threading.Event()
    states: list[tuple[bool, bool]] = []

    class Processor:
        dropped: list[PullRequestJob] | None = None

        def __call__(self, job: PullRequestJob, state: JobState) -> None:
            started.set()
            release.wait(10)
            states.append((state(), state.stopping))

        def on_shutdown(self, dropped: list[PullRequestJob]) -> None:
            self.dropped = dropped
            release.set()

    processor = Processor()
    queue = WebhookQueue(processor, workers=1)
    queue.submit(PullRequestJob(1, "a/b", 1, "x"))
    assert started.wait(10)
    queue.submit(PullRequestJob(1, "a/b", 2, "y"))
    queue.close()
    assert [job.number for job in processor.dropped or []] == [2] and queue.dropped == 1
    assert states == [(True, True)]  # Still the newest push, but the service is stopping.


def test_app_on_shutdown_concludes_active_checks_and_marks_queued_pushes(private_key: str, tmp_path: Path) -> None:
    fake = FakeGitHub()
    config = GitHubAppConfig(app_id="1", private_key=private_key, webhook_secret=SECRET)
    tokens = InstallationTokens("1", private_key, GitHubClient(transport=fake.transport()))
    processor = AppProcessor(config, tokens, tmp_path, transport=fake.transport())
    fake.check_runs.append({"id": 5, "status": "in_progress"})
    processor._active[(1, "acme/x", 3)] = ("/repos/acme/x", 5)
    processor.on_shutdown([PullRequestJob(1, "acme/x", 4, "c" * 40, "d" * 40)])
    assert fake.check_runs[0]["status"] == "completed" and fake.check_runs[0]["output"]["title"] == RESTART_TITLE
    assert fake.check_runs[1]["head_sha"] == "c" * 40 and fake.check_runs[1]["conclusion"] == "neutral"
    assert "/docs-drift recheck" in fake.check_runs[1]["output"]["summary"]


# 11. Markers on comments by other authors are neither trusted nor edited.
def test_markers_from_other_authors_are_ignored(pr_repo: tuple[Path, str, str], bundle: ReviewBundle) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    planned = publish_pull_request(
        client_for(FakeGitHub(pr_files(repo, base, head))), ref(head), bundle, PublishOptions(dry_run=True)
    )
    review_text = next(a.body for a in planned.actions if a.kind == "create-review") or ""
    first_marker = review_text.split("\n", 1)[0]
    # A participant pastes our exact marker, hoping to suppress the finding and get their comment edited.
    fake.review_comments.append({"id": 1, "user": HUMAN, "path": "README.md", "line": 4, "body": first_marker})
    fake.issue_comments[7] = [{"id": 2, "user": HUMAN, "body": render.SUMMARY_MARKER}]
    publish_pull_request(client_for(fake), ref(head), bundle, PublishOptions(bot_login=OURS))
    assert any(c["user"] == BOT and c["line"] == 4 for c in fake.review_comments)
    assert not any(r.method == "PATCH" and r.url.path.endswith(("/comments/1", "/comments/2")) for r in fake.requests)
    assert [c["user"] for c in fake.issue_comments[7]] == [HUMAN, BOT]


# 12. Line-wrapped base64 private keys are accepted.
def test_wrapped_base64_private_key_is_accepted(private_key: str) -> None:
    encoded = base64.encodebytes(private_key.encode()).decode()  # 76-column lines.
    assert "\n" in encoded.strip()
    assert load_private_key(encoded) == private_key.strip() + "\n"


# 13. The inline cap is applied after dedupe.
def test_inline_cap_never_retires_valid_comments(pr_repo: tuple[Path, str, str], bundle: ReviewBundle) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    client = client_for(fake)
    publish_pull_request(client, ref(head), bundle, PublishOptions(bot_login=OURS, max_inline=1))
    assert len(fake.review_comments) == 1
    rerun = publish_pull_request(client, ref(head), bundle, PublishOptions(bot_login=OURS, max_inline=1))
    assert "resolve-comment" not in [a.kind for a in rerun.actions]
    assert len(fake.review_comments) == 1 and "state=resolved" not in fake.review_comments[0]["body"]


# 14. Truncation reserves room for, and matches, its own closers.
def test_truncate_closes_long_fences_and_details_within_the_limit() -> None:
    body = render.SUMMARY_MARKER + "\n<details>\n\n````suggestion\n" + ("x" * 60 + "\n") * 2000
    cut = render.truncate(body, 10_000)
    assert len(cut) <= 10_000 and cut.startswith(render.SUMMARY_MARKER)
    tail = cut[: cut.index("\n\n> [!NOTE]")]
    assert tail.endswith("\n````\n</details>")


# 15. Workspaces left by a crash are removed at startup.
def test_stale_app_workspaces_are_swept_at_startup(private_key: str, tmp_path: Path) -> None:
    work = tmp_path / "github-work"
    (work / ("a" * 32)).mkdir(parents=True)
    (work / ("a" * 32) / "file.md").write_text("x", encoding="utf-8")
    (work / "keep-me").mkdir()
    config = GitHubAppConfig(app_id="1", private_key=private_key, webhook_secret=SECRET)
    create_router(config, work_dir=work, processor=lambda job, state: None)
    assert sorted(p.name for p in work.iterdir()) == ["keep-me"]


def test_scan_job_errors_still_conclude_with_the_failure(private_key: str, tmp_path: Path) -> None:
    fake = FakeGitHub()
    config = GitHubAppConfig(app_id="1", private_key=private_key, webhook_secret=SECRET)
    tokens = InstallationTokens("1", private_key, GitHubClient(transport=fake.transport()))
    processor = AppProcessor(config, tokens, tmp_path, transport=fake.transport())

    def too_large(job: PullRequestJob, head: str, base_sha: str) -> ReviewBundle:
        raise JobError("too_large", "Too large.", "Scan locally.")

    processor.scan = too_large  # type: ignore[method-assign]
    queue = WebhookQueue(processor, workers=1)
    queue.submit(PullRequestJob(1, "acme/py-cli", 7, "a" * 40, "b" * 40))
    assert queue.wait_idle(20)
    queue.close()
    assert fake.check_runs[0]["output"]["summary"] == "Too large. Scan locally." and queue.failed == 0
