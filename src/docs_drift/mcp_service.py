"""Agent-facing drift checks with bounded, structured results.

This module is transport-free so it can be tested without the optional MCP SDK.
It never imports or executes code from the inspected repository: it reuses the
same static scan, index and fix pipeline as the CLI, LSP and watch mode.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from pathspec import GitIgnoreSpec
from pydantic import BaseModel, Field

from docs_drift.config import Config, load_config
from docs_drift.docs.discover import discover_docs
from docs_drift.docs.pipeline import _head, scan_claims
from docs_drift.errors import DocsDriftError
from docs_drift.fix.edits import (
    FixPlan,
    StaleDocumentError,
    TextEdit,
    apply_file,
    decode,
    render_file,
    unified_patch,
)
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.history import _run as _git
from docs_drift.ids import normalize_repo_path
from docs_drift.index import build_index
from docs_drift.index.base import CodeIndex, source_files
from docs_drift.limits import MAX_DOC_CHARS, MAX_PATH_CHARS
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import ClaimKind, Finding, Package, ScanResult, Verdict
from docs_drift.ownership import coverage_report
from docs_drift.report.common import RULES, health, message
from docs_drift.scanning import package_indexes, run_scan, scoped_findings
from docs_drift.support import ECOSYSTEMS
from docs_drift.workspaces import detect_packages, package_for

DOC_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".rst", ".txt", ".text"})
MAX_FINDINGS_LIMIT = 200
DEFAULT_MAX_FINDINGS = 50
MAX_ID_ARGUMENTS = 50
MAX_GLOBS = 50
MIN_ID_HEX = 6
SHORT_ID_CHARS = 16
MAX_EVIDENCE_ITEMS = 3
MAX_EXPLAIN_EVIDENCE = 10
MAX_TEXT_CHARS = 240
MAX_REASONS = 6
MAX_WARNINGS = 10
MAX_DIFF_CHARS = 30_000
MAX_RECENT_FINDINGS = 500
MAX_UNTRACKED_PATHS = 20_000
LOCK_POLL_SECONDS = 0.05
_ID_PREFIX = "claim_"

ProgressCallback = Callable[[int, int, str], None]
Origin = Literal["repository", "document", "unsaved_text"]


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Mask secret-bearing lines and bound text so responses stay small for model context."""
    masked = mask_secrets(text)
    return masked if len(masked) <= limit else masked[: limit - 1] + "…"


class EvidenceOut(BaseModel):
    """One piece of supporting evidence from documentation, source or Git history."""

    file: str = Field(description="Repository-relative file path.")
    line: int = Field(description="1-based line number.")
    text: str = Field(description="Evidence excerpt, truncated and secret-masked.")


class CommitOut(BaseModel):
    """The commit that most likely made the documentation stale."""

    sha: str = Field(description="Abbreviated commit SHA.")
    summary: str
    date: str = Field(description="ISO 8601 commit date.")


class FindingOut(BaseModel):
    """A documented claim and the verdict static analysis reached for it."""

    id: str = Field(description="Finding id prefix; pass it to explain_finding, preview_fix or apply_fix.")
    verdict: Verdict = Field(
        description="ok, missing, renamed, default_changed, invalid_command (all four are drift) or unverifiable."
    )
    is_drift: bool = Field(description="True when the documentation contradicts the code.")
    kind: ClaimKind = Field(description="What the documentation claims exists (flag, env var, endpoint, ...).")
    location: str = Field(description="doc_path:line of the claim.")
    doc_path: str
    line: int
    column: int
    claim: str = Field(description="The documented value, for example --port or APP_DEBUG.")
    doc_line: str | None = Field(default=None, description="The documentation line containing the claim.")
    message: str = Field(description="One-sentence human-readable explanation.")
    suggestion: str | None = Field(default=None, description="Replacement the code supports, when known.")
    confidence: float = Field(description="0-1 confidence in the verdict.")
    evidence: list[EvidenceOut] = Field(default_factory=list)
    introduced_by: CommitOut | None = Field(default=None, description="Commit that changed the code, when known.")
    package: str | None = Field(default=None, description="Workspace package that owns the document.")


class SuggestedEdit(BaseModel):
    """An exact, single-span documentation edit produced by the conservative rule fixer."""

    finding_id: str
    doc_path: str
    line: int
    column: int = Field(description="1-based column where old_text starts.")
    old_text: str
    new_text: str
    reason: str


class CheckSummary(BaseModel):
    docs_scanned: int
    claims_checked: int
    drift: int = Field(description="Findings whose documentation contradicts the code (all confidences).")
    by_verdict: dict[str, int] = Field(description="Counts for every verdict before filtering.")
    health_score: int = Field(description="0-100 share of checkable claims that are not drift.")
    returned: int = Field(description="Findings included in this response.")
    hidden_below_confidence: int = Field(description="Matching findings hidden by min_confidence.")
    truncated: bool = Field(description="True when max_findings cut the list; narrow with paths to see more.")


