"""Check dictated or typed documentation against a scan's saved static index.

The public demo deletes every clone right after scanning, so a scan keeps a compact copy of
its static index (symbols, source paths, packages and packaging metadata). A write check
rebuilds a *skeleton* tree from that copy -- metadata files plus empty placeholders, never
source text -- and runs the same in-memory document path as ``check_text`` in the MCP
service and the language server: extraction, matching and the conservative rule fixer are
reused unchanged. Git history is not available, so history-derived verdicts are carried
over from the saved scan of the same revision only when the claim is identical.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from heapq import nlargest
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.config import Config
from docs_drift.errors import DocsDriftError
from docs_drift.fix.edits import StaleDocumentError, TextEdit
from docs_drift.fix.rules import _edit, plan_rule_fixes
from docs_drift.ids import normalize_repo_path
from docs_drift.index.base import CodeIndex, ensure_coverage, normalize_name
from docs_drift.limits import MAX_FILE_BYTES
from docs_drift.match.rename import _KINDS as RENAME_KINDS
from docs_drift.match.rename import similarity
from docs_drift.mcp_service import DriftService, Ticket, _IndexState, finding_out
from docs_drift.models import Evidence, Finding, Package, ScanResult, Symbol, Verdict
from docs_drift.safe_files import metadata_text
from docs_drift.scanning import package_indexes

WRITE_INDEX_FILE = "write-index.json"
METADATA_FILES = ("pyproject.toml", "package.json", "setup.cfg")
MAX_WRITE_TEXT_BYTES = 20_000
MAX_WRITE_BODY_BYTES = 64_000
MAX_WRITE_INDEX_BYTES = 4_000_000
MAX_WRITE_PATHS = 20_000
MAX_METADATA_BYTES = 262_144
MAX_CACHED_TREES = 12
MAX_WRITE_FINDINGS = 100
MIN_CANDIDATE_SCORE = 65
MIN_CANDIDATE_MARGIN = 8
DEFAULT_WRITE_PATH = "docs/new.md"
HISTORY_NOTE = (
    "Checked against the saved static index of this revision. Git history is not available for text you "
    "write here, so renames are reported only from index near-misses or from the saved scan of the same claim."
)


class WriteIndex(BaseModel):
    """The minimum a write check needs once the clone is gone; no source text is kept."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    head_sha: str = Field(default="0" * 40, max_length=64)
    symbols: list[Symbol] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    files_by_language: dict[str, int] = Field(default_factory=dict)
    packages: list[Package] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    unavailable: str | None = None

    def code_index(self) -> CodeIndex:
        index = CodeIndex(
            symbols=list(self.symbols),
            files_by_language=dict(self.files_by_language),
            source_paths=list(self.source_paths),
        )
        ensure_coverage(index)
        return index


def build_write_index(
    repo: Path,
    index: CodeIndex,
    packages: list[Package],
    head_sha: str,
    *,
    scrub: Callable[[Any], Any] | None = None,
) -> WriteIndex:
    """Capture a scan's static index; ``scrub`` masks secrets exactly like the public result."""
    metadata: dict[str, str] = {}
    for name in METADATA_FILES:
        try:
            text = metadata_text(repo / name)
        except OSError:
            continue
        if len(text.encode("utf-8")) <= MAX_METADATA_BYTES:
            metadata[name] = text
    if len(index.source_paths) > MAX_WRITE_PATHS:
        return WriteIndex(
            head_sha=head_sha,
            unavailable=(
                f"This repository has {len(index.source_paths):,} source files; write checks keep indexes for up to "
                f"{MAX_WRITE_PATHS:,}. Run `docs-drift check` locally or use the VS Code extension instead."
            ),
        )
    data = {
        "head_sha": head_sha,
        "symbols": [symbol.model_dump(mode="json", exclude_defaults=True) for symbol in index.symbols],
        "source_paths": list(index.source_paths),
        "files_by_language": dict(index.files_by_language),
        "packages": [package.model_dump(mode="json") for package in packages],
        "metadata": metadata,
    }
    if scrub is not None:
        data = scrub(data)
    return WriteIndex.model_validate(data)


