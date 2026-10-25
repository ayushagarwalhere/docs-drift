"""GitHub App: webhook authentication, replay defence, App auth, queue coalescing and an end-to-end PR job."""

import base64
import hashlib
import hmac
import io
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docs_drift.github import scan_worker
from docs_drift.github.app_config import AppConfigError, GitHubAppConfig, load_private_key
from docs_drift.github.app_server import (
    AppProcessor,
    PullRequestJob,
    QueueFull,
    ReplayCache,
    WebhookQueue,
    create_router,
    job_from_event,
    verify_signature,
)
from docs_drift.github.auth import InstallationTokens, app_jwt
from docs_drift.github.bundle import BUNDLE_NAME, ReviewBundle
from docs_drift.github.client import GitHubClient
from docs_drift.github.render import SUMMARY_MARKER
from docs_drift.web.app import create_app
from docs_drift.web.settings import Settings
from tests.fixtures.build_fixtures import build_fixtures
from tests.github_fake import FakeGitHub, git, pr_files

SECRET = "webhook-secret-for-tests-only"


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def config(private_key: str) -> GitHubAppConfig:
    return GitHubAppConfig(app_id="12345", private_key=private_key, webhook_secret=SECRET, max_concurrent=1)


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_event(number: int = 7, head: str = "a" * 40, base: str = "b" * 40, **pr: Any) -> dict[str, Any]:
    return {
        "action": "synchronize",
        "installation": {"id": 99},
        "repository": {"full_name": "acme/py-cli"},
        "pull_request": {
            "number": number,
            "state": "open",
            "draft": False,
            "head": {"sha": head},
            "base": {"sha": base},
            **pr,
        },
    }


def deliver(
    client: TestClient, payload: dict[str, Any], *, event: str = "pull_request", delivery: str = "d-1", **headers: str
) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": headers.get("signature", sign(body)),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )


# --- authentication -----------------------------------------------------------------------------


def test_signature_verification_valid_invalid_missing() -> None:
    body = b'{"zen": "Keep it logically awesome."}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "other-secret-value"))
    assert not verify_signature(SECRET, body + b" ", sign(body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=" + sign(body)[7:])


def test_webhook_route_rejects_bad_signatures_and_replays(config: GitHubAppConfig, tmp_path: Path) -> None:
    seen: list[PullRequestJob] = []
    router = create_router(config, work_dir=tmp_path, processor=lambda job, current: seen.append(job))
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False), github_router=router)
    with TestClient(app) as client:
        assert deliver(client, pr_event(), signature="sha256=" + "0" * 64).status_code == 401
        missing = client.post(
            "/github/webhook", content=b"{}", headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "x"}
        )
        assert missing.status_code == 401
        first = deliver(client, pr_event(), delivery="abc-1")
        assert first.status_code == 202 and first.json() == {"status": "queued"}
        assert first.headers["cache-control"] == "no-store"
        assert deliver(client, pr_event(), delivery="abc-1").status_code == 409
        assert deliver(client, {"zen": "x"}, event="ping", delivery="abc-2").json() == {"status": "pong"}
        assert router.queue.wait_idle(10)  # type: ignore[attr-defined]
    assert [job.number for job in seen] == [7]


def test_webhook_accepts_large_signed_payloads_while_public_api_keeps_tight_limit(
    config: GitHubAppConfig, tmp_path: Path
) -> None:
    router = create_router(config, work_dir=tmp_path, processor=lambda job, current: None)
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False), github_router=router)
    with TestClient(app) as client:
        response = deliver(client, pr_event(body="x" * 20_000), delivery="big-1")
        assert response.status_code == 202
        assert client.post("/api/scans", content=b"x" * 5000).status_code == 413