class CheckResult(BaseModel):
    """Result of check_docs."""

    repo: str = Field(description="Absolute repository root that was checked.")
    head_sha: str
    summary: CheckSummary
    findings: list[FindingOut]
    warnings: list[str] = Field(default_factory=list)
    cached: bool = Field(description="True when an unchanged repository reused the previous scan.")
    next_step: str = Field(description="Suggested follow-up for the agent.")


class DocumentCheckResult(CheckResult):
    """Result of check_document or check_text."""

    path: str = Field(description="Repository-relative path the text was checked as.")
    unsaved: bool = Field(description="True when the text came from the request rather than disk.")
    suggested_edits: list[SuggestedEdit] = Field(
        default_factory=list, description="Certain rule-based edits against the checked text."
    )


class SymbolOut(BaseModel):
    kind: str
    name: str
    qualified_name: str
    file: str
    line: int
    default: str | None = None


class ExplainResult(BaseModel):
    """Full evidence for one finding."""

    full_id: str
    finding: FindingOut
    rule: str = Field(description="Stable rule code, for example DD002.")
    reasons: list[str]
    decided_by: str
    matched_symbol: SymbolOut | None = None
    source: Origin = Field(
        description="Which call issued the id: repository (check_docs, current files on disk), "
        "document (check_document) or unsaved_text (check_text)."
    )
    auto_fixable: bool = Field(description="True when preview_fix can produce a certain edit.")


class FixResult(BaseModel):
    """Result of preview_fix or apply_fix."""

    applied: bool = Field(description="True only when apply_fix wrote files.")
    diff: str = Field(description="Unified diff of the edits (git-applyable).")
    diff_truncated: bool
    files: list[str]
    edits: list[SuggestedEdit]
    not_auto_fixable: list[str] = Field(
        default_factory=list, description="Findings without a certain edit, with review notes."
    )
    plan_token: str = Field(description="Pass to apply_fix as plan_token so it writes exactly this diff or nothing.")
    written: list[str] = Field(default_factory=list, description="Files apply_fix actually wrote.")
    skipped: list[str] = Field(
        default_factory=list, description="Files apply_fix did not write (cancelled or changed on disk)."
    )


class CoverageRow(BaseModel):
    path: str
    status: str = Field(description="checked, no-claims or not-checked.")
    claims: int
    drift: int
    unverifiable: int
    without_source_match: int
    owners: list[str]
    important: bool


class CoverageResult(BaseModel):
    head_sha: str
    documents: list[CoverageRow]
    warnings: list[str]
    limits: str


class SurfaceRow(BaseModel):
    language: str
    surface: str
    support: str


class SurfacesResult(BaseModel):
    claim_kinds: list[str] = Field(description="Kinds of documentation statements docs-drift extracts.")
    verdicts: dict[str, str] = Field(description="Verdict name to meaning.")
    ecosystems: list[SurfaceRow]
    notes: str


@dataclass
class _IndexState:
    key: str
    index: CodeIndex
    packages: list[Package]
    indexes: dict[str, CodeIndex]


def short_id(finding: Finding) -> str:
    return finding.claim.claim_id[:SHORT_ID_CHARS]


def finding_out(finding: Finding, *, evidence_limit: int = MAX_EVIDENCE_ITEMS) -> FindingOut:
    claim = finding.claim
    commit = finding.introduced_by
    return FindingOut(
        id=short_id(finding),
        verdict=finding.verdict,
        is_drift=finding.is_drift,
        kind=claim.kind,
        location=f"{claim.doc_path}:{claim.line}",
        doc_path=claim.doc_path,
        line=claim.line,
        column=claim.column,
        claim=_clip(claim.value),
        doc_line=_clip(finding.doc_text.strip(), 300) if finding.doc_text else None,
        message=_clip(message(finding), 400),
        suggestion=_clip(finding.suggestion) if finding.suggestion else None,
        confidence=round(finding.confidence, 3),
        evidence=[EvidenceOut(file=e.file, line=e.line, text=_clip(e.text)) for e in finding.evidence[:evidence_limit]],
        introduced_by=CommitOut(sha=commit.short_sha, summary=_clip(commit.summary, 160), date=commit.date.isoformat())
        if commit
        else None,
        package=finding.package,
    )


def _edit_out(edit: TextEdit) -> SuggestedEdit:
    return SuggestedEdit(
        finding_id=edit.claim_id[:SHORT_ID_CHARS],
        doc_path=edit.file,
        line=edit.line,
        column=edit.column,
        old_text=edit.old_text,
        new_text=edit.new_text,
        reason=edit.reason,
    )