def dump_write_index(value: WriteIndex) -> str:
    """Serialise compactly, replacing an oversized index with an explanation instead of failing the scan."""
    text = value.model_dump_json(exclude_defaults=True)
    if len(text.encode("utf-8")) <= MAX_WRITE_INDEX_BYTES:
        return text
    return WriteIndex(
        head_sha=value.head_sha,
        unavailable=(
            f"This repository's static index is larger than the {MAX_WRITE_INDEX_BYTES // 1_000_000} MB write-check "
            "limit. Run `docs-drift check` locally or use the VS Code extension instead."
        ),
    ).model_dump_json(exclude_defaults=True)


def load_write_index(path: Path) -> WriteIndex | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_WRITE_INDEX_BYTES + 1_000:
            return None
        return WriteIndex.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_relative(value: str) -> PurePosixPath | None:
    try:
        relative = PurePosixPath(normalize_repo_path(value))
    except ValueError:
        return None
    if PureWindowsPath(value).drive or ".git" in relative.parts or any(part in {"", "."} for part in relative.parts):
        return None
    return relative


def materialize(write_index: WriteIndex, root: Path) -> None:
    """Create metadata files and empty placeholders; paths are validated and never leave ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    for value in write_index.source_paths[:MAX_WRITE_PATHS]:
        relative = _safe_relative(value)
        if relative is None:
            continue
        target = resolved.joinpath(*relative.parts)
        try:
            if not target.resolve().is_relative_to(resolved):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        except OSError:
            continue  # Names that this filesystem cannot represent only lose the placeholder.
    for name, text in write_index.metadata.items():
        if name in METADATA_FILES and len(text.encode("utf-8")) <= min(MAX_METADATA_BYTES, MAX_FILE_BYTES):
            (resolved / name).write_text(text, encoding="utf-8")


def _declared(index: CodeIndex, finding: Finding, name: str | None) -> Symbol | None:
    """The indexed declaration of ``name`` for the claim's kind, if this revision still has one."""
    if not name:
        return None
    for kind in RENAME_KINDS.get(finding.claim.kind, ()):
        found = index.by_name.get((kind, normalize_name(name))) or index.by_alias.get((kind, normalize_name(name)))
        if found:
            return found[0]
    return None


def closest(index: CodeIndex, finding: Finding) -> Symbol | None:
    """A "did you mean" for missing names: below the rename threshold, never a verdict.

    Dictation often drops or adds a plural or a dash; the writer decides whether it was meant.
    """
    if finding.verdict != Verdict.missing or finding.suggestion:
        return None
    pool = [symbol for kind in RENAME_KINDS.get(finding.claim.kind, ()) for symbol in index.by_kind.get(kind, [])]
    ranked = nlargest(2, ((similarity(finding.claim.value, s.name), s) for s in pool), key=lambda pair: pair[0])
    if not ranked or ranked[0][0] < MIN_CANDIDATE_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_CANDIDATE_MARGIN:
        return None
    return ranked[0][1]


def _carry_over(findings: list[Finding], saved: ScanResult | None, index: CodeIndex) -> list[Finding]:
    """Reuse history-derived evidence from the saved scan of this revision for identical claims only."""
    if saved is None:
        return findings
    known: dict[tuple[str, str], Finding] = {}
    for item in saved.findings:
        if item.is_drift:
            known.setdefault((item.claim.kind.value, item.claim.value), item)
    output = []
    for finding in findings:
        previous = known.get((finding.claim.kind.value, finding.claim.value))
        if previous is None or finding.verdict == Verdict.ok:
            output.append(finding)
            continue
        commit = previous.introduced_by
        trace = f" to {commit.short_sha} ({commit.summary})." if commit else "."
        note = f"The saved scan of this revision traced {finding.claim.value} through Git history{trace}"
        symbol = _declared(index, finding, previous.suggestion)
        if finding.verdict == Verdict.missing and previous.verdict == Verdict.renamed and symbol is not None:
            evidence = [*finding.evidence, Evidence(file=symbol.file, line=symbol.line, text=symbol.qualified_name)]
            update: dict[str, Any] = {
                "verdict": Verdict.renamed,
                "suggestion": previous.suggestion,
                "matched_symbol": symbol,
                "confidence": previous.confidence,
                "introduced_by": commit,
                "evidence": evidence,
                "reasons": [*finding.reasons, note],
            }
        elif finding.verdict == previous.verdict:
            update = {
                "suggestion": finding.suggestion or previous.suggestion,
                "introduced_by": finding.introduced_by or commit,
                "reasons": [*finding.reasons, note],
            }
        else:
            update = {}
        output.append(finding.model_copy(update=update) if update else finding)
    return output


