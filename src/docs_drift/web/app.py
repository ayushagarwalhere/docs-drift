"""One deployable service for the public API and server-rendered demo."""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from docs_drift import badge as badges
from docs_drift.fix.bundle import FixBundle, FixStatus
from docs_drift.fix.pull_request import (
    apply_commands,
    patch_document,
    remote_for,
    render_issue_drafts,
    render_pull_request,
)
from docs_drift.limits import MAX_BODY_BYTES, MAX_IP_BUCKETS, MAX_WEBHOOK_BYTES
from docs_drift.models import ScanResult
from docs_drift.report import ContributePanel, RenderOptions, render
from docs_drift.report.html import graph_data
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.jobs import Job, LocalQueue, Store
from docs_drift.web.settings import Settings
from docs_drift.web.try_routes import CHECK_PATH as TRY_CHECK_PATH
from docs_drift.web.try_routes import MAX_TRY_BODY_BYTES, register_try_routes
from docs_drift.web.validation import validate_repository
from docs_drift.web.write import MAX_WRITE_BODY_BYTES
from docs_drift.web.write_routes import CHECK_PATH as WRITE_CHECK_PATH
from docs_drift.web.write_routes import register_write_routes

HERE = Path(__file__).parent
REPORT = HERE.parent / "report"


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_url: str = Field(max_length=512)
    ref: str | None = Field(default=None, max_length=200)


class SafetyMiddleware:
    def __init__(self, app: Any, large_paths: dict[str, int] | None = None):
        self.app = app
        # Signed GitHub webhooks are the only larger POST bodies; everything else keeps the tight limit.
        self.large_paths = large_paths or {}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST":
            limit = self.large_paths.get(scope["path"], MAX_BODY_BYTES)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    await JSONResponse(
                        {"detail": "The request is too large. Send only a GitHub URL and optional ref."},
                        status_code=413,
                    )(scope, receive, send)
                    return
                if not message.get("more_body"):
                    break
            sent = False
            original = receive

            async def replay() -> Any:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original()

            receive = replay
        await self.app(scope, receive, send)


def _github_router(settings: Settings) -> Any:
    """Mount the GitHub App webhook only when DOCS_DRIFT_GITHUB_APP_* is configured."""
    from docs_drift.github.app_config import GitHubAppConfig  # noqa: PLC0415 - import-light settings.

    config = GitHubAppConfig.from_env()
    if config is None:
        return None
    from docs_drift.github.app_server import create_router  # noqa: PLC0415 - optional github-app extra.

    return create_router(config, work_dir=settings.data_dir.resolve() / "github-work")