def _bounded_warnings(items: Iterable[str]) -> list[str]:
    return [_clip(item, 300) for item in list(dict.fromkeys(items))[:MAX_WARNINGS]]


def normalize_finding_id(value: str) -> str:
    text = value.strip().lower()
    if not text.startswith(_ID_PREFIX):
        text = _ID_PREFIX + text
    digits = text.removeprefix(_ID_PREFIX)
    if len(digits) < MIN_ID_HEX or any(c not in "0123456789abcdef" for c in digits):
        raise DocsDriftError(
            f"{value!r} is not a finding id.",
            f"Use an id from check_docs, such as claim_0123456789 (at least {MIN_ID_HEX} hex digits).",
        )
    return text


class OperationCancelledError(DocsDriftError):
    """The caller's deadline passed or the request was cancelled; the operation stopped cooperatively."""


class ServiceBusyError(DocsDriftError):
    """An earlier operation on the same repository still holds the lock."""


@dataclass
class Ticket:
    """Per-call cancellation flag shared between the transport and the worker thread."""

    cancel: threading.Event = field(default_factory=threading.Event)
    waiting: bool = False

    def check(self) -> None:
        if self.cancel.is_set():
            raise OperationCancelledError("The operation was cancelled before it finished.")


@dataclass
class _Issued:
    """A finding as returned by a specific tool call, so later calls can resolve its id faithfully."""

    finding: Finding
    origin: Origin
    path: str


def plan_token(plan: FixPlan) -> str:
    """Bind a fix to the exact edits and scanned document contents shown in a preview."""
    files = sorted({edit.file for edit in plan.edits})
    payload = {
        "files": [[file, plan.hashes.get(file, "")] for file in files],
        "edits": sorted(
            [e.file, e.line, e.column, e.end_line, e.end_column, e.old_text, e.new_text] for e in plan.edits
        ),
    }
    return "plan_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


