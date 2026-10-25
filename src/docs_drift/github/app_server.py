"""Hosted GitHub App: verified webhooks, a bounded coalescing queue, and PR scan/publish jobs.

``POST /github/webhook`` authenticates every delivery with ``X-Hub-Signature-256``
(HMAC-SHA256, constant-time comparison), rejects replayed delivery IDs, and
acknowledges within milliseconds. Work happens on a small thread pool: one
pending job per pull request (a newer push supersedes older pending or running
work), a bounded backlog, a disposable scan subprocess without credentials, and
publishing through the same core as the CLI and Action.
"""

import hashlib
import hmac
import json
import logging
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from docs_drift.github.app_config import GitHubAppConfig
from docs_drift.github.auth import InstallationTokens
from docs_drift.github.bundle import BUNDLE_NAME, ReviewBundle
from docs_drift.github.client import GitHubClient, GitHubError
from docs_drift.github.clone import fetch_pull_request
from docs_drift.github.publish import PublishOptions, PullRequestRef, conclude_check_run, publish_pull_request
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.git import JobError, git_environment

LOG = logging.getLogger("docs_drift.github.app")
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
RECHECK = re.compile(r"^\s*/docs-drift\s+recheck\b", re.I)
_SLUG = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DELIVERY = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's ``sha256=<hex>`` HMAC over the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().encode())


class ReplayCache:
    """Remember recent delivery IDs (bounded LRU with TTL); a repeated ID is a replay."""

    def __init__(self, capacity: int = 10_000, ttl: float = 86_400, clock: Callable[[], float] = time.monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, delivery: str) -> bool:
        """Return True for a first delivery, False for a replay."""
        now = self.clock()
        with self._lock:
            while self._seen and (now - next(iter(self._seen.values())) > self.ttl or len(self._seen) > self.capacity):
                self._seen.popitem(last=False)
            if delivery in self._seen:
                return False
            self._seen[delivery] = now
            return True

    def discard(self, delivery: str) -> None:
        """Forget a delivery we did not accept, so GitHub's redelivery (same ID) can succeed."""
        with self._lock:
            self._seen.pop(delivery, None)


@dataclass(frozen=True)
class PullRequestJob:
    installation_id: int
    repository: str
    number: int
    head_sha: str | None = None
    base_sha: str | None = None
    reason: str = "pull_request"
    delivery: str = ""

    @property
    def key(self) -> tuple[int, str, int]:
        return self.installation_id, self.repository.lower(), self.number


class QueueFull(Exception):
    """The bounded backlog is full; GitHub will show the delivery as failed and it can be redelivered."""


RESTART_TITLE = "Service restarting"
RESTART_SUMMARY = "docs-drift restarted before this check finished. Comment `/docs-drift recheck` to run it again."


class JobState:
    """Passed to a job: call it to ask "am I still the newest push?"; ``stopping`` reports shutdown."""

    def __init__(self, queue: "WebhookQueue", key: tuple[int, str, int], generation: int):
        self._queue, self._key, self._generation = queue, key, generation

    def __call__(self) -> bool:
        with self._queue._condition:
            return self._queue._generation.get(self._key) == self._generation

    @property
    def stopping(self) -> bool:
        return self._queue.closed


Processor = Callable[[PullRequestJob, JobState], None]