def test_app_mounts_only_when_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key: str) -> None:
    for name in ("DOCS_DRIFT_GITHUB_APP_ID", "DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY", "DOCS_DRIFT_GITHUB_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path / "a", live_enabled=False))) as client:
        assert client.post("/github/webhook", content=b"{}").status_code in {404, 405}
    monkeypatch.setenv("DOCS_DRIFT_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY", base64.b64encode(private_key.encode()).decode())
    monkeypatch.setenv("DOCS_DRIFT_GITHUB_WEBHOOK_SECRET", SECRET)
    with TestClient(create_app(Settings(data_dir=tmp_path / "b", live_enabled=False))) as client:
        assert deliver(client, {"zen": "x"}, event="ping").json() == {"status": "pong"}
        assert client.get("/healthz").status_code == 200


def test_app_config_from_env_validates_without_echoing_secrets(private_key: str) -> None:
    assert GitHubAppConfig.from_env({}) is None
    with pytest.raises(AppConfigError, match="partially configured") as raised:
        GitHubAppConfig.from_env({"DOCS_DRIFT_GITHUB_WEBHOOK_SECRET": "s" * 32})
    assert "s" * 32 not in str(raised.value)
    env = {
        "DOCS_DRIFT_GITHUB_APP_ID": "12345",
        "DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY": private_key.replace("\n", "\\n"),
        "DOCS_DRIFT_GITHUB_WEBHOOK_SECRET": SECRET,
    }
    loaded = GitHubAppConfig.from_env(env)
    assert loaded is not None and loaded.private_key == private_key.strip() + "\n"
    assert private_key[40:80] not in repr(loaded) and SECRET not in repr(loaded)
    for broken in ({"DOCS_DRIFT_GITHUB_APP_ID": "abc"}, {"DOCS_DRIFT_GITHUB_WEBHOOK_SECRET": "short"}):
        with pytest.raises(AppConfigError):
            GitHubAppConfig.from_env({**env, **broken})
    with pytest.raises(AppConfigError):
        load_private_key("not a key")
    assert load_private_key(base64.b64encode(private_key.encode()).decode()) == private_key.strip() + "\n"


def test_app_jwt_claims_are_backdated_short_lived_and_rs256(private_key: str) -> None:
    now = 1_800_000_000
    token = app_jwt("12345", private_key, now)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    key = serialization.load_pem_private_key(private_key.encode(), None)
    assert isinstance(key, rsa.RSAPrivateKey)
    public = key.public_key()
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == now - 60
    assert claims["exp"] - now <= 600 and claims["exp"] > now


def test_installation_tokens_are_cached_and_refreshed_before_expiry(private_key: str) -> None:
    fake = FakeGitHub()
    fake.token_expiry = "2030-01-01T01:00:00Z"
    clock = [1893456000.0]  # 2030-01-01T00:00:00Z
    tokens = InstallationTokens("12345", private_key, GitHubClient(transport=fake.transport()), clock=lambda: clock[0])
    assert tokens.token(99) == "inst-token-1"
    assert tokens.token(99) == "inst-token-1" and fake.tokens_issued == 1
    clock[0] += 3600 - 200  # Inside the five-minute refresh margin.
    assert tokens.token(99) == "inst-token-2"
    assert tokens.token(100) == "inst-token-3"
    tokens.invalidate(99)
    assert tokens.token(99) == "inst-token-4"
    assert "inst-token" not in repr(tokens)


# --- events and queue ---------------------------------------------------------------------------


def test_job_from_event_filters_drafts_closed_prs_and_untrusted_recheck() -> None:
    assert job_from_event("pull_request", pr_event(), "d") == PullRequestJob(
        99, "acme/py-cli", 7, "a" * 40, "b" * 40, "synchronize", "d"
    )
    assert job_from_event("pull_request", pr_event(draft=True), "d") is None
    assert job_from_event("pull_request", {**pr_event(), "action": "closed"}, "d") is None
    assert job_from_event("pull_request", pr_event(head="not-a-sha"), "d") is None
    comment: dict[str, Any] = {
        "action": "created",
        "installation": {"id": 99},
        "repository": {"full_name": "acme/py-cli"},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": "/docs-drift recheck please", "author_association": "MEMBER", "user": {"type": "User"}},
    }
    job = job_from_event("issue_comment", comment, "d")
    assert job is not None and job.reason == "recheck" and job.head_sha is None
    outsider = {**comment, "comment": {**comment["comment"], "author_association": "NONE"}}
    assert job_from_event("issue_comment", outsider, "d") is None
    assert job_from_event("issue_comment", {**comment, "repository": {"full_name": "../x"}}, "d") is None


def test_queue_coalesces_per_pr_and_supersedes_running_jobs() -> None:
    started, release = threading.Event(), threading.Event()
    outcomes: list[tuple[str | None, bool]] = []

    def processor(job: PullRequestJob, is_current: Callable[[], bool]) -> None:
        if job.head_sha == "1":
            started.set()
            release.wait(10)
        outcomes.append((job.head_sha, is_current()))

    queue = WebhookQueue(processor, max_pending=2, workers=1)
    assert queue.submit(PullRequestJob(1, "a/b", 7, "1")) == "queued"
    assert started.wait(10)
    assert queue.submit(PullRequestJob(1, "a/b", 7, "2")) == "queued"
    assert queue.submit(PullRequestJob(1, "a/b", 7, "3")) == "coalesced"
    assert queue.submit(PullRequestJob(1, "a/b", 8, "x")) == "queued"
    with pytest.raises(QueueFull):
        queue.submit(PullRequestJob(1, "a/b", 9, "y"))
    release.set()
    assert queue.wait_idle(10)
    queue.close()
    # Push 1 learns it was superseded; push 2 never runs; push 3 is current.
    assert outcomes == [("1", False), ("3", True), ("x", True)]


def test_replay_cache_is_bounded_and_expires() -> None:
    clock = [0.0]
    cache = ReplayCache(capacity=2, ttl=10, clock=lambda: clock[0])
    assert cache.check_and_add("a") and not cache.check_and_add("a")
    assert cache.check_and_add("b") and cache.check_and_add("c")
    clock[0] = 20
    assert cache.check_and_add("a")


def test_failing_job_does_not_kill_the_worker() -> None:
    calls: list[int] = []

    def processor(job: PullRequestJob, is_current: Callable[[], bool]) -> None:
        calls.append(job.number)
        if job.number == 1:
            raise RuntimeError("boom")

    queue = WebhookQueue(processor, workers=1)
    queue.submit(PullRequestJob(1, "a/b", 1, "x"))
    assert queue.wait_idle(10)
    queue.submit(PullRequestJob(1, "a/b", 2, "x"))
    assert queue.wait_idle(10)
    queue.close()
    assert calls == [1, 2] and queue.failed == 1 and queue.completed == 1


# --- end to end ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_pr(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    root = tmp_path_factory.mktemp("app-pr")
    build_fixtures(root)
    repo = root / "py-cli"
    base = git(repo, "rev-parse", "HEAD~4").strip()
    readme = (repo / "README.md").read_text(encoding="utf-8")
    (repo / "README.md").write_bytes(
        readme.replace("(default 8000).", "(default 8000) for the HTTP listener.").encode()
    )
    git(repo, "-c", "user.name=PR", "-c", "user.email=pr@example.invalid", "commit", "-qam", "Clarify serve")
    return repo, base, git(repo, "rev-parse", "HEAD").strip()


def test_scan_worker_honours_repo_config_and_reports_safe_failures(
    fixture_pr: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base, _ = fixture_pr
    workspace = tmp_path / "ws"
    shutil.copytree(repo, workspace / "repository")
    monkeypatch.setattr(sys, "platform", "win32")  # Never apply rlimits to the test process.

    def run_worker(base_sha: str = base) -> dict[str, Any]:
        request = json.dumps({"workspace": str(workspace), "base_sha": base_sha}) + "\n"
        monkeypatch.setattr(sys, "stdin", io.StringIO(request))
        scan_worker.main()
        return dict(json.loads(capsys.readouterr().out.splitlines()[-1]))

    (workspace / "repository" / ".docs-drift.toml").write_text("min_confidence = 0.95\n", encoding="utf-8")
    assert run_worker() == {"status": "done"}
    strict = ReviewBundle.model_validate_json((workspace / BUNDLE_NAME).read_text(encoding="utf-8"))
    assert strict.gate.policy.min_confidence == 0.95
    assert all(f.confidence >= 0.95 for f in strict.findings)
    (workspace / "repository" / ".docs-drift.toml").write_text("min_confidence = 5\n", encoding="utf-8")
    assert run_worker() == {
        "status": "failed",
        "kind": "config",
        "message": "The repository's docs-drift configuration is invalid.",
    }
    (workspace / "repository" / ".docs-drift.toml").unlink()
    missing = run_worker("f" * 40)  # An unavailable base fails with fixed guidance, never a traceback.
    assert missing["status"] == "failed" and missing["kind"] == "scan" and "Traceback" not in missing["message"]


def test_pull_request_webhook_end_to_end(
    config: GitHubAppConfig, fixture_pr: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = fixture_pr
    fake = FakeGitHub(pr_files(repo, base, head))
    transport = fake.transport()
    tokens = InstallationTokens(config.app_id, config.private_key, GitHubClient(transport=transport))
    processor = AppProcessor(
        config,
        tokens,
        tmp_path / "work",
        remote=lambda slug: repo.resolve().as_uri(),  # Local fixture instead of github.com.
        allow_local=True,
        transport=transport,
    )
    router = create_router(config, work_dir=tmp_path / "work", processor=processor)
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False), github_router=router)
    with TestClient(app) as client:
        started = time.monotonic()
        response = deliver(client, pr_event(head=head, base=base), delivery="e2e-1")
        assert response.status_code == 202 and time.monotonic() - started < 2  # Acknowledged before scanning.
        assert router.queue.wait_idle(240)  # type: ignore[attr-defined]
    assert router.queue.failed == 0  # type: ignore[attr-defined]
    (run,) = fake.check_runs
    assert run["head_sha"] == head and run["status"] == "completed" and run["conclusion"] == "failure"
    assert run["output"]["annotations"]
    (summary,) = fake.issue_comments[7]
    assert summary["body"].startswith(SUMMARY_MARKER) and "new documentation drift" in summary["body"]
    (review,) = fake.reviews
    assert review["commit_id"] == head
    suggestion = next(c for c in fake.review_comments if c["line"] == 4)
    assert (
        "```suggestion\n- `serve` accepts `--listen` (default 8000) for the HTTP listener.\n```" in suggestion["body"]
    )
    assert fake.tokens_issued == 1  # Fetch and publishing share one cached installation token.
    app_level = {"/app"}
    tokens_used = {
        r.headers.get("authorization")
        for r in fake.requests
        if "/access_tokens" not in r.url.path and r.url.path not in app_level
    }
    assert tokens_used == {"Bearer inst-token-1"}
    assert list((tmp_path / "work").iterdir()) == []  # Workspaces are always removed.