class DriftService:
    """Cached, thread-safe drift checks for one repository root."""

    def __init__(self, repo: Path, config_path: Path | None = None) -> None:
        self.repo = repo.resolve()
        if not self.repo.is_dir():
            raise DocsDriftError(f"Repository directory does not exist: {self.repo}", "Choose an existing --repo.")
        self._config_path = config_path
        self._lock = threading.RLock()
        self._config: Config | None = None
        self._config_stamp: tuple[object, ...] | None = None
        self._index: _IndexState | None = None
        self._scan: tuple[str, bool, ScanResult] | None = None
        self._recent: OrderedDict[str, _Issued] = OrderedDict()

    # Locking and cancellation -------------------------------------------------------

    @contextmanager
    def _locked(self, ticket: Ticket | None) -> Iterator[None]:
        """Serialize work per repository without waiting past the caller's deadline."""
        if ticket is not None:
            ticket.waiting = True
        try:
            while not self._lock.acquire(timeout=LOCK_POLL_SECONDS):
                if ticket is not None and ticket.cancel.is_set():
                    raise ServiceBusyError(
                        "A previous docs-drift operation on this repository is still running.",
                        "It stops at its next checkpoint; retry in a few seconds.",
                    )
        finally:
            if ticket is not None:
                ticket.waiting = False
        try:
            if ticket is not None:
                ticket.check()
            yield
        finally:
            self._lock.release()

    # Configuration and cache keys -------------------------------------------------

    def _stamp(self) -> tuple[object, ...]:
        candidates = (
            [self._config_path] if self._config_path else [self.repo / ".docs-drift.toml", self.repo / "pyproject.toml"]
        )
        stamp: list[object] = []
        for path in candidates:
            try:
                stat = path.stat()
                stamp.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamp.append((str(path), None))
        return tuple(stamp)

    @property
    def config(self) -> Config:
        with self._lock:
            stamp = self._stamp()
            if self._config is None or stamp != self._config_stamp:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._config = load_config(self.repo, self._config_path)
                self._config_stamp = stamp
                self._index = None
                self._scan = None
            return self._config

    def _stat_digest(self, digest: hashlib._Hash, paths: Iterable[Path]) -> None:
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(f"{path.relative_to(self.repo).as_posix()}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode())

    def _source_key(self, config: Config) -> str:
        digest = hashlib.sha256()
        self._stat_digest(digest, source_files(self.repo, config.exclude))
        return digest.hexdigest()

    def _untracked_key(self, config: Config) -> str:
        """File-path claims are checked against the filesystem, so untracked files can change verdicts."""
        digest = hashlib.sha256()
        if not (self.repo / ".git").exists():
            return digest.hexdigest()
        listing = _git(self.repo, ["ls-files", "--others", "--exclude-standard", "-z"])
        if listing is None:
            # Unknown state must not reuse a possibly stale scan.
            return "unknown:" + str(time.monotonic_ns())
        # docs-drift's own cache churns on every scan; configured exclusions never affect verdicts.
        excluded = GitIgnoreSpec.from_lines([".docs-drift/", *config.exclude])
        names = [n for n in listing.stdout.split("\0") if n and not excluded.match_file(n)][:MAX_UNTRACKED_PATHS]
        self._stat_digest(digest, (self.repo / name for name in names))
        return digest.hexdigest()

    def _docs_key(self, config: Config) -> str:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            docs = discover_docs(self.repo, config)
        return hashlib.sha256("\n".join(f"{d.path}\0{d.content_hash}" for d in docs).encode()).hexdigest()

    def invalidate(self) -> None:
        with self._lock:
            self._index = None
            self._scan = None

    # Pipelines ----------------------------------------------------------------------

    def _index_state(self, config: Config) -> _IndexState:
        key = self._source_key(config)
        if self._index is not None and self._index.key == key:
            return self._index
        # build_index keeps its own per-file cache in .docs-drift/cache; this layer
        # avoids re-reading and re-hashing every source file for unchanged trees.
        index = build_index(self.repo, config)
        packages = detect_packages(self.repo, [self.repo / p for p in index.source_paths])
        self._index = _IndexState(key, index, packages, package_indexes(index, packages))
        return self._index

    def full_scan(
        self, *, history: bool = True, progress: ProgressCallback | None = None, ticket: Ticket | None = None
    ) -> tuple[ScanResult, bool]:
        """Return a scan of the files on disk and whether it came from the in-memory cache."""
        with self._locked(ticket):
            config = self.config
            key = "\0".join(
                (self._source_key(config), self._docs_key(config), self._untracked_key(config), _head(self.repo))
            )
            if self._scan is not None and self._scan[0] == key and (self._scan[1] or not history):
                return self._scan[2], True
            steps = {"indexing": 1, "matching": 2, "history": 3}

            def stage(name: str, counts: dict[str, int]) -> None:
                # Cooperative cancellation: a timed-out scan stops here and releases the lock.
                if ticket is not None:
                    ticket.check()
                if progress is not None:
                    detail = ", ".join(f"{k}={v}" for k, v in counts.items())
                    progress(steps.get(name, 0), 4, f"{name} ({detail})")

            result = run_scan(self.repo, config, history=history, stage_callback=stage)
            if ticket is not None:
                ticket.check()
            if progress is not None:
                progress(4, 4, "done")
            self._scan = (key, history, result)
            return result, False

    def _enrich(self, findings: list[Finding], state: _IndexState, config: Config) -> tuple[list[Finding], list[str]]:
        from docs_drift.cli import _full_findings  # noqa: PLC0415 - shared history enrichment lives with the CLI.

        notes: list[str] = []
        enriched: dict[str, Finding] = {}
        for package in state.packages:
            group = [f for f in findings if package_for(f.claim.doc_path, state.packages).root == package.root]
            if not group:
                continue
            items, warnings_out = _full_findings(
                [f.claim for f in group],
                state.indexes[package.root],
                self.repo,
                initial=group,
                history_config=config.history,
            )
            enriched.update((f.claim.claim_id, f) for f in items)
            notes.extend(warnings_out)
        return [enriched.get(f.claim.claim_id, f) for f in findings], notes

    def _remember(self, findings: Sequence[Finding], origin: Origin) -> None:
        for finding in findings:
            key = finding.claim.claim_id
            self._recent[key] = _Issued(finding, origin, finding.claim.doc_path)
            self._recent.move_to_end(key)
        while len(self._recent) > MAX_RECENT_FINDINGS:
            self._recent.popitem(last=False)

    def _issued(self, value: str) -> _Issued | None:
        prefix = normalize_finding_id(value)
        with self._lock:
            matches = [item for key, item in self._recent.items() if key.startswith(prefix)]
        if len(matches) > 1:
            raise DocsDriftError(f"Finding id {value!r} is ambiguous ({len(matches)} matches).", "Use a longer prefix.")
        return matches[0] if matches else None

    # Path safety --------------------------------------------------------------------

    def document_path(self, value: str, *, must_exist: bool) -> tuple[Path, str]:
        """Resolve a user-supplied document path strictly inside the repository root."""
        text = value.strip()
        if not text or len(text) > MAX_PATH_CHARS or "\0" in text:
            raise DocsDriftError("Use a non-empty documentation path.", f"Paths are limited to {MAX_PATH_CHARS} chars.")
        candidate = Path(text)
        absolute = candidate if candidate.is_absolute() else self.repo / candidate
        resolved = absolute.resolve()
        if not resolved.is_relative_to(self.repo):
            raise DocsDriftError(
                f"Refusing to read {value!r}: it resolves outside the repository root {self.repo}.",
                "Pass a path inside the repository, for example README.md or docs/usage.md.",
            )
        relative = resolved.relative_to(self.repo).as_posix()
        try:
            relative = normalize_repo_path(relative)
        except ValueError as exc:
            raise DocsDriftError(str(exc), "Pass a documentation file path, not the repository root.") from None
        parts = PurePosixPath(relative).parts
        if ".git" in parts or ".docs-drift" in parts:
            raise DocsDriftError(
                f"Refusing to check {relative}: it is internal metadata.", "Choose a documentation file."
            )
        if resolved.suffix.casefold() not in DOC_SUFFIXES:
            raise DocsDriftError(
                f"{relative} is not a documentation file.", "Supported suffixes: " + ", ".join(sorted(DOC_SUFFIXES))
            )
        if must_exist:
            if absolute.is_symlink() or not resolved.is_file():
                raise DocsDriftError(
                    f"{relative} is not a regular file in the repository.",
                    "Use check_text to check content that is not saved yet; symbolic links are not followed.",
                )
            if resolved.stat().st_size > MAX_DOC_CHARS:
                raise DocsDriftError(f"{relative} is larger than the {MAX_DOC_CHARS:,}-byte document limit.")
        return resolved, relative

    def _read_saved(self, path: str) -> tuple[str, str, bytes]:
        resolved, relative = self.document_path(path, must_exist=True)
        raw = resolved.read_bytes()
        try:
            return relative, decode(raw)[0], raw
        except UnicodeError as exc:
            raise DocsDriftError(f"{relative} is not valid UTF-8 or UTF-16 text: {exc}.") from None

    # Tool operations ----------------------------------------------------------------

    def check_docs(  # noqa: PLR0913 - mirrors the tool's optional filters.
        self,
        *,
        paths: Sequence[str] | None = None,
        min_confidence: float | None = None,
        include_ok: bool = False,
        include_unverifiable: bool = False,
        max_findings: int = DEFAULT_MAX_FINDINGS,
        history: bool = True,
        progress: ProgressCallback | None = None,
        ticket: Ticket | None = None,
    ) -> CheckResult:
        spec = self._globs(paths)
        result, cached = self.full_scan(history=history, progress=progress, ticket=ticket)
        findings = [f for f in result.findings if spec is None or spec.match_file(f.claim.doc_path)]
        docs = [d for d in result.docs if spec is None or spec.match_file(d.path)]
        claims = [c for c in result.claims if spec is None or spec.match_file(c.doc_path)]
        packaged, selected = self._package(
            result,
            findings,
            len(docs),
            len(claims),
            cached,
            min_confidence,
            include_ok,
            include_unverifiable,
            max_findings,
        )
        with self._lock:
            self._remember(selected, "repository")
        return packaged

    def check_document(
        self,
        path: str,
        *,
        history: bool = True,
        include_ok: bool = False,
        min_confidence: float | None = None,
        ticket: Ticket | None = None,
    ) -> DocumentCheckResult:
        relative, text, raw = self._read_saved(path)
        return self._check_contents(relative, text, raw, history, include_ok, min_confidence, ticket)

    def check_text(  # noqa: PLR0913 - mirrors the tool's optional filters.
        self,
        path: str,
        text: str,
        *,
        history: bool = True,
        include_ok: bool = False,
        min_confidence: float | None = None,
        ticket: Ticket | None = None,
    ) -> DocumentCheckResult:
        if len(text) > MAX_DOC_CHARS:
            raise DocsDriftError(f"The text exceeds the {MAX_DOC_CHARS:,}-character document limit.")
        _, relative = self.document_path(path, must_exist=False)
        return self._check_contents(relative, text, None, history, include_ok, min_confidence, ticket)

    def _document_result(
        self, relative: str, text: str, raw: bytes | None, history: bool, ticket: Ticket | None
    ) -> ScanResult:
        """Check one document's text in memory, as the language server does; nothing is written."""
        config = self.config
        partial = scan_claims(self.repo, config, doc_paths={relative}, contents={relative: text})
        if ticket is not None:
            ticket.check()
        state = self._index_state(config)
        findings, notes = scoped_findings(
            partial.claims, state.index, self.repo, state.packages, history=False, indexes=state.indexes
        )
        if history and any(f.is_drift or f.verdict == Verdict.unverifiable for f in findings):
            if ticket is not None:
                ticket.check()
            findings, more = self._enrich(findings, state, config)
            notes.extend(more)
        lines = text.splitlines()
        for finding in findings:
            if finding.claim.line <= len(lines):
                finding.doc_text = lines[finding.claim.line - 1]
        docs = partial.docs
        if raw is not None:
            # Saved documents are planned against their exact on-disk bytes (BOMs included).
            digest = hashlib.sha256(raw).hexdigest()
            docs = [d.model_copy(update={"content_hash": digest}) if d.path == relative else d for d in docs]
        return partial.model_copy(
            update={
                "docs": docs,
                "findings": findings,
                "symbols": state.index.symbols,
                "packages": state.packages,
                "warnings": [*partial.warnings, *notes],
            }
        )

    def _check_contents(  # noqa: PLR0913, PLR0917 - internal shared path for both document tools.
        self,
        relative: str,
        text: str,
        raw: bytes | None,
        history: bool,
        include_ok: bool,
        min_confidence: float | None,
        ticket: Ticket | None,
    ) -> DocumentCheckResult:
        unsaved = raw is None
        with self._locked(ticket):
            result = self._document_result(relative, text, raw, history, ticket)
            edits: list[SuggestedEdit] = []
            try:
                plan = plan_rule_fixes(result, contents=None if raw is not None else {relative: text})
                edits = [_edit_out(edit) for edit in plan.edits]
            except (StaleDocumentError, ValueError, OSError, IndexError):
                pass
            self._remember(result.findings, "unsaved_text" if unsaved else "document")
        base, _ = self._package(
            result,
            result.findings,
            len(result.docs),
            len(result.claims),
            False,
            min_confidence,
            include_ok,
            True,
            MAX_FINDINGS_LIMIT,
            next_step=(
                "Apply suggested_edits in the editor or rewrite the flagged statements, then call check_text again."
                if unsaved and any(f.is_drift for f in result.findings)
                else "Use preview_fix with the finding ids for a reviewable diff of certain edits."
                if any(f.is_drift for f in result.findings)
                else "No drift found in this text."
            ),
        )
        return DocumentCheckResult(
            **base.model_dump(), path=relative, unsaved=unsaved, suggested_edits=edits[:MAX_FINDINGS_LIMIT]
        )

    def _globs(self, paths: Sequence[str] | None) -> GitIgnoreSpec | None:
        if not paths:
            return None
        if len(paths) > MAX_GLOBS or any(not p.strip() or len(p) > MAX_PATH_CHARS or "\0" in p for p in paths):
            raise DocsDriftError("Use up to 50 non-empty documentation globs.", "For example: README.md, docs/**/*.md")
        return GitIgnoreSpec.from_lines([p.strip().replace("\\", "/") for p in paths])

    def _package(  # noqa: PLR0913, PLR0917 - result assembly from one place.
        self,
        result: ScanResult,
        findings: list[Finding],
        docs: int,
        claims: int,
        cached: bool,
        min_confidence: float | None,
        include_ok: bool,
        include_unverifiable: bool,
        max_findings: int,
        *,
        next_step: str | None = None,
    ) -> tuple[CheckResult, list[Finding]]:
        threshold = result.config_used.min_confidence if min_confidence is None else min_confidence
        by_verdict = dict.fromkeys((v.value for v in Verdict), 0)
        for finding in findings:
            by_verdict[finding.verdict.value] += 1
        wanted = [
            f
            for f in findings
            if f.is_drift
            or (include_ok and f.verdict == Verdict.ok)
            or (include_unverifiable and f.verdict == Verdict.unverifiable)
        ]
        # The threshold gates drift; ok and unverifiable claims appear only when explicitly requested.
        visible = [f for f in wanted if f.confidence >= threshold or not f.is_drift]
        visible.sort(key=lambda f: (not f.is_drift, f.claim.doc_path, f.claim.line, f.claim.claim_id))
        selected = visible[:max_findings]
        drift = sum(f.is_drift for f in findings)
        hidden = len(wanted) - len(visible)
        if next_step is None:
            next_step = (
                "Call explain_finding for detail, then preview_fix for certain edits; apply_fix once the user agrees."
                if any(f.is_drift for f in selected)
                else f"{hidden} lower-confidence findings are hidden; lower min_confidence to review them."
                if hidden
                else "Documentation matches the code for every checked claim."
            )
        packaged = CheckResult(
            repo=str(self.repo),
            head_sha=result.head_sha,
            summary=CheckSummary(
                docs_scanned=docs,
                claims_checked=claims,
                drift=drift,
                by_verdict=by_verdict,
                health_score=health(findings),
                returned=len(selected),
                hidden_below_confidence=hidden,
                truncated=len(visible) > len(selected),
            ),
            findings=[finding_out(f) for f in selected],
            warnings=_bounded_warnings(result.warnings),
            cached=cached,
            next_step=next_step,
        )
        return packaged, selected

    def _resolve(self, findings: Sequence[Finding], value: str) -> Finding | None:
        prefix = normalize_finding_id(value)
        matches = [f for f in findings if f.claim.claim_id.startswith(prefix)]
        if len(matches) > 1:
            raise DocsDriftError(f"Finding id {value!r} is ambiguous ({len(matches)} matches).", "Use a longer prefix.")
        return matches[0] if matches else None

    def explain(self, finding_id: str, *, ticket: Ticket | None = None) -> ExplainResult:
        issued = self._issued(finding_id)
        finding: Finding | None = None
        source: Origin = "repository"
        result: ScanResult | None = None
        if issued is not None and issued.origin != "repository":
            # Claim ids ignore line numbers: explain the finding exactly as the issuing call returned it.
            finding, source = issued.finding, issued.origin
        else:
            result, _ = self.full_scan(history=True, ticket=ticket)
            finding = self._resolve(result.findings, finding_id)
        if finding is None:
            raise DocsDriftError(
                f"No current finding matches {finding_id!r}.",
                "The documentation or code may have changed; run check_docs again for fresh ids.",
            )
        fixable = False
        if result is not None:
            try:
                fixable = any(e.claim_id == finding.claim.claim_id for e in plan_rule_fixes(result).edits)
            except (StaleDocumentError, ValueError, OSError, IndexError):
                fixable = False
        elif source == "document":
            try:
                with self._locked(ticket):
                    plan, _ = self._plan([finding.claim.claim_id], ticket)
                fixable = bool(plan.edits)
            except (DocsDriftError, StaleDocumentError, ValueError, OSError, IndexError):
                fixable = False
        symbol = finding.matched_symbol
        return ExplainResult(
            full_id=finding.claim.claim_id,
            finding=finding_out(finding, evidence_limit=MAX_EXPLAIN_EVIDENCE),
            rule=RULES[finding.verdict.value][0],
            reasons=[_clip(r, 400) for r in finding.reasons[:MAX_REASONS]],
            decided_by=finding.decided_by.value,
            matched_symbol=SymbolOut(
                kind=symbol.kind.value,
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                file=symbol.file,
                line=symbol.line,
                default=symbol.default,
            )
            if symbol
            else None,
            source=source,
            auto_fixable=fixable,
        )

    def _plan(self, finding_ids: Sequence[str], ticket: Ticket | None) -> tuple[FixPlan, list[str]]:
        """Plan certain edits for findings on disk: configured documents or explicitly checked saved files."""
        if not finding_ids or len(finding_ids) > MAX_ID_ARGUMENTS:
            raise DocsDriftError(f"Pass between 1 and {MAX_ID_ARGUMENTS} finding ids.", "Get ids from check_docs.")
        result, _ = self.full_scan(history=True, ticket=ticket)
        groups: dict[str, tuple[ScanResult, list[Finding]]] = {}
        for value in finding_ids:
            finding = self._resolve(result.findings, value)
            if finding is not None:
                groups.setdefault("", (result, []))[1].append(finding)
                continue
            issued = self._issued(value)
            if issued is not None and issued.origin == "document":
                # A saved file outside the configured globs: re-check it from disk and plan from that result.
                relative, text, raw = self._read_saved(issued.path)
                if relative not in groups:
                    groups[relative] = (self._document_result(relative, text, raw, True, ticket), [])
                finding = self._resolve(groups[relative][0].findings, value)
                if finding is not None:
                    groups[relative][1].append(finding)
                    continue
            raise DocsDriftError(
                f"No current finding on disk matches {value!r}.",
                "This finding came from check_text on unsaved content; apply its suggested_edits in the editor."
                if issued is not None and issued.origin == "unsaved_text"
                else "Run check_docs or check_document again; the documentation or code may have changed.",
            )
        edits: list[TextEdit] = []
        hashes: dict[str, str] = {}
        notes: list[str] = []
        for source, selected in groups.values():
            plan = plan_rule_fixes(source)
            wanted = {f.claim.claim_id for f in selected}
            chosen = [edit for edit in plan.edits if edit.claim_id in wanted]
            edits.extend(chosen)
            hashes.update({edit.file: plan.hashes[edit.file] for edit in chosen})
            covered = {edit.claim_id for edit in chosen}
            for finding in selected:
                if finding.claim.claim_id in covered:
                    continue
                reason = (
                    "it is not drift"
                    if not finding.is_drift
                    else "no certain rule edit; rewrite or remove the statement after review"
                )
                notes.append(f"{short_id(finding)} ({finding.claim.doc_path}:{finding.claim.line}): {reason}.")
        unique = {(e.file, e.line, e.column, e.end_column, e.new_text): e for e in edits}
        return FixPlan(edits=list(unique.values()), hashes=hashes), notes

    def _result(
        self, plan: FixPlan, notes: list[str], *, written: Sequence[str] = (), skipped: Sequence[str] = ()
    ) -> FixResult:
        diff = unified_patch(self.repo, plan) if plan.edits else ""
        return FixResult(
            applied=bool(written),
            diff=diff[:MAX_DIFF_CHARS],
            diff_truncated=len(diff) > MAX_DIFF_CHARS,
            files=sorted({edit.file for edit in plan.edits}),
            edits=[_edit_out(edit) for edit in plan.edits],
            not_auto_fixable=_bounded_warnings(notes),
            plan_token=plan_token(plan),
            written=list(written),
            skipped=list(skipped),
        )

    def preview_fix(self, finding_ids: Sequence[str], *, ticket: Ticket | None = None) -> FixResult:
        with self._locked(ticket):
            try:
                plan, notes = self._plan(finding_ids, ticket)
                return self._result(plan, notes)
            except StaleDocumentError:
                # Read-only: a fresh rescan is always safe here.
                self.invalidate()
            try:
                plan, notes = self._plan(finding_ids, ticket)
                return self._result(plan, notes)
            except StaleDocumentError:
                raise DocsDriftError("Documentation is changing while the preview is built.", "Retry.") from None

    def apply_fix(
        self,
        finding_ids: Sequence[str],
        *,
        expected_plan_token: str | None = None,
        rescan_if_stale: bool = False,
        ticket: Ticket | None = None,
    ) -> FixResult:
        """Write certain edits; never writes content the caller has not seen when a plan token is given."""
        with self._locked(ticket):
            try:
                plan, notes = self._plan(finding_ids, ticket)
            except StaleDocumentError:
                if expected_plan_token is not None or not rescan_if_stale:
                    raise DocsDriftError(
                        "Documentation changed after it was scanned; nothing was written.",
                        "Run preview_fix again and review the new diff.",
                    ) from None
                self.invalidate()
                plan, notes = self._plan(finding_ids, ticket)
            if expected_plan_token is not None and plan_token(plan) != expected_plan_token:
                raise DocsDriftError(
                    "The documentation or the proposed edits changed since preview_fix; nothing was written.",
                    "Run preview_fix again and review the new diff before applying it.",
                )
            files = sorted({edit.file for edit in plan.edits})
            try:
                # Verify every file (hash and exact spans) before writing the first one.
                for relative in files:
                    render_file(self.repo, plan, relative)
                result = self._result(plan, notes)
            except StaleDocumentError as exc:
                self.invalidate()
                raise DocsDriftError(f"{exc} Nothing was written.", "Run preview_fix again.") from None
            written: list[str] = []
            skipped: list[str] = []
            for relative in files:
                if ticket is not None and ticket.cancel.is_set():
                    skipped.append(relative)
                    continue
                try:
                    # apply_file re-checks the hash and replaces atomically; paths never leave the repository.
                    apply_file(self.repo, plan, relative)
                    written.append(relative)
                except StaleDocumentError:
                    skipped.append(relative)
            if written:
                self.invalidate()
            if files and not written and ticket is not None and ticket.cancel.is_set():
                raise OperationCancelledError(
                    "The fix was cancelled before any file was written; nothing was written.",
                    "Call apply_fix again (with the same plan_token) to retry.",
                )
            return result.model_copy(update={"applied": bool(written), "written": written, "skipped": skipped})

    def coverage(self, important: Sequence[str] | None = None, *, ticket: Ticket | None = None) -> CoverageResult:
        if important:
            for path in important:
                self.document_path(path, must_exist=False)
        result, _ = self.full_scan(history=False, ticket=ticket)
        report = coverage_report(result, list(important or []))
        return CoverageResult(
            head_sha=str(report["head_sha"]),
            documents=[CoverageRow.model_validate(row) for row in report["documents"][:MAX_FINDINGS_LIMIT]],
            warnings=_bounded_warnings(report["warnings"]),
            limits=str(report["limits"]),
        )


def supported_surfaces() -> SurfacesResult:
    return SurfacesResult(
        claim_kinds=[kind.value for kind in ClaimKind],
        verdicts={name: meaning for name, (_, _, meaning) in RULES.items()},
        ecosystems=[SurfaceRow(language=lang, surface=surface, support=level) for lang, surface, level in ECOSYSTEMS],
        notes="Analysis is static: target code is parsed, never imported or executed. 'partial' support means "
        "common declaration forms are recognized; unsupported surfaces produce unverifiable findings, not drift.",
    )