class IndexedDriftService(DriftService):
    """``DriftService`` over a skeleton tree with a fixed, pre-built index and server-owned config."""

    def __init__(self, root: Path, write_index: WriteIndex, saved: ScanResult | None) -> None:
        super().__init__(root)
        index = write_index.code_index()
        packages = list(write_index.packages) or [Package(name=root.name or "repository", root=".", language="")]
        self._fixed_config = Config()
        self._fixed_state = _IndexState("saved-index", index, packages, package_indexes(index, packages))
        self._saved = saved
        self.head_sha = write_index.head_sha
        self.index = index

    @property
    def config(self) -> Config:
        # Repository configuration is never loaded by public jobs (see web/settings.py).
        return self._fixed_config

    def _index_state(self, config: Config) -> _IndexState:
        return self._fixed_state

    def _document_result(
        self, relative: str, text: str, raw: bytes | None, history: bool, ticket: Ticket | None
    ) -> ScanResult:
        result = super()._document_result(relative, text, raw, False, ticket)
        return result.model_copy(
            update={"findings": _carry_over(result.findings, self._saved, self._fixed_state.index)}
        )

    def check_write(self, path: str, text: str, ticket: Ticket | None = None) -> tuple[str, ScanResult, list[TextEdit]]:
        """The ``check_text`` document path, returning full findings and certain edits."""
        with self._locked(ticket):
            _, relative = self.document_path(path, must_exist=False)
            result = self._document_result(relative, text, None, False, ticket)
            try:
                edits = plan_rule_fixes(result, contents={relative: text}).edits
            except (StaleDocumentError, ValueError, OSError, IndexError):
                edits = []
        return relative, result, edits


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+[^`]*`+)")
_BARE_FLAG = re.compile(r"(?<![`\w/-])(--[A-Za-z][\w-]*[A-Za-z0-9])(?![`\w])")
_BARE_ENV = re.compile(r"(?<![`\w$])([A-Z][A-Z0-9]*_[A-Z0-9_]*[A-Z0-9])(?![`\w])")


