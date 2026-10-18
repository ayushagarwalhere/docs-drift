"""Bounded text reads for packaging metadata that is not visited by the source walker."""

from pathlib import Path

from docs_drift.limits import MAX_FILE_BYTES, MAX_LINE_CHARS, MAX_NESTING


def metadata_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise OSError("Metadata must be a bounded regular file, not a symbolic link.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if any(len(line) > MAX_LINE_CHARS for line in text.splitlines()):
        raise OSError("Metadata contains an excessively long line.")
    depth = 0
    for character in text:
        depth += (character in "[{") - (character in "]}")
        if depth > MAX_NESTING:
            raise OSError("Metadata nesting exceeds the parser limit.")
    return text
