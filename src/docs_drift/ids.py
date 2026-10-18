"""Pure, versioned identity helpers; callers supply already-normalized claim values."""

import hashlib
import json
from collections.abc import Sequence
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from docs_drift.limits import CONTROL_START, DELETE_CHARACTER, MAX_PATH_CHARS


def normalize_repo_path(value: object) -> str:
    """Normalize separators without touching disk or guessing a repository root."""
    if not isinstance(value, (str, PurePath)):
        raise ValueError("Use a repository-relative path as text or a pathlib path.")
    text = str(value).replace("\\", "/")
    if len(text) > MAX_PATH_CHARS or any(ord(c) < CONTROL_START or ord(c) == DELETE_CHARACTER for c in text):
        raise ValueError(f"Use a repository path under {MAX_PATH_CHARS} characters without control characters.")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or PureWindowsPath(text).drive or ".." in path.parts or str(path) == ".":
        raise ValueError("Use a nonempty repository-relative path without a drive or '..'.")
    return path.as_posix()


def _digest(namespace: str, parts: Sequence[object]) -> str:
    encoded = json.dumps([namespace, 1, *parts], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{namespace}_{hashlib.sha256(encoded).hexdigest()}"


def _occurrence(parts: list[object], occurrence: int) -> list[object]:
    if occurrence < 0:
        raise ValueError("Use zero for the first occurrence and a positive counter for duplicates.")
    return [*parts, occurrence] if occurrence else parts


def make_section_id(doc_path: str | PurePath, heading_path: Sequence[str], occurrence: int = 0) -> str:
    """The heading trail includes the current heading; duplicate trails get a counter."""
    parts: list[object] = [normalize_repo_path(doc_path), list(heading_path)]
    return _digest("section", _occurrence(parts, occurrence))


def make_claim_id(
    doc_path: str | PurePath, heading_path: Sequence[str], kind: str, value: str, occurrence: int = 0
) -> str:
    """Exclude line, column, raw text, and confidence; count repeated kind/value pairs per section."""
    parts: list[object] = [normalize_repo_path(doc_path), list(heading_path), kind, value]
    return _digest("claim", _occurrence(parts, occurrence))


def make_symbol_id(kind: str, qualified_name: str, file: str | PurePath) -> str:
    return _digest("symbol", [kind, qualified_name, normalize_repo_path(file)])