def normalize_dictation(text: str) -> str:
    """Dictation never types Markdown backticks; mark bare --flags and ENV_NAMES as code so they are checked.

    Only prose outside fenced blocks and existing code spans changes, and line numbers never move, so the
    findings still point at the text the writer sees.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        if opener:
            fence = opener.group(1)
            out.append(line)
            continue
        parts = _CODE_SPAN.split(line)
        for index in range(0, len(parts), 2):
            parts[index] = _BARE_ENV.sub(r"`\1`", _BARE_FLAG.sub(r"`\1`", parts[index]))
        out.append("".join(parts))
    return "\n".join(out)


def _verdict_label(finding: Finding) -> str:
    return {
        Verdict.ok: "ok",
        Verdict.missing: "missing",
        Verdict.renamed: "renamed",
        Verdict.default_changed: "default changed",
        Verdict.invalid_command: "invalid command",
        Verdict.unverifiable: "unverifiable",
    }[finding.verdict]


def _edit_out(edit: TextEdit, *, certain: bool) -> dict[str, Any]:
    return {
        "line": edit.line,
        "column": edit.column,
        "old_text": edit.old_text,
        "new_text": edit.new_text,
        "reason": edit.reason,
        "certain": certain,
    }


def present(relative: str, text: str, result: ScanResult, edits: list[TextEdit], index: CodeIndex) -> dict[str, Any]:
    """Shape findings for the editor: drift first, then unverifiable and ok, each in document order."""
    lines = text.splitlines()
    by_claim: dict[str, list[TextEdit]] = {}
    for planned in edits:
        by_claim.setdefault(planned.claim_id, []).append(planned)
    rows = []
    order = {Verdict.ok: 2, Verdict.unverifiable: 1}
    for finding in sorted(
        result.findings, key=lambda f: (order.get(f.verdict, 0), f.claim.line, f.claim.column, f.claim.claim_id)
    ):
        out = finding_out(finding).model_dump(mode="json")
        claim_edits = sorted(by_claim.get(finding.claim.claim_id, []), key=lambda e: (e.line, e.column))
        primary = next((e for e in claim_edits if e.line == finding.claim.line), None)
        edit: dict[str, Any] | None = _edit_out(primary, certain=True) if primary else None
        candidate = closest(index, finding)
        # Offer a one-click replacement only when something ties the names together: a rename verdict,
        # the saved scan's history, or a close spelling. A lone "closest candidate" stays text only.
        supported = finding.suggestion and (
            finding.verdict == Verdict.renamed
            or finding.introduced_by is not None
            or similarity(finding.claim.value, finding.suggestion) >= MIN_CANDIDATE_SCORE
        )
        proposed = finding.suggestion if supported else (candidate.name if candidate else None)
        if (
            edit is None
            and finding.is_drift
            and finding.verdict != Verdict.default_changed
            and proposed
            and finding.claim.line <= len(lines)
        ):
            # Not certain enough for the rule fixer, but the writer may still choose the indexed name.
            proposal = _edit(
                finding.claim,
                lines[finding.claim.line - 1],
                finding.claim.value,
                proposed,
                "Use the name the code declares now; review before keeping it.",
            )
            edit = _edit_out(proposal, certain=False) if proposal else None
        rows.append(
            {
                "id": out["id"],
                "verdict": finding.verdict.value,
                "label": _verdict_label(finding),
                "is_drift": finding.is_drift,
                "kind": finding.claim.kind.value,
                "claim": out["claim"],
                "expected_default": finding.claim.expected_default,
                "line": finding.claim.line,
                "column": finding.claim.column,
                "suggestion": out["suggestion"],
                "candidate": {"name": candidate.name, "file": candidate.file, "line": candidate.line}
                if candidate
                else None,
                "confidence": out["confidence"],
                "message": out["message"],
                "reasons": [reason[:300] for reason in finding.reasons[:4]],
                "evidence": [e for e in out["evidence"] if e["file"] != relative],
                "introduced_by": out["introduced_by"],
                "edit": edit,
                "other_edits": [_edit_out(e, certain=True) for e in claim_edits if e is not primary],
            }
        )
    counts = dict.fromkeys((v.value for v in Verdict), 0)
    for finding in result.findings:
        counts[finding.verdict.value] += 1
    return {
        "path": relative,
        "claims": len(result.findings),
        "drift": sum(f.is_drift for f in result.findings),
        "by_verdict": counts,
        "findings": rows[:MAX_WRITE_FINDINGS],
        "truncated": len(rows) > MAX_WRITE_FINDINGS,
    }


class WriteChecker:
    """Caches one skeleton tree and indexed service per scan; never shares state between scans."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._services: OrderedDict[str, IndexedDriftService] = OrderedDict()

    def service(self, key: str, write_index: WriteIndex, saved: ScanResult | None) -> IndexedDriftService:
        digest = hashlib.sha256(
            (key + "\0" + write_index.head_sha + "\0" + str(len(write_index.symbols))).encode()
        ).hexdigest()[:32]
        with self._lock:
            cached = self._services.get(digest)
            if cached is not None:
                self._services.move_to_end(digest)
                return cached
            root = self.directory / digest
            staging = self.directory / f"tmp-{uuid.uuid4().hex}"
            materialize(write_index, staging)
            shutil.rmtree(root, ignore_errors=True)
            staging.replace(root)
            service = IndexedDriftService(root, write_index, saved)
            self._services[digest] = service
            while len(self._services) > MAX_CACHED_TREES:
                old, evicted = self._services.popitem(last=False)
                with evicted._lock:  # Wait for a check still running on that tree.
                    shutil.rmtree(self.directory / old, ignore_errors=True)
            return service

    def check(  # noqa: PLR0913, PLR0917 - one call carries the whole request into the worker thread.
        self, key: str, write_index: WriteIndex, saved: ScanResult | None, path: str, text: str, ticket: Ticket
    ) -> dict[str, Any]:
        service = self.service(key, write_index, saved)
        relative, result, edits = service.check_write(path, normalize_dictation(text), ticket)
        if any(w.startswith("Could not process") for w in result.warnings):
            raise DocsDriftError("This text could not be parsed as Markdown.", "Simplify the formatting and retry.")
        body = present(relative, text, result, edits, service.index)
        body["head_sha"] = service.head_sha
        return body