@dataclass
class WebhookQueue:
    """Thread-pool queue with per-PR coalescing; ``state()`` tells a job whether it was superseded."""

    processor: Processor
    max_pending: int = 32
    workers: int = 2
    _pending: "OrderedDict[tuple[int, str, int], PullRequestJob]" = field(default_factory=OrderedDict, init=False)
    _generation: dict[tuple[int, str, int], int] = field(default_factory=dict, init=False)
    _running: int = field(default=0, init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)
    completed: int = field(default=0, init=False)
    failed: int = field(default=0, init=False)
    superseded: int = field(default=0, init=False)
    dropped: int = field(default=0, init=False)

    def submit(self, job: PullRequestJob) -> str:
        with self._condition:
            if self.closed:
                raise QueueFull
            if job.key not in self._pending and len(self._pending) >= self.max_pending:
                raise QueueFull
            self._generation[job.key] = self._generation.get(job.key, 0) + 1
            if job.key in self._pending:
                self._pending[job.key] = job
                self.superseded += 1
                return "coalesced"
            self._pending[job.key] = job
            self._start()
            self._condition.notify()
            return "queued"

    def _start(self) -> None:
        while len(self._threads) < self.workers:
            thread = threading.Thread(target=self._loop, name=f"docs-drift-app-{len(self._threads)}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _loop(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self.closed:
                    self._condition.wait()
                if self.closed:
                    return
                key, job = self._pending.popitem(last=False)
                generation = self._generation[key]
                self._running += 1
            state = JobState(self, key, generation)
            try:
                self.processor(job, state)
                ok = True
            except Exception:
                LOG.exception("GitHub App job failed for %s#%s", job.repository, job.number)
                ok = False
            with self._condition:
                self._running -= 1
                self.completed += ok
                self.failed += not ok
                if self._generation.get(key) != generation:
                    self.superseded += 1
                elif key not in self._pending:
                    del self._generation[key]  # Bounded: only PRs with queued or running work are tracked.
                self._condition.notify_all()

    def wait_idle(self, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @property
    def depth(self) -> int:
        with self._condition:
            return len(self._pending) + self._running

    @property
    def tracked(self) -> int:
        with self._condition:
            return len(self._generation)

    def close(self, timeout: float = 15) -> None:
        """Stop accepting work. Acknowledged-but-unstarted jobs are reported, never silently dropped."""
        with self._condition:
            self.closed = True
            dropped = list(self._pending.values())
            self._pending.clear()
            self.dropped += len(dropped)
            self._condition.notify_all()
        for job in dropped:
            LOG.warning(
                "Shutting down: queued job for %s#%s was not run; comment /docs-drift recheck.",
                job.repository,
                job.number,
            )
        on_shutdown = getattr(self.processor, "on_shutdown", None)
        if callable(on_shutdown):
            try:
                on_shutdown(dropped)
            except Exception:
                LOG.exception("Could not mark interrupted docs-drift checks during shutdown.")
        for thread in self._threads:
            thread.join(timeout)


@dataclass
class AppProcessor:
    """Fetch → scan (subprocess, no credentials) → publish (check run, sticky summary, suggestions)."""

    config: GitHubAppConfig
    tokens: InstallationTokens
    work_dir: Path
    clone_timeout: float = 90
    scan_timeout: float = 180
    max_bytes: int = 200_000_000
    remote: Callable[[str], str] = lambda slug: f"https://github.com/{slug}.git"
    allow_local: bool = False
    transport: Any = None
    _active: dict[tuple[int, str, int], tuple[str, int]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _bot_login: str | None = field(default=None, init=False, repr=False)

    def client(self, installation_id: int) -> GitHubClient:
        return GitHubClient(
            token_provider=self.tokens.provider(installation_id), api_url=self.config.api_url, transport=self.transport
        )

    def bot_login(self) -> str | None:
        """``<app-slug>[bot]``: the only author whose markers and comments this App trusts or edits."""
        if self._bot_login is None:
            try:
                app = self.tokens.client.request("GET", "/app", auth=self.tokens.jwt()).json()
                self._bot_login = f"{app['slug']}[bot]"
            except (GitHubError, KeyError, ValueError, TypeError):
                LOG.warning("Could not read the App slug; only Bot-authored markers will be trusted.")
        return self._bot_login

    def __call__(self, job: PullRequestJob, state: JobState) -> None:  # noqa: PLR0912, PLR0915 - every exit concludes.
        if not _SLUG.fullmatch(job.repository):
            raise ValueError("Unexpected repository name in webhook payload.")
        owner, name = job.repository.split("/", 1)
        base = f"/repos/{owner}/{name}"
        with self.client(job.installation_id) as client:
            head, base_sha = job.head_sha, job.base_sha
            if not (head and base_sha):
                pr = client.get(f"{base}/pulls/{job.number}")
                if pr.get("state") != "open":
                    return
                head, base_sha = pr["head"]["sha"], pr["base"]["sha"]
            if not (head and base_sha and _SHA.fullmatch(head) and _SHA.fullmatch(base_sha)):
                raise ValueError("Unexpected commit identifiers in webhook payload.")
            check_id = None
            try:
                created = client.post(
                    f"{base}/check-runs",
                    {"name": self.config.check_name, "head_sha": head, "status": "in_progress"},
                )
                check_id = int(created["id"])
                with self._lock:
                    self._active[job.key] = (base, check_id)
            except (GitHubError, KeyError, ValueError, TypeError):
                LOG.warning("Could not create a check run for %s#%s", job.repository, job.number)
            concluded = False
            try:
                bundle, failure = None, None
                try:
                    bundle = self.scan(job, head, base_sha)
                except JobError as exc:
                    failure = f"{exc} {exc.hint}".strip()
                if state.stopping:
                    return  # on_shutdown already concluded the check run as interrupted.
                if not state():
                    LOG.info("Superseded %s#%s at %s; not publishing.", job.repository, job.number, head[:7])
                    if check_id:
                        conclude_check_run(
                            client, base, check_id, None, "A newer push replaced this run.", title="Superseded"
                        )
                        concluded = True
                    return
                if check_id:
                    conclude_check_run(client, base, check_id, bundle, failure)
                    concluded = True
                if bundle is None:
                    LOG.info("Scan failed for %s#%s: %s", job.repository, job.number, failure)
                    return
                report = publish_pull_request(
                    client,
                    PullRequestRef(owner, name, job.number, head),
                    bundle,
                    PublishOptions(check_run=False, check_name=self.config.check_name, bot_login=self.bot_login()),
                )
            except Exception:
                # Never leave a check run spinning: unexpected failures conclude it before re-raising.
                if check_id and not concluded and not state.stopping:
                    try:
                        conclude_check_run(
                            client,
                            base,
                            check_id,
                            None,
                            "docs-drift hit an internal error; comment `/docs-drift recheck` to retry.",
                            title="docs-drift could not finish",
                        )
                    except GitHubError:
                        LOG.warning("Could not conclude check run %s after a failure.", check_id)
                raise
            finally:
                with self._lock:
                    if self._active.get(job.key, (None, None))[1] == check_id:
                        self._active.pop(job.key, None)
            LOG.info(
                json.dumps(
                    {
                        "event": "pr_published",
                        "repository": job.repository,
                        "pr": job.number,
                        "sha": head,
                        "findings": len(bundle.findings),
                        "actions": [a.kind for a in report.actions],
                        "skipped": report.skipped,
                    }
                )
            )

    def on_shutdown(self, dropped: list[PullRequestJob]) -> None:
        """Conclude in-flight checks and mark queued pushes as interrupted, so nothing spins forever."""
        with self._lock:
            active = list(self._active.items())
            self._active.clear()
        for (installation, _repo, _number), (base, check_id) in active:
            with self.client(installation) as client:
                try:
                    conclude_check_run(client, base, check_id, None, RESTART_SUMMARY, title=RESTART_TITLE)
                except GitHubError:
                    LOG.warning("Could not conclude check run %s during shutdown.", check_id)
        for job in dropped[:20]:
            if not job.head_sha or not _SLUG.fullmatch(job.repository):
                continue
            with self.client(job.installation_id) as client:
                try:
                    client.post(
                        f"/repos/{job.repository}/check-runs",
                        {
                            "name": self.config.check_name,
                            "head_sha": job.head_sha,
                            "status": "completed",
                            "conclusion": "neutral",
                            "output": {"title": RESTART_TITLE, "summary": RESTART_SUMMARY},
                        },
                    )
                except GitHubError:
                    LOG.warning("Could not report the interrupted job for %s#%s.", job.repository, job.number)

    def scan(self, job: PullRequestJob, head: str, base_sha: str) -> ReviewBundle:
        workspace = self.work_dir / uuid.uuid4().hex
        workspace.mkdir(mode=0o700, parents=True)
        try:
            token = self.tokens.token(job.installation_id)
            fetch_pull_request(
                self.remote(job.repository),
                head,
                base_sha,
                workspace,
                token=token,
                deadline=time.monotonic() + self.clone_timeout,
                max_bytes=self.max_bytes,
                depth=self.config.history_depth,
                allow_local=self.allow_local,
            )
            env = git_environment(workspace)  # No tokens, keys or proxies reach the scanner.
            env["PYTHONPATH"] = str(Path(__file__).parents[2])
            options: dict[str, Any] = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if sys.platform == "win32"
                else {"start_new_session": True}
            )
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "docs_drift.github.scan_worker"],
                    input=json.dumps({"workspace": str(workspace), "base_sha": base_sha}) + "\n",
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=self.scan_timeout,
                    check=False,
                    **options,
                )
            except subprocess.TimeoutExpired:
                raise JobError(
                    "timeout", "The scan reached its time limit.", "Scan locally for large repositories."
                ) from None
            lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
            status = (
                json.loads(lines[-1]) if lines else {"status": "failed", "message": "The scan exited unexpectedly."}
            )
            if status.get("status") != "done":
                raise JobError(str(status.get("kind", "scan")), str(status.get("message", "Scan failed.")), "")
            return ReviewBundle.model_validate_json((workspace / BUNDLE_NAME).read_text(encoding="utf-8"))
        finally:
            remove_workspace(self.work_dir, workspace)


