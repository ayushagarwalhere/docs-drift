"""HTTP routes for "Write with voice": an editor page plus a bounded text-check API."""

import asyncio
import hashlib
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from docs_drift.errors import DocsDriftError
from docs_drift.limits import MAX_IP_BUCKETS
from docs_drift.mcp_service import Ticket
from docs_drift.web.jobs import Job, Store
from docs_drift.web.settings import Settings
from docs_drift.web.write import (
    DEFAULT_WRITE_PATH,
    HISTORY_NOTE,
    MAX_WRITE_TEXT_BYTES,
    WriteChecker,
    WriteIndex,
    load_write_index,
)

CHECK_PATH = "/api/write/check"
CHECK_TIMEOUT_SECONDS = 15
MAX_CONCURRENT_CHECKS = 2
MAX_LOADED_INDEXES = 16
DOC_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9_./ -]{1,200}\.(?:md|markdown|mdx|rst|txt)$")
DEFAULT_REPO = "example-py-cli"
SAMPLES = {
    "example-py-cli": (
        "# Run the server\n\n"
        "Start the server with the `serve` subcommand. `serve` accepts `--port` (default 8000) and `--host`.\n"
        "The `--workers` default is `4`.\n"
        "Set `APP_DEBUG` to enable verbose logs. Use the `migrate` subcommand to upgrade the database.\n"
    ),
    "example-fastapi-svc": (
        "# API\n\nCall `GET /users/{id}` to fetch a user and `POST /login` to sign in.\n"
        "Set `request_timeout` (default 30) in the settings. Create the app with `create_app()`.\n"
    ),
    "example-node-tool": (
        "# Usage\n\nBuild with `npm run build:prod`. Point the tool at your API with `API_URL`.\n"
        "Check `GET /health` before deploying.\n"
    ),
    "example-rich-cli": (
        "# Show line numbers\n\nAdd the `--line-number` switch to enable line numbers. "
        "Use `--theme` to pick a syntax theme and `--pager` to page long output.\n"
    ),
}
SAMPLES["example-rich-cli-reviewed"] = SAMPLES["example-rich-cli"]


class WriteCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    path: str = Field(default=DEFAULT_WRITE_PATH, min_length=1, max_length=200)
    text: str = Field(max_length=MAX_WRITE_TEXT_BYTES)


def register_write_routes(  # noqa: PLR0913, PLR0915 - route factory sharing the app's closures.
    app: FastAPI,
    *,
    settings: Settings,
    templates: Jinja2Templates,
    context: Callable[..., dict[str, Any]],
    job_for: Callable[[str], Job],
    store: Store,
    examples: list[dict[str, Any]],
    examples_dir: Path,
    buckets: dict[str, deque[float]],
    salt: bytes,
) -> None:
    """Register /write and /api/write/*; kept apart from the scan routes in app.py."""
    checker = WriteChecker(settings.data_dir.resolve() / "write-trees")
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    def example_for(ident: str) -> dict[str, Any] | None:
        return next((item for item in examples if item["scan_id"] == ident), None)

    loaded: OrderedDict[Path, tuple[tuple[int, int], WriteIndex | None]] = OrderedDict()

    def index_for(ident: str) -> tuple[Job, WriteIndex | None]:
        job = job_for(ident)
        example = example_for(ident)
        path = examples_dir / "indexes" / example["file"] if example else store.index_path(ident)
        if path is None or not path.is_file():
            return job, None
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = loaded.get(path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, load_write_index(path))
            loaded[path] = cached
        loaded.move_to_end(path)
        while len(loaded) > MAX_LOADED_INDEXES:
            loaded.popitem(last=False)
        return job, cached[1]

    def meta(ident: str) -> dict[str, Any]:
        job, index = index_for(ident)
        example = example_for(ident)
        reason = None
        if not job.result:
            reason = "This scan has not finished yet."
        elif index is None:
            reason = "This scan has no saved index for write checks. Scan the repository again to enable them."
        elif index.unavailable:
            reason = index.unavailable
        return {
            "scan_id": ident,
            "name": example["name"] if example else job.repo_url.removeprefix("https://github.com/"),
            "repo_url": job.repo_url,
            "head_sha": job.result.head_sha if job.result else None,
            "available": reason is None,
            "reason": reason,
            "sample": SAMPLES.get(ident, ""),
        }

    def limited(request: Request) -> None:
        now = time.monotonic()
        host = request.client.host if request.client else "unknown"
        # Same salted, ephemeral bucket store as scans, under a separate key and budget.
        key = hashlib.sha256(salt + b"write\0" + host.encode()).hexdigest()
        window = settings.write_rate_window
        for stale in [k for k, q in buckets.items() if not q or now - q[-1] > max(window, settings.rate_window)]:
            buckets.pop(stale, None)
        bucket = buckets[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= settings.write_rate_limit or len(buckets) > MAX_IP_BUCKETS:
            raise HTTPException(
                429,
                "Too many checks in a short time. Pause for a moment; your text is kept.",
                headers={"Retry-After": str(window)},
            )
        bucket.append(now)

    @app.get("/write", response_class=HTMLResponse)
    async def write_page(request: Request) -> Response:
        repos = []
        for item in examples:
            if (examples_dir / "indexes" / item["file"]).is_file():
                repos.append({"scan_id": item["scan_id"], "name": item["name"], "kind": item.get("kind", "fixture")})
        return templates.TemplateResponse(
            request,
            "write.html",
            context(
                request,
                write_repos=repos,
                default_repo=DEFAULT_REPO,
                default_sample=SAMPLES[DEFAULT_REPO],
                default_path=DEFAULT_WRITE_PATH,
                max_text_bytes=MAX_WRITE_TEXT_BYTES,
            ),
        )

    @app.get("/api/write/repos/{ident}")
    async def write_repo(ident: str) -> dict[str, Any]:
        return meta(ident)

    @app.post("/api/write/check")
    async def write_check(payload: WriteCheckRequest, request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and origin != settings.allowed_origin:
            raise HTTPException(403, "Check text from the demo's own page.")
        limited(request)
        if len(payload.text.encode("utf-8")) > MAX_WRITE_TEXT_BYTES:
            raise HTTPException(413, f"Write checks accept up to {MAX_WRITE_TEXT_BYTES // 1000} KB of text.")
        if not DOC_PATH.fullmatch(payload.path.replace("\\", "/")) or ".git/" in payload.path:
            raise HTTPException(422, "Use a relative documentation path such as docs/new.md or README.md.")
        info = meta(payload.scan_id)
        if not info["available"]:
            raise HTTPException(409, info["reason"])
        job, index = index_for(payload.scan_id)
        assert index is not None and job.result is not None
        ticket = Ticket()
        async with gate:
            try:
                body = await asyncio.wait_for(
                    asyncio.to_thread(
                        checker.check, payload.scan_id, index, job.result, payload.path, payload.text, ticket
                    ),
                    CHECK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                ticket.cancel.set()
                raise HTTPException(503, "The check took too long. Try a shorter passage.") from None
            except DocsDriftError:
                # Service messages can name the server-side tree; keep the public message generic.
                raise HTTPException(
                    422, "This text could not be checked. Use a Markdown path such as docs/new.md and plain Markdown."
                ) from None
        return {
            "scan_id": payload.scan_id,
            "repo": info["name"],
            "repo_url": job.repo_url,
            "note": HISTORY_NOTE,
            **body,
        }
