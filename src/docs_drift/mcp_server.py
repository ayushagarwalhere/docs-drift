"""Model Context Protocol server: docs-drift tools for AI coding agents.

stdout is reserved for the protocol. Logs go to stderr. Every tool runs the
static pipeline in a worker thread with a timeout, so a slow scan never blocks
the session, and failures become MCP tool errors with an actionable message.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import Field

from docs_drift import __version__
from docs_drift.errors import DocsDriftError

try:
    import anyio
    import anyio.from_thread
    import anyio.to_thread
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
    from mcp.types import ToolAnnotations
except ImportError:
    raise DocsDriftError(
        "MCP support is not installed.",
        'Install it with pip install "docs-drift[mcp]" (requires the mcp package 2.3 or newer), '
        'or run uvx --from "docs-drift[mcp]" docs-drift mcp.',
    ) from None

from docs_drift.fix.edits import StaleDocumentError
from docs_drift.mcp_service import (
    DEFAULT_MAX_FINDINGS,
    MAX_FINDINGS_LIMIT,
    MAX_ID_ARGUMENTS,
    CheckResult,
    CoverageResult,
    DocumentCheckResult,
    DriftService,
    ExplainResult,
    FixResult,
    OperationCancelledError,
    ProgressCallback,
    SurfacesResult,
    Ticket,
    finding_out,
    supported_surfaces,
)
from docs_drift.support import support_table

DEFAULT_TIMEOUT = 120.0
MAX_SERVICES = 8
_T = TypeVar("_T")
logger = logging.getLogger("docs_drift.mcp")

INSTRUCTIONS = """\
docs-drift finds documentation statements the code no longer supports: stale CLI flags and commands,
environment variables, HTTP endpoints, config keys and defaults, symbols and file paths. It is static and
evidence-backed (source index + Git history); it never runs project code.

Typical workflow:
- After changing code (renames, removed flags, new defaults), call check_docs to find stale docs.
- Before or while writing documentation, call check_text with the draft and the path it will live at.
- Use explain_finding for evidence, preview_fix for a reviewed diff, and apply_fix only when the user agrees.
Verdicts: missing, renamed, default_changed and invalid_command are drift; unverifiable means static evidence
was insufficient (not an error); ok means the claim matches the code.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

RepoArg = Annotated[
    str | None,
    Field(
        description="Repository root to check. Omit to use the server's repository. "
        "Must be inside a root the server was started with."
    ),
]
HistoryArg = Annotated[
    bool,
    Field(description="Use Git history to detect renames and the commit that made docs stale. Set false for speed."),
]
MinConfidenceArg = Annotated[
    float | None,
    Field(ge=0, le=1, description="Hide drift below this confidence (default: the repository's min_confidence)."),
]
IncludeOkArg = Annotated[bool, Field(description="Also return claims that match the code (verdict ok).")]
FindingIdsArg = Annotated[
    list[str],
    Field(
        min_length=1,
        max_length=MAX_ID_ARGUMENTS,
        description="Finding ids (or unique prefixes) from check_docs or check_document.",
    ),
]


class _Roots:
    """Allowed repository roots and one cached service per root."""

    def __init__(self, repo: Path, config_path: Path | None, extra_roots: Sequence[Path]) -> None:
        self.primary = repo.resolve()
        self.allowed = [self.primary, *(root.resolve() for root in extra_roots)]
        self.config_path = config_path
        self.services: dict[Path, DriftService] = {}

    def service(self, repo: str | None) -> DriftService:
        if repo is None or not repo.strip():
            target = self.primary
        else:
            if "\0" in repo:
                raise DocsDriftError("Use a valid repository path.")
            candidate = Path(repo.strip())
            target = (candidate if candidate.is_absolute() else self.primary / candidate).resolve()
            if not any(target.is_relative_to(root) for root in self.allowed):
                raise DocsDriftError(
                    f"Refusing to inspect {repo!r}: it is outside the allowed roots.",
                    "Allowed: " + ", ".join(str(root) for root in self.allowed) + ". Start the server with "
                    "--allow-root PATH to permit another repository.",
                )
            if not target.is_dir():
                raise DocsDriftError(f"Repository directory does not exist: {target}", "Choose an existing directory.")
        if target not in self.services:
            if len(self.services) >= MAX_SERVICES:
                self.services.pop(next(iter(self.services)))
            self.services[target] = DriftService(target, self.config_path if target == self.primary else None)
        return self.services[target]


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, DocsDriftError):
        return str(exc) + (f" Hint: {exc.hint}" if exc.hint else "")
    if isinstance(exc, StaleDocumentError):
        return f"{exc} Run the check again before fixing."
    return f"{exc}. Check the path, file permissions and configuration, then retry."