def _response(code: int, **body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})


def job_from_event(event: str, payload: dict[str, Any], delivery: str) -> PullRequestJob | None:
    """Translate a verified delivery into a job, or None when the event needs no work."""
    installation = (payload.get("installation") or {}).get("id")
    repository = (payload.get("repository") or {}).get("full_name")
    if not isinstance(installation, int) or not isinstance(repository, str) or not _SLUG.fullmatch(repository):
        return None
    if event == "pull_request" and payload.get("action") in PR_ACTIONS:
        pr = payload.get("pull_request") or {}
        if pr.get("draft") or pr.get("state") != "open" or not isinstance(pr.get("number"), int):
            return None
        head, base = (pr.get("head") or {}).get("sha"), (pr.get("base") or {}).get("sha")
        if not (isinstance(head, str) and isinstance(base, str) and _SHA.fullmatch(head) and _SHA.fullmatch(base)):
            return None
        return PullRequestJob(installation, repository, pr["number"], head, base, str(payload["action"]), delivery)
    if event == "issue_comment" and payload.get("action") == "created":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        if (
            issue.get("pull_request")
            and isinstance(issue.get("number"), int)
            and RECHECK.match(str(comment.get("body") or ""))
            and comment.get("author_association") in TRUSTED_ASSOCIATIONS
            and (comment.get("user") or {}).get("type") != "Bot"
        ):
            return PullRequestJob(installation, repository, issue["number"], reason="recheck", delivery=delivery)
    return None


