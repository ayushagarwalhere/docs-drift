"""Bounded replaceable queue, expiring LRU result store, and disposable workers."""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from docs_drift.limits import CLONE_FAILURE_THRESHOLD, MAX_STORED_BYTES
from docs_drift.models import ScanResult
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.git import JobError, disk_bytes, git_environment
from docs_drift.web.settings import Settings
from docs_drift.web.validation import Repository

LOG = logging.getLogger("docs_drift.web")
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
LOG.propagate = False
STAGES = {"queued": 0, "cloning": 10, "indexing": 35, "matching": 60, "history": 80, "done": 100, "failed": 100}


def _worker_process_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@dataclass
class Job:
    scan_id: str
    repo_url: str
    ref: str | None = None
    status: str = "queued"
    created: float = field(default_factory=time.time)
    counts: dict[str, int] = field(default_factory=dict)
    result: ScanResult | None = None
    error: dict[str, str] | None = None
    cached: bool = False
    metrics: dict[str, float] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "repo_url": self.repo_url,
            "status": self.status,
            "progress": STAGES[self.status],
            "counts": self.counts,
            "error": self.error,
            "cached": self.cached,
            "result": self.result.model_dump(mode="json") if self.result else None,
            "metrics": self.metrics,
        }


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.cache: dict[str, str] = {}
        self.directory = settings.data_dir.resolve() / "results"
        self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, ident: str) -> Job | None:
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", ident):
            return None
        job = self.jobs.get(ident)
        path = self.directory / f"{ident}.json"
        if job is None and path.is_file() and path.stat().st_size <= MAX_STORED_BYTES:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["result"] = ScanResult.model_validate(data["result"]) if data.get("result") else None
            job = Job(**data)
        if job and time.time() - job.created < self.settings.ttl:
            self.jobs[ident] = job
            self.jobs.move_to_end(ident)
            self.trim()
            return job
        self.jobs.pop(ident, None)
        path.unlink(missing_ok=True)
        return None

    def trim(self) -> None:
        while len(self.jobs) > self.settings.max_results:
            inactive = next((k for k, v in self.jobs.items() if v.status in {"done", "failed"}), None)
            if inactive is None:
                break
            self.jobs.pop(inactive)

    def save(self, job: Job) -> None:
        self.jobs[job.scan_id] = job
        self.trim()
        if job.status not in {"done", "failed"}:
            return
        data = asdict(job)
        data["result"] = job.result.model_dump(mode="json") if job.result else None
        path = self.directory / f"{job.scan_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)
        if job.result:
            self.cache[self.key(job.repo_url, job.result.head_sha)] = job.scan_id
        (self.directory / "cache.json").write_text(json.dumps(self.cache), encoding="utf-8")
        self.expire()

    @staticmethod
    def key(url: str, sha: str) -> str:
        return hashlib.sha256((url + "\0" + sha).encode()).hexdigest()

    def expire(self) -> None:
        files = sorted(
            (p for p in self.directory.glob("*.json") if p.name != "cache.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        used = 0
        for index, path in enumerate(files):
            used += path.stat().st_size
            if (
                time.time() - path.stat().st_mtime > self.settings.ttl
                or index >= self.settings.max_results
                or used > self.settings.max_bytes
            ):
                path.unlink(missing_ok=True)
                self.jobs.pop(path.stem, None)
        self.cache = {key: ident for key, ident in self.cache.items() if (self.directory / f"{ident}.json").is_file()}
        (self.directory / "cache.json").write_text(json.dumps(self.cache), encoding="utf-8")


class QueueBackend(Protocol):
    def submit(self, repository: Repository) -> Job: ...
    async def close(self) -> None: ...


class LocalQueue:
    def __init__(self, settings: Settings, store: Store, test_runner: Any = None):
        self.settings, self.store = settings, store
        self.semaphore = asyncio.Semaphore(settings.max_concurrent)
        self.tasks: set[asyncio.Task[None]] = set()
        self.test_runner = test_runner  # Dependency injection only; no HTTP request can select it.
        self.clone_failures = 0
        self.circuit_until = 0.0
        self.total, self.drift, self.failed = 0, 0, 0
        self.work = settings.data_dir.resolve() / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        cache = store.directory / "cache.json"
        if cache.exists():
            store.cache = json.loads(cache.read_text(encoding="utf-8"))

    @property
    def available(self) -> bool:
        return self.settings.live_enabled and time.monotonic() >= self.circuit_until

    def submit(self, repository: Repository) -> Job:
        if len(self.tasks) >= self.settings.max_queue:
            raise ValueError("The scan queue is full. Try an instant example or retry in a minute.")
        job = Job(uuid.uuid4().hex, repository.url, repository.ref)
        self.store.save(job)
        task = asyncio.create_task(self.run(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def run(self, job: Job) -> None:  # noqa: PLR0912 - terminal states share mandatory cleanup.
        async with self.semaphore:
            start = time.monotonic()
            workspace = self.work / job.scan_id
            workspace.mkdir(mode=0o700)
            try:
                async with asyncio.timeout(self.settings.scan_timeout):
                    if self.test_runner:
                        await self.test_runner(job, workspace)
                    else:
                        await self.bounded_worker(job, workspace)
                if job.result:
                    job.status = "done"
                    self.total += not job.cached
                    self.drift += sum(f.is_drift for f in job.result.findings) if not job.cached else 0
                    self.clone_failures = 0
            except TimeoutError:
                job.status, job.error = (
                    "failed",
                    {
                        "kind": "timeout",
                        "message": "The scan reached its 120 second time limit.",
                        "hint": "Try a smaller repository or an instant example.",
                    },
                )
            except JobError as exc:
                job.status, job.error = "failed", {"kind": exc.kind, "message": str(exc), "hint": exc.hint}
            except asyncio.CancelledError:
                job.status, job.error = (
                    "failed",
                    {
                        "kind": "restart",
                        "message": "The service restarted during this scan.",
                        "hint": "Please submit it again.",
                    },
                )
                raise
            except Exception:
                job.status, job.error = (
                    "failed",
                    {
                        "kind": "scan",
                        "message": "This scan could not finish safely.",
                        "hint": "Try an example or run the CLI locally.",
                    },
                )
            finally:
                if job.status == "failed":
                    self.failed += 1
                    if job.error and job.error["kind"] == "clone":
                        self.clone_failures += 1
                        if self.clone_failures >= CLONE_FAILURE_THRESHOLD:
                            self.circuit_until = time.monotonic() + 300
                job.metrics["duration_seconds"] = round(time.monotonic() - start, 3)
                try:
                    self.store.save(job)
                finally:
                    # Only trusted UUID workspaces are removed; child symlinks are not followed.
                    if workspace.parent.resolve() == self.work and not workspace.is_symlink():
                        cleanup = asyncio.create_task(asyncio.to_thread(remove_workspace, self.work, workspace))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # Shutdown must wait for deletion already running in
                            # a thread; cancelling its await does not stop it.
                            await cleanup
                            raise
                LOG.info(
                    json.dumps(
                        {
                            "event": "scan_finished",
                            "repo": job.repo_url,
                            "sha": job.result.head_sha if job.result else None,
                            "duration": job.metrics["duration_seconds"],
                            "counts": job.result.summary()["by_verdict"] if job.result else {},
                            "failure": job.error["kind"] if job.error else None,
                            "cached": job.cached,
                        }
                    )
                )

    async def bounded_worker(self, job: Job, workspace: Path) -> None:
        async def monitor() -> None:
            while True:
                if await asyncio.to_thread(disk_bytes, workspace) > self.settings.max_bytes:
                    raise JobError(
                        "too_large",
                        "The repository exceeded the 200 MB scan limit.",
                        "Choose a smaller repository or a baked example.",
                    )
                await asyncio.sleep(0.5)

        scan = asyncio.create_task(self.worker(job, workspace))
        watch = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({scan, watch}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            scan.cancel()
            watch.cancel()
            await asyncio.gather(scan, watch, return_exceptions=True)

    async def worker(self, job: Job, workspace: Path) -> None:

        options = _worker_process_options()
        env = git_environment(workspace)
        # Preserve trusted package import configuration, not credentials.
        env["PYTHONPATH"] = str(Path(__file__).parents[2])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "docs_drift.web.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=1_000_000,
            **options,
        )
        assert process.stdin and process.stdout
        config = asdict(self.settings)
        config["data_dir"] = str(self.settings.data_dir)
        process.stdin.write(
            (
                json.dumps({"repo_url": job.repo_url, "ref": job.ref, "workspace": str(workspace), "settings": config})
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
        try:
            while line := await process.stdout.readline():
                data = json.loads(line)
                if "resolved_sha" in data:
                    cached_id = self.store.cache.get(self.store.key(job.repo_url, data["resolved_sha"]))
                    cached = self.store.get(cached_id) if cached_id else None
                    if cached and cached.result:
                        job.result, job.cached = cached.result, True
                        process.stdin.write(b"stop\n")
                        await process.stdin.drain()
                        break
                    process.stdin.write(b"continue\n")
                    await process.stdin.drain()
                elif data.get("status") in STAGES:
                    job.status = data["status"]
                    job.counts = data.get("counts", job.counts)
                    if job.status == "failed":
                        job.error = data["error"]
                    if job.status == "done":
                        job.result = ScanResult.model_validate_json(
                            (workspace / "result.json").read_text(encoding="utf-8")
                        )
                        job.metrics.update({k: data[k] for k in ("clone_seconds", "download_bytes")})
            await process.wait()
            if not job.result and job.status != "failed":
                raise RuntimeError("Worker exited without a result")
        finally:
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
            if process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                await process.wait()

    async def close(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