def create_server(  # noqa: PLR0915 - tool, resource and prompt registration.
    repo: Path,
    *,
    config_path: Path | None = None,
    extra_roots: Sequence[Path] = (),
    timeout: float = DEFAULT_TIMEOUT,
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING",
) -> MCPServer[Any]:
    """Build the docs-drift MCP server for a repository root without starting a transport."""
    roots = _Roots(repo, config_path, extra_roots)
    if not roots.primary.is_dir():
        raise DocsDriftError(f"Repository directory does not exist: {roots.primary}", "Choose an existing --repo.")
    server: MCPServer[Any] = MCPServer(
        "docs-drift",
        title="docs-drift",
        description="Find documentation that the code no longer supports.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/ayushagarwalhere/docs-drift",
        version=__version__,
        log_level=log_level,
    )

    def service_for(repo: str | None) -> DriftService:
        try:
            return roots.service(repo)
        except DocsDriftError as exc:
            raise ToolError(_error_text(exc)) from None

    def failure(exc: BaseException) -> ToolError:
        return ToolError(_error_text(exc))

    async def run(
        ctx: Context[Any, Any] | None,
        work: Callable[[ProgressCallback | None, Ticket], _T],
        *,
        destructive: bool = False,
    ) -> _T:
        """Run blocking analysis in a worker thread with a deadline and progress notifications.

        Read-only work is abandoned at the deadline and stops at its next checkpoint. Destructive
        work is never abandoned: the deadline (or a client cancellation) sets the ticket, the worker
        stops before its next write, and the response reports exactly what was written.
        """
        ticket = Ticket()

        def progress(step: int, total: int, message: str) -> None:
            if ctx is None:
                return
            try:
                anyio.from_thread.run(ctx.report_progress, step, total, message)
            except Exception:  # Progress is best effort; the request may already have ended.
                logger.debug("Progress notification dropped.", exc_info=True)

        def timed_out() -> ToolError:
            if ticket.waiting:
                return ToolError(
                    "A previous docs-drift operation on this repository is still running, so this call could not "
                    f"start within {timeout:g}s. It stops at its next checkpoint; retry in a few seconds."
                )
            return ToolError(
                f"docs-drift did not finish within {timeout:g}s. Narrow the check with paths, pass history=false, "
                "or restart the server with a larger --timeout."
            )

        try:
            if not destructive:
                try:
                    with anyio.fail_after(timeout):
                        return await anyio.to_thread.run_sync(partial(work, progress, ticket), abandon_on_cancel=True)
                except TimeoutError:
                    raise timed_out() from None
                finally:
                    ticket.cancel.set()  # Stops abandoned work cooperatively; harmless after completion.

            async def deadline() -> None:
                try:
                    await anyio.sleep(timeout)
                finally:
                    ticket.cancel.set()

            outcome: list[_T] = []
            errors: list[Exception] = []
            async with anyio.create_task_group() as group:
                group.start_soon(deadline)
                try:
                    # Not abandoned: the worker's outcome (written or not) is always what we report.
                    outcome.append(await anyio.to_thread.run_sync(partial(work, progress, ticket)))
                except Exception as exc:  # Re-raised below, outside the task group's exception grouping.
                    errors.append(exc)
                finally:
                    group.cancel_scope.cancel()
            if errors:
                raise errors[0]
            return outcome[0]
        except OperationCancelledError:
            if destructive:
                raise ToolError(
                    f"Stopped at the {timeout:g}s deadline before writing; nothing was written. "
                    "Call apply_fix again (with the same plan_token) to retry."
                ) from None
            raise timed_out() from None
        except (DocsDriftError, StaleDocumentError, OSError, ValueError) as exc:
            raise failure(exc) from None

    @server.tool(
        title="Check documentation drift",
        annotations=READ_ONLY,
        description=(
            "Scan the repository's documentation (README, docs/**) against the code and Git history and return "
            "statements the code no longer supports: renamed or removed CLI flags/commands, environment variables, "
            "HTTP endpoints, config keys, changed defaults, missing symbols and file paths. Use after changing code "
            "(e.g. 'is my README still accurate after this rename?'). Results are cached and refreshed when files "
            "change. Each finding has an id for explain_finding, preview_fix and apply_fix. Read-only."
        ),
    )
    async def check_docs(  # noqa: PLR0913, PLR0917 - tool parameters are the JSON schema.
        ctx: Context[Any, Any],
        repo: RepoArg = None,
        paths: Annotated[
            list[str] | None,
            Field(
                max_length=50,
                description="Only report documents matching these gitignore-style globs, e.g. ['README.md', "
                "'docs/**/*.md']. Default: every configured document.",
            ),
        ] = None,
        min_confidence: MinConfidenceArg = None,
        include_ok: IncludeOkArg = False,
        include_unverifiable: Annotated[
            bool, Field(description="Also return claims static analysis could not verify (not drift).")
        ] = False,
        max_findings: Annotated[
            int, Field(ge=1, le=MAX_FINDINGS_LIMIT, description="Maximum findings to return (drift first).")
        ] = DEFAULT_MAX_FINDINGS,
        history: HistoryArg = True,
    ) -> CheckResult:
        service = service_for(repo)
        return await run(
            ctx,
            lambda progress, ticket: service.check_docs(
                paths=paths,
                min_confidence=min_confidence,
                include_ok=include_ok,
                include_unverifiable=include_unverifiable,
                max_findings=max_findings,
                history=history,
                progress=progress,
                ticket=ticket,
            ),
        )

    @server.tool(
        title="Check one document",
        annotations=READ_ONLY,
        description=(
            "Check a single saved documentation file (Markdown, MDX, reStructuredText or text) against the code. "
            "Faster than check_docs when you only touched one file. Returns findings plus certain suggested_edits "
            "(exact old_text -> new_text replacements). Read-only."
        ),
    )
    async def check_document(
        path: Annotated[str, Field(description="Repository-relative document path, e.g. README.md or docs/cli.md.")],
        repo: RepoArg = None,
        min_confidence: MinConfidenceArg = None,
        include_ok: IncludeOkArg = False,
        history: HistoryArg = True,
    ) -> DocumentCheckResult:
        service = service_for(repo)
        return await run(
            None,
            lambda _, ticket: service.check_document(
                path, history=history, include_ok=include_ok, min_confidence=min_confidence, ticket=ticket
            ),
        )

    @server.tool(
        title="Check unsaved documentation text",
        annotations=READ_ONLY,
        description=(
            "Check Markdown/reST text you are writing or editing, before it is saved, as if it lived at `path`. "
            "Nothing is written to disk. Use it to verify a paragraph, section or whole draft mentions only flags, "
            "commands, env vars, endpoints, settings, defaults and files the code actually has. Returns findings "
            "with line numbers relative to `text` and certain suggested_edits. Read-only."
        ),
    )
    async def check_text(  # noqa: PLR0913, PLR0917 - tool parameters are the JSON schema.
        path: Annotated[
            str,
            Field(description="Where the text lives or will live, e.g. README.md. Used for package scoping."),
        ],
        text: Annotated[str, Field(description="The full document or snippet to check (Markdown or reST).")],
        repo: RepoArg = None,
        min_confidence: MinConfidenceArg = None,
        include_ok: IncludeOkArg = False,
        history: HistoryArg = True,
    ) -> DocumentCheckResult:
        service = service_for(repo)
        return await run(
            None,
            lambda _, ticket: service.check_text(
                path, text, history=history, include_ok=include_ok, min_confidence=min_confidence, ticket=ticket
            ),
        )

    @server.tool(
        title="Explain a finding",
        annotations=READ_ONLY,
        description=(
            "Return full evidence for one finding: the documented claim, matched source symbol, reasons, Git commit "
            "that introduced the drift, confidence, rule code and whether a certain automatic fix exists. Read-only."
        ),
    )
    async def explain_finding(
        finding_id: Annotated[str, Field(description="A finding id or unique prefix, e.g. claim_8ed4cec49e.")],
        repo: RepoArg = None,
    ) -> ExplainResult:
        service = service_for(repo)
        return await run(None, lambda _, ticket: service.explain(finding_id, ticket=ticket))

    @server.tool(
        title="Preview documentation fix",
        annotations=READ_ONLY,
        description=(
            "Return a unified diff of certain, exact-span documentation edits for the given finding ids, without "
            "writing anything, plus a plan_token to pass to apply_fix. Only high-confidence renames, changed "
            "defaults and removed flags are auto-fixable; others are listed in not_auto_fixable with a review note. "
            "Show the diff to the user before apply_fix."
        ),
    )
    async def preview_fix(finding_ids: FindingIdsArg, repo: RepoArg = None) -> FixResult:
        service = service_for(repo)
        return await run(None, lambda _, ticket: service.preview_fix(finding_ids, ticket=ticket))

    @server.tool(
        title="Apply documentation fix",
        annotations=WRITES,
        description=(
            "Write the edits that preview_fix showed for the given finding ids to the documentation files. Only call "
            "this after the user has reviewed the preview and agreed, and pass the preview's plan_token: the call then "
            "writes exactly that diff or nothing (if a document or the proposed edits changed, nothing is written and "
            "you must preview again). Every file is verified before the first write and replaced atomically. The "
            "result lists written and skipped files. Never touches source code."
        ),
    )
    async def apply_fix(
        finding_ids: FindingIdsArg,
        plan_token: Annotated[
            str | None,
            Field(description="plan_token from preview_fix. Strongly recommended; binds the write to that diff."),
        ] = None,
        rescan_if_stale: Annotated[
            bool,
            Field(
                description="Without a plan_token only: if a document changed during planning, rescan and write the "
                "fresh edits instead of failing. Default false (fail closed)."
            ),
        ] = False,
        repo: RepoArg = None,
    ) -> FixResult:
        service = service_for(repo)
        return await run(
            None,
            lambda _, ticket: service.apply_fix(
                finding_ids, expected_plan_token=plan_token, rescan_if_stale=rescan_if_stale, ticket=ticket
            ),
            destructive=True,
        )

    @server.tool(
        title="Documentation coverage",
        annotations=READ_ONLY,
        description=(
            "List each documentation file with its status (checked, no-claims, not-checked), claim and drift counts, "
            "and declared CODEOWNERS. Use it to see which docs docs-drift can verify. Read-only."
        ),
    )
    async def coverage(
        repo: RepoArg = None,
        important: Annotated[
            list[str] | None,
            Field(max_length=50, description="Document paths that must be checked; missing ones are reported."),
        ] = None,
    ) -> CoverageResult:
        service = service_for(repo)
        return await run(None, lambda _, ticket: service.coverage(important, ticket=ticket))

    @server.tool(
        name="supported_surfaces",
        title="Supported surfaces",
        annotations=READ_ONLY,
        description=(
            "Describe what docs-drift can verify: claim kinds, verdict meanings, and per-language framework support "
            "levels. Use it to judge whether an unverifiable finding is a tool limitation. Read-only and instant."
        ),
    )
    def supported_surfaces_tool() -> SurfacesResult:
        return supported_surfaces()

    @server.resource(
        "docs-drift://report/latest",
        name="latest-report",
        title="Latest drift report",
        description="Drift findings for the server's repository as JSON (cached; refreshed when files change).",
        mime_type="application/json",
    )
    async def latest_report() -> str:
        service = roots.service(None)  # The primary root always exists.
        ticket = Ticket()
        try:
            with anyio.fail_after(timeout):
                result, _ = await anyio.to_thread.run_sync(
                    partial(service.full_scan, history=True, ticket=ticket), abandon_on_cancel=True
                )
        except TimeoutError:
            ticket.cancel.set()
            raise ResourceError(f"docs-drift did not finish within {timeout:g}s.") from None
        except (DocsDriftError, OSError, ValueError) as exc:
            raise ResourceError(_error_text(exc)) from None
        drift = [f for f in result.findings if f.is_drift][:MAX_FINDINGS_LIMIT]
        payload = {
            "repo": str(service.repo),
            "head_sha": result.head_sha,
            "summary": result.summary()["by_verdict"],
            "findings": [finding_out(f).model_dump(mode="json") for f in drift],
        }
        return json.dumps(payload, indent=2)

    @server.resource(
        "docs-drift://supported-surfaces",
        name="supported-surfaces",
        title="Supported languages and frameworks",
        description="Markdown table of the languages and frameworks docs-drift indexes, with support levels.",
        mime_type="text/markdown",
    )
    def surfaces_resource() -> str:
        return support_table() + "\n"

    @server.prompt(
        name="review-docs-after-change",
        title="Review docs after a code change",
        description="Find and fix documentation made stale by a code change, with user approval before writing.",
    )
    def review_docs_after_change(
        change: Annotated[str, Field(description="Short summary of the code change, e.g. 'renamed --port'.")] = "",
    ) -> str:
        context = f" The change: {change}." if change else ""
        return (
            f"I just changed code in this repository.{context} Use the docs-drift tools to check the documentation:\n"
            "1. Call check_docs (drift only).\n"
            "2. For each finding related to the change, call explain_finding and summarize the evidence.\n"
            "3. Call preview_fix with the relevant finding ids and show me the diff plus any not_auto_fixable notes.\n"
            "4. Ask before calling apply_fix. For findings without an automatic fix, propose wording by hand and "
            "verify it with check_text before editing the file."
        )

    @server.prompt(
        name="write-docs-safely",
        title="Write documentation that matches the code",
        description="Draft documentation and verify every flag, env var, endpoint and setting with check_text.",
    )
    def write_docs_safely(
        path: Annotated[str, Field(description="Document to write or update, e.g. README.md.")] = "README.md",
        topic: Annotated[str, Field(description="What the documentation should cover.")] = "",
    ) -> str:
        about = f" about {topic}" if topic else ""
        return (
            f"Draft documentation{about} for {path}. Before saving, call check_text with path={path!r} and the full "
            "draft. Fix every drift finding (use suggested_edits when present) and call check_text again until no "
            "drift remains. Mention only commands, flags, environment variables, endpoints and settings the code has."
        )

    return server


def serve(  # noqa: PLR0913 - CLI options.
    repo: Path,
    *,
    config_path: Path | None = None,
    extra_roots: Sequence[Path] = (),
    transport: Literal["stdio", "streamable-http"] = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    timeout: float = DEFAULT_TIMEOUT,
    verbose: bool = False,
) -> None:
    """Start the server; blocks until the client disconnects."""
    server = create_server(
        repo,
        config_path=config_path,
        extra_roots=extra_roots,
        timeout=timeout,
        log_level="INFO" if verbose else "WARNING",
    )
    if transport == "stdio":
        server.run("stdio")
        return
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"Warning: docs-drift MCP has no authentication; binding {host} exposes repository contents.",
            file=sys.stderr,
        )
    print(f"docs-drift MCP listening on http://{host}:{port}/mcp", file=sys.stderr)
    server.run("streamable-http", host=host, port=port)
