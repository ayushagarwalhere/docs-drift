"""HTTP routes for "Try it": a sandbox where visitors edit a tiny CLI and its README, then check them.

The code is written to a fresh temporary directory and only parsed (the indexers read syntax
trees); it is never imported or run. When the visitor changed the code, the original sample and
their edit become two commits, so renames and changed defaults are proven from Git history the
same way a real repository scan proves them. Every request gets its own directory, removed
when the check ends.
"""

import asyncio
import hashlib
import shutil
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from docs_drift.config import Config
from docs_drift.errors import DocsDriftError
from docs_drift.fix.edits import StaleDocumentError, TextEdit
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.history import _run
from docs_drift.limits import MAX_IP_BUCKETS
from docs_drift.mcp_service import DriftService, Ticket
from docs_drift.models import ScanResult
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.settings import Settings
from docs_drift.web.write import present

CHECK_PATH = "/api/try/check"
MAX_TRY_TEXT_BYTES = 20_000
# Two panes of up to 20 KB each, plus the unchanged original code, JSON-escaped.
MAX_TRY_BODY_BYTES = 160_000
CHECK_TIMEOUT_SECONDS = 15
MAX_CONCURRENT_CHECKS = 2
CODE_FILE = "app.py"
DOC_FILE = "README.md"
NOTE = (
    "Checked in a throwaway folder: app.py is parsed, never run. Your edit to the code is committed on top of "
    "the original, so renames are proven from Git history like a real scan."
)

# The project file is fixed by the server so `shipit ...` commands in the README resolve to app.py.
PROJECT_FILE = (
    '[project]\nname = "shipit"\nversion = "1.0.0"\nrequires-python = ">=3.10"\n\n'
    '[project.scripts]\nshipit = "app:main"\n'
)

SAMPLE_CODE = '''"""shipit: a tiny deploy server."""
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="shipit")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the deploy server.")
    serve.add_argument("--listen", default="127.0.0.1:8000", help="Address to bind.")
    serve.add_argument("--workers", type=int, default=4, help="Worker processes.")
    serve.add_argument("--dry-run", action="store_true", help="Print actions only.")

    commands.add_parser("status", help="Show the last deploy.")

    args = parser.parse_args()
    token = os.environ.get("SHIPIT_TOKEN")
    if args.command == "serve" and not token:
        parser.error("Set SHIPIT_TOKEN first.")


if __name__ == "__main__":
    main()
'''

SAMPLE_README = """# shipit

A tiny deploy server.

## Run it

Export `SHIPIT_TOKEN`, then start the server:

```sh
shipit serve --listen 0.0.0.0:8000
```

- `--workers` sets the number of worker processes (default `4`).
- `--dry-run` prints what would happen without deploying.
"""


class TryCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=MAX_TRY_TEXT_BYTES)
    readme: str = Field(max_length=MAX_TRY_TEXT_BYTES)
    previous_code: str | None = Field(default=None, max_length=MAX_TRY_TEXT_BYTES)