def sweep_workspaces(work_dir: Path) -> int:
    """Remove job workspaces left behind by a crash or restart (only our 32-hex directories)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for workspace in work_dir.iterdir():
        if re.fullmatch(r"[0-9a-f]{32}", workspace.name) and workspace.is_dir() and not workspace.is_symlink():
            remove_workspace(work_dir, workspace)
            removed += 1
    return removed


def create_router(
    config: GitHubAppConfig,
    *,
    work_dir: Path,
    processor: Processor | None = None,
    replay: ReplayCache | None = None,
    transport: Any = None,
) -> APIRouter:
    """Build the webhook router. ``processor``/``transport`` are dependency injection for tests only."""
    sweep_workspaces(work_dir)
    if processor is None:
        tokens = InstallationTokens(
            config.app_id, config.private_key, GitHubClient(api_url=config.api_url, transport=transport)
        )
        processor = AppProcessor(config, tokens, work_dir, transport=transport)
    queue = WebhookQueue(processor, max_pending=config.max_pending, workers=config.max_concurrent)
    replays = replay or ReplayCache()
    router = APIRouter()
    router.queue = queue  # type: ignore[attr-defined]

    @router.post("/github/webhook", include_in_schema=False)
    async def webhook(request: Request) -> JSONResponse:  # noqa: PLR0911 - one response per rejection.
        body = await request.body()
        if not verify_signature(config.webhook_secret, body, request.headers.get("x-hub-signature-256")):
            return _response(401, detail="Invalid or missing webhook signature.")
        delivery = request.headers.get("x-github-delivery", "")
        event = request.headers.get("x-github-event", "")
        if not _DELIVERY.fullmatch(delivery) or not re.fullmatch(r"[a-z_]{1,64}", event):
            return _response(400, detail="Missing GitHub delivery headers.")
        try:
            payload = json.loads(body)
        except ValueError:
            return _response(400, detail="Payload is not JSON.")
        if not isinstance(payload, dict):
            return _response(400, detail="Payload is not a JSON object.")
        # Record the ID only for deliveries we accept: GitHub redelivers with the same ID.
        if not replays.check_and_add(delivery):
            return _response(409, detail="Duplicate delivery ignored.")
        if event == "ping":
            return _response(200, status="pong")
        if event in {"installation", "installation_repositories"}:
            account = ((payload.get("installation") or {}).get("account") or {}).get("login")
            LOG.info(json.dumps({"event": event, "action": payload.get("action"), "account": account}))
            return _response(202, status="logged")
        job = job_from_event(event, payload, delivery)
        if job is None:
            return _response(202, status="ignored")
        try:
            state = queue.submit(job)
        except QueueFull:
            replays.discard(delivery)
            return _response(503, detail="The scan queue is full; redeliver later.")
        LOG.info(json.dumps({"event": event, "repository": job.repository, "pr": job.number, "queue": state}))
        return _response(202, status=state)

    return router