def create_app(  # noqa: PLR0915 - route factory.
    settings: Settings | None = None, *, test_runner: Any = None, github_router: Any = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    if github_router is None:
        github_router = _github_router(settings)
    store = Store(settings)
    queue = LocalQueue(settings, store, test_runner)
    examples: list[dict[str, Any]] = []
    example_jobs: dict[str, Job] = {}
    example_meta: dict[str, dict[str, Any]] = {}
    templates = Jinja2Templates(directory=HERE / "templates")
    buckets: dict[str, deque[float]] = defaultdict(deque)
    salt = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for workspace in queue.work.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}", workspace.name) and workspace.is_dir() and not workspace.is_symlink():
                await asyncio.to_thread(remove_workspace, queue.work, workspace)
        store.expire()
        manifest = HERE / "examples" / "manifest.json"
        if manifest.exists():
            for item in json.loads(manifest.read_text(encoding="utf-8")):
                result = ScanResult.model_validate_json((HERE / "examples" / item["file"]).read_text(encoding="utf-8"))
                job = Job(item["scan_id"], item["repo_url"], status="done", result=result, cached=True)
                if item.get("fixes"):
                    # Baked with scripts/build_web_examples.py --fixes from the same revision's checkout.
                    job.fixes = FixBundle.model_validate_json(
                        (HERE / "examples" / item["fixes"]).read_text(encoding="utf-8")
                    )
                store.jobs[job.scan_id] = job
                example_jobs[job.scan_id] = job
                example_meta[job.scan_id] = item
                examples.append(item)
        yield
        await queue.close()
        if github_router is not None:
            await asyncio.to_thread(github_router.queue.close)

    app = FastAPI(title="docs-drift", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.queue, app.state.store = queue, store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.allowed_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    large_paths = {"/github/webhook": MAX_WEBHOOK_BYTES} if github_router is not None else {}
    large_paths[WRITE_CHECK_PATH] = MAX_WRITE_BODY_BYTES  # /write sends up to 20 KB of documentation text.
    large_paths[TRY_CHECK_PATH] = MAX_TRY_BODY_BYTES  # /try sends two 20 KB panes plus the original code.
    app.add_middleware(SafetyMiddleware, large_paths=large_paths)
    if github_router is not None:
        app.include_router(github_router)

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        hashes = []
        if response.headers.get("content-type", "").startswith("text/html"):
            content = b"".join([part async for part in response.body_iterator])
            # Hash only trusted inline executable blocks; repo JSON cannot break out of its escaped data tag.
            for script in re.findall(rb"<script(?:\s[^>]*)?>(.*?)</script>", content, re.S):
                hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'")
            response = HTMLResponse(content, status_code=response.status_code, headers=dict(response.headers))
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' "
            + " ".join(hashes)
            + "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/scans/"):
            response.headers["Cache-Control"] = "no-store"
        return cast(Response, response)

    def job_for(ident: str) -> Job:
        # Examples are immutable baked assets, independent of the expiring live-job LRU.
        if ident in example_jobs:
            return example_jobs[ident]
        job = store.get(ident)
        if job is None:
            raise HTTPException(404, "This scan expired or does not exist. Run a new scan or choose an example.")
        return job

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "examples": examples,
            "settings": settings,
            "live_available": queue.available,
            **extra,
        }

    def ready(job: Job, what: str) -> ScanResult:
        if not job.result:
            raise HTTPException(409, f"The {what} is not ready yet.")
        return job.result

    def links(ident: str, job: Job) -> tuple[str | None, str | None]:
        """GitHub remote for evidence links, plus the checkout step for synthetic fixtures without one."""
        item = example_meta.get(ident, {})
        if item.get("kind") == "fixture":
            return None, item.get("checkout")
        return remote_for(job.repo_url), None

    def contribute_panel(ident: str, job: Job) -> ContributePanel | None:
        result = job.result
        if result is None:
            return None
        remote, checkout = links(ident, job)
        bundle = job.fixes
        statuses = {claim: status.value for claim, status in bundle.status().items()} if bundle else {}
        drift = [statuses.get(f.claim.claim_id, FixStatus.manual.value) for f in result.findings if f.is_drift]
        panel = ContributePanel(
            scan_id=ident,
            prepared=bundle is not None,
            patch_available=bool(bundle and bundle.available),
            patched=drift.count(FixStatus.patched.value),
            manual=drift.count(FixStatus.manual.value),
            withheld=drift.count(FixStatus.withheld.value),
            issues_available=bool(drift),
            commands=apply_commands(remote, result.head_sha, checkout=checkout),
            files=[(item.path, item.additions, item.deletions) for item in bundle.files] if bundle else [],
            statuses=statuses,
        )
        if ident in example_meta and example_meta[ident].get("kind") == "fixture":
            panel.note = "Synthetic fixture: its repository URL is illustrative, so the first command rebuilds it."
        if ident not in example_jobs and job.ref is None and remote:
            slug = remote.removeprefix("https://github.com/")
            origin = settings.allowed_origin
            # Preview this scan's own count inline: the badge index may not include it until the job is saved.
            stale = badges.stale_count(result.findings, result.config_used.min_confidence)
            panel.badge_image = "data:image/svg+xml;base64," + base64.b64encode(badges.svg(stale).encode()).decode()
            panel.badge_markdown = f"[![docs drift]({origin}/badge/{slug}.svg)]({origin}/)"
            panel.badge_endpoint = "https://img.shields.io/endpoint?url=" + quote(
                f"{origin}/badge/{slug}.json", safe=""
            )
        return panel

    def report_options(ident: str, job: Job) -> RenderOptions:
        return RenderOptions(share_id=ident, build_sha=settings.build_sha, contribute=contribute_panel(ident, job))

    def badge_target(owner: str, repo: str) -> str:
        try:
            return validate_repository(f"https://github.com/{owner}/{repo}").url
        except ValueError:
            raise HTTPException(
                404, "Use /badge/<owner>/<repo>.svg with a public GitHub owner and repository name."
            ) from None

    def badge_response(body: bytes, media_type: str, found: bool, request: Request) -> Response:
        # Badge hits only read an in-memory summary; they never start or load a scan.
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {"Cache-Control": f"public, max-age={300 if found else 60}", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=media_type, headers=headers)

    @app.get("/badge/{owner}/{repo}.svg")
    async def badge_svg(owner: str, repo: str, request: Request) -> Response:
        entry = store.badge(badge_target(owner, repo))
        body = badges.svg(entry["stale"] if entry else None).encode()
        return badge_response(body, "image/svg+xml", entry is not None, request)

    @app.get("/badge/{owner}/{repo}.json")
    async def badge_json(owner: str, repo: str, request: Request) -> Response:
        entry = store.badge(badge_target(owner, repo))
        body = json.dumps(badges.endpoint(entry["stale"] if entry else None), separators=(",", ":")).encode()
        return badge_response(body, "application/json", entry is not None, request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "sha": settings.build_sha}

    @app.get("/api/examples")
    async def get_examples() -> list[dict[str, Any]]:
        return [{k: v for k, v in e.items() if k != "file"} for e in examples]

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return {
            "scanned_repos": queue.total,
            "stale_statements": queue.drift,
            "failed_scans": queue.failed,
            "active_or_queued": len(queue.tasks),
            "live_available": queue.available,
            "examples": len(examples),
        }

    @app.post("/api/scans", status_code=202)
    async def submit(payload: ScanRequest, request: Request) -> dict[str, str]:
        origin = request.headers.get("origin")
        if origin and origin != settings.allowed_origin:
            raise HTTPException(403, "Submit scans from the demo's own page.")
        now = time.monotonic()
        # IP identifiers exist only as ephemeral salted hashes; neither addresses nor hashes are logged.
        key = hashlib.sha256(salt + (request.client.host if request.client else "unknown").encode()).hexdigest()
        for stale in [k for k, q in buckets.items() if not q or now - q[-1] > settings.rate_window]:
            buckets.pop(stale, None)
        bucket = buckets[key]
        while bucket and now - bucket[0] >= settings.rate_window:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit or len(buckets) > MAX_IP_BUCKETS:
            raise HTTPException(
                429,
                "You've reached 5 scans in 10 minutes. Try an instant example or come back shortly.",
                headers={"Retry-After": str(settings.rate_window)},
            )
        bucket.append(now)
        try:
            repository = validate_repository(payload.repo_url, payload.ref)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not queue.available:
            raise HTTPException(
                503, "Live GitHub scans are temporarily paused. The pre-scanned examples are still available."
            )
        try:
            job = queue.submit(repository)
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from None
        return {"scan_id": job.scan_id, "status": job.status}

    @app.get("/api/scans/{ident}")
    async def status(ident: str) -> dict[str, Any]:
        return job_for(ident).public()

    @app.get("/api/scans/{ident}/result.json")
    async def result_json(ident: str) -> Response:
        job = job_for(ident)
        if not job.result:
            raise HTTPException(409, "The report is not ready yet.")
        return Response(
            render(job.result, "json"),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="scan.json"'},
        )

    @app.get("/api/scans/{ident}/graph")
    async def graph(ident: str) -> dict[str, Any]:
        job = job_for(ident)
        if not job.result:
            raise HTTPException(409, "The graph is not ready yet.")
        return graph_data(job.result)

    @app.get("/api/scans/{ident}/report.html", response_class=HTMLResponse)
    async def html_report(ident: str) -> str:
        job = job_for(ident)
        return render(ready(job, "report"), "html", report_options(ident, job))

    no_patch = (
        "docs-drift found no certain documentation edits for this scan, so there is no patch. "
        "Copy the issue drafts instead, or run `docs-drift fix` on a local checkout."
    )

    @app.get("/api/scans/{ident}/fix.patch")
    async def fix_patch(ident: str) -> Response:
        job = job_for(ident)
        ready(job, "patch")
        if job.fixes is None or not job.fixes.available:
            raise HTTPException(404, no_patch)
        return Response(
            patch_document(job.fixes, links(ident, job)[0]),
            media_type="text/x-diff",
            headers={"Content-Disposition": 'attachment; filename="fix.patch"'},
        )

    @app.get("/api/scans/{ident}/pr.md")
    async def pull_request(ident: str) -> Response:
        job = job_for(ident)
        result = ready(job, "pull request description")
        if job.fixes is None or not job.fixes.available:
            raise HTTPException(404, no_patch)
        text = render_pull_request(result, job.fixes, remote=links(ident, job)[0], project_url=settings.project_url)
        return Response(
            text, media_type="text/markdown", headers={"Content-Disposition": 'inline; filename="pull-request.md"'}
        )

    @app.get("/api/scans/{ident}/issues.md")
    async def issue_drafts(ident: str) -> Response:
        job = job_for(ident)
        result = ready(job, "issue drafts")
        if not any(finding.is_drift for finding in result.findings):
            raise HTTPException(404, "This scan found no stale statements, so there are no issues to draft.")
        text = render_issue_drafts(result, job.fixes, remote=links(ident, job)[0], project_url=settings.project_url)
        return Response(
            text, media_type="text/markdown", headers={"Content-Disposition": 'inline; filename="issue-drafts.md"'}
        )

    @app.get("/api/scans/{ident}/report.md")
    async def review_report(ident: str) -> Response:
        job = job_for(ident)
        if not job.result:
            raise HTTPException(409, "The review is not ready yet.")
        report_url = f"{settings.allowed_origin}/scan/{ident}"
        introduction = (
            "# Documentation review draft\n\n"
            "Inspect each finding before changing documentation. This summary is not an applicable patch "
            "or an independent accuracy evaluation. Unverifiable claims remain unresolved.\n\n"
            f"[Open the evidence report]({report_url}) · Revision `{job.result.head_sha}`\n\n"
        )
        return Response(
            introduction + render(job.result, "markdown", RenderOptions(artifact_url=report_url)),
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="documentation-review.md"'},
        )

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> Response:
        return templates.TemplateResponse(request, "landing.html", context(request))

    @app.get("/scan/{ident}", response_class=HTMLResponse)
    async def scan_page(ident: str, request: Request) -> Response:
        job = job_for(ident)
        if job.result:
            return HTMLResponse(render(job.result, "html", report_options(ident, job)))
        return templates.TemplateResponse(request, "scan.html", context(request, job=job))

    @app.get("/built-with-voice", response_class=HTMLResponse)
    async def built(request: Request) -> Response:
        return templates.TemplateResponse(request, "voice.html", context(request))

    @app.get("/case-study", response_class=HTMLResponse)
    async def case_study(request: Request) -> Response:
        return templates.TemplateResponse(request, "case.html", context(request))

    @app.get("/static/cytoscape.min.js")
    async def cytoscape() -> FileResponse:
        return FileResponse(REPORT / "vendor/cytoscape.min.js", media_type="text/javascript")

    @app.get("/static/report.css")
    async def report_css() -> Response:
        return Response((REPORT / "partials/report.css").read_text(encoding="utf-8"), media_type="text/css")

    # --- Write with voice (/write, /api/write/*): registered separately in write_routes.py. ---
    register_write_routes(
        app,
        settings=settings,
        templates=templates,
        context=context,
        job_for=job_for,
        store=store,
        examples=examples,
        examples_dir=HERE / "examples",
        buckets=buckets,
        salt=salt,
    )

    # --- Try it (/try, /api/try/check): a throwaway sandbox, registered in try_routes.py. ---
    register_try_routes(app, settings=settings, templates=templates, context=context, buckets=buckets, salt=salt)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    return app


app = create_app()