class SandboxService(DriftService):
    """``DriftService`` with server-owned defaults: a visitor's files never configure the scan."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._fixed_config = Config()

    @property
    def config(self) -> Config:
        return self._fixed_config


def _git(root: Path, *args: str) -> None:
    # history._run strips GIT_* variables, ignores user and system config and disables hooks.
    identity = ["-c", "user.name=docs-drift sandbox", "-c", "user.email=sandbox@docs-drift.invalid"]
    if _run(root, [*identity, "-c", "commit.gpgsign=false", *args]) is None:
        raise DocsDriftError("The sandbox could not record your edit.", "Retry the check.")


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", message)


def _without_overlaps(body: dict[str, Any]) -> dict[str, Any]:
    """Prefer the narrowest certain fix when two findings would edit the same span.

    A renamed flag inside a documented command is reported twice: as an invalid command (whose fix
    drops the flag) and as the rename (whose fix replaces it). Replacing is the better fix.
    """
    rows = body["findings"]

    def span(edit: dict[str, Any]) -> tuple[int, int, int]:
        return edit["line"], edit["column"], edit["column"] + len(edit["old_text"])

    for row in rows:
        edit = row.get("edit")
        if not edit or not row["is_drift"]:
            continue
        line, start, end = span(edit)
        for other in rows:
            theirs = other.get("edit")
            if other is row or not theirs or not theirs["certain"] or not other["is_drift"]:
                continue
            o_line, o_start, o_end = span(theirs)
            if o_line == line and start <= o_start and o_end <= end and (o_end - o_start) < (end - start):
                row["edit"] = None
                row["fixed_by"] = theirs["new_text"]
                break
    return body


def check_sandbox(base: Path, code: str, readme: str, previous_code: str | None, ticket: Ticket) -> dict[str, Any]:
    """Write the two panes to a fresh directory, check README.md against app.py and always clean up."""
    root = base / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        (root / "pyproject.toml").write_text(PROJECT_FILE, encoding="utf-8")
        (root / DOC_FILE).write_text(readme, encoding="utf-8")
        history = previous_code is not None and previous_code != code
        if history:
            _git(root, "init", "--quiet")
            (root / CODE_FILE).write_text(previous_code or "", encoding="utf-8")
            _commit(root, "Original sample")
            ticket.check()
            (root / CODE_FILE).write_text(code, encoding="utf-8")
            _commit(root, "Your edit to app.py")
        else:
            (root / CODE_FILE).write_text(code, encoding="utf-8")
        ticket.check()
        service = SandboxService(root)
        with service._locked(ticket):
            result: ScanResult = service._document_result(DOC_FILE, readme, None, history, ticket)
            try:
                edits: list[TextEdit] = plan_rule_fixes(result, contents={DOC_FILE: readme}).edits
            except (StaleDocumentError, ValueError, OSError, IndexError):
                edits = []
            index = service._index_state(service.config).index
        if any(w.startswith("Could not process") for w in result.warnings):
            raise DocsDriftError("This text could not be parsed as Markdown.", "Simplify the formatting and retry.")
        return _without_overlaps(present(DOC_FILE, readme, result, edits, index))
    finally:
        try:
            remove_workspace(base, root)  # Also clears Git's read-only object files on Windows.
        except OSError:
            shutil.rmtree(root, ignore_errors=True)


def register_try_routes(  # noqa: PLR0913 - route factory sharing the app's closures.
    app: FastAPI,
    *,
    settings: Settings,
    templates: Jinja2Templates,
    context: Callable[..., dict[str, Any]],
    buckets: dict[str, deque[float]],
    salt: bytes,
) -> None:
    """Register /try and /api/try/check; kept apart from the scan routes in app.py."""
    base = settings.data_dir.resolve() / "try-trees"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    def limited(request: Request) -> None:
        now = time.monotonic()
        host = request.client.host if request.client else "unknown"
        key = hashlib.sha256(salt + b"try\0" + host.encode()).hexdigest()
        window = settings.write_rate_window
        for stale in [k for k, q in buckets.items() if not q or now - q[-1] > max(window, settings.rate_window)]:
            buckets.pop(stale, None)
        bucket = buckets[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= settings.write_rate_limit or len(buckets) > MAX_IP_BUCKETS:
            raise HTTPException(
                429,
                "Too many checks in a short time. Pause for a moment; your edits are kept.",
                headers={"Retry-After": str(window)},
            )
        bucket.append(now)

    @app.get("/try", response_class=HTMLResponse)
    async def try_page(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "try.html",
            context(request, sample_code=SAMPLE_CODE, sample_readme=SAMPLE_README, max_text_bytes=MAX_TRY_TEXT_BYTES),
        )

    @app.post(CHECK_PATH)
    async def try_check(payload: TryCheckRequest, request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and origin != settings.allowed_origin:
            raise HTTPException(403, "Check the sandbox from the demo's own page.")
        limited(request)
        for value in (payload.code, payload.readme, payload.previous_code or ""):
            if len(value.encode("utf-8")) > MAX_TRY_TEXT_BYTES:
                raise HTTPException(413, f"Each pane accepts up to {MAX_TRY_TEXT_BYTES // 1000} KB.")
        ticket = Ticket()
        async with gate:
            try:
                body = await asyncio.wait_for(
                    asyncio.to_thread(check_sandbox, base, payload.code, payload.readme, payload.previous_code, ticket),
                    CHECK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                ticket.cancel.set()
                raise HTTPException(503, "The check took too long. Try shorter code or docs.") from None
            except DocsDriftError:
                raise HTTPException(
                    422, "These files could not be checked. Keep app.py as Python and README.md as Markdown."
                ) from None
        return {"note": NOTE, **body}
