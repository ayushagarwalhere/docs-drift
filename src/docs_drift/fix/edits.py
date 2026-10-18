"""Exact-span edits, stale-hash checks, byte-preserving writes and unified patches."""

import codecs
import difflib
import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import Field

from docs_drift.models import DecidedBy, PipelineModel, RepoPath


class StaleDocumentError(ValueError):
    """The caller must scan again and obtain fresh spans before writing."""


class TextEdit(PipelineModel):
    file: RepoPath
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)
    old_text: str
    new_text: str
    reason: str
    claim_id: str
    decided_by: DecidedBy = DecidedBy.rules
    replayed: bool = False


class FixPlan(PipelineModel):
    edits: list[TextEdit] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    hashes: dict[str, str] = Field(default_factory=dict)


def safe_path(repo: Path, relative: str) -> Path:
    target = (repo / relative).resolve()
    if not target.is_relative_to(repo.resolve()):
        raise ValueError(f"Refusing to edit {relative}: it resolves outside the repository.")
    return target


def decode(raw: bytes) -> tuple[str, str, bytes]:
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(bom):
            return raw[len(bom) :].decode(encoding), encoding, bom
    return raw.decode("utf-8"), "utf-8", b""


def offsets(text: str, edit: TextEdit) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    if edit.line > len(lines) or edit.end_line > len(lines):
        raise StaleDocumentError(f"Line spans changed in {edit.file}; scan again.")
    start = sum(map(len, lines[: edit.line - 1])) + edit.column - 1
    end = sum(map(len, lines[: edit.end_line - 1])) + edit.end_column - 1
    if text[start:end] != edit.old_text:
        raise StaleDocumentError(f"The exact edit span changed in {edit.file}; scan again.")
    return start, end


def render_file(repo: Path, plan: FixPlan, relative: str) -> tuple[bytes, bytes]:
    path = safe_path(repo, relative)
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != plan.hashes[relative]:
        raise StaleDocumentError(f"{relative} changed since the scan; scan again before applying edits.")
    text, encoding, bom = decode(original)
    selected = [edit for edit in plan.edits if edit.file == relative]
    spans = sorted([(offsets(text, edit), edit) for edit in selected], key=lambda pair: pair[0], reverse=True)
    last_start = len(text) + 1
    for (start, end), edit in spans:
        if end > last_start:
            raise ValueError(f"Overlapping edits in {relative}; review conflicting suggestions manually.")
        text = text[:start] + edit.new_text + text[end:]
        last_start = start
    return original, bom + text.encode(encoding)


def unified_patch(repo: Path, plan: FixPlan) -> str:
    parts: list[str] = []
    for relative in sorted({edit.file for edit in plan.edits}):
        original, modified = render_file(repo, plan, relative)
        before_text, encoding, bom = decode(original)
        after_text = decode(modified)[0]
        if encoding != "utf-8":
            raise ValueError("Unified patches require UTF-8 documents; use a reviewed exact-span write for UTF-16.")
        prefix = "\ufeff" if bom == codecs.BOM_UTF8 else ""
        before = prefix + before_text
        after = prefix + after_text
        for line in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        ):
            parts.append(line)
            if not line.endswith("\n"):
                parts.append("\n\\ No newline at end of file\n")
    return "".join(parts)


def apply_file(repo: Path, plan: FixPlan, relative: str) -> None:
    original, modified = render_file(repo, plan, relative)
    path = safe_path(repo, relative)
    mode = path.stat().st_mode
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".docs-drift-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(modified)
        # Check again immediately before replacing; a changed hash requires another scan.
        if path.read_bytes() != original:
            raise StaleDocumentError(f"{relative} changed during review; scan again.")
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
