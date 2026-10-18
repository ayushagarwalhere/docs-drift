"""Bounded source retrieval from static claim-to-symbol links, never imports."""

import re
from pathlib import Path

from docs_drift.llm.privacy import excluded_path, mask_secrets, suspicious_text
from docs_drift.llm.types import Candidate, Snippet
from docs_drift.models import Symbol

MAX_SNIPPETS = 6
MAX_SNIPPET_LINES = 60
MAX_SOURCE_BYTES = 2 * 1024 * 1024
_COMMON_WORDS = {"the", "and", "that", "this", "with", "from", "after", "before", "only", "always", "never"}


def retrieve(repo: Path, candidate: Candidate, symbols: list[Symbol], redact_paths: list[str]) -> list[Snippet]:
    root = repo.resolve()
    snippets: list[Snippet] = []
    keywords = set(re.findall(r"[A-Za-z_]{4,}", candidate.claim.value.casefold())) - _COMMON_WORDS
    for relative in candidate.source_files:
        if excluded_path(relative, redact_paths):
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        definitions = [symbol for symbol in symbols if symbol.file == relative]
        named = [symbol.line for symbol in definitions if symbol.name in candidate.claim.value]
        keyword_lines = [
            index + 1 for index, line in enumerate(lines) if any(word in line.casefold() for word in keywords)
        ]
        anchors = [*named, *keyword_lines, *[symbol.line for symbol in definitions], 1]
        ranges: list[tuple[int, int]] = []
        for anchor in anchors:
            start = max(1, min(anchor - 8, len(lines) - MAX_SNIPPET_LINES + 1))
            end = min(len(lines), start + MAX_SNIPPET_LINES - 1)
            if any(start <= old_end and end >= old_start for old_start, old_end in ranges):
                continue
            ranges.append((start, end))
            raw = "\n".join(lines[start - 1 : end])
            snippets.append(
                Snippet(
                    snippet_id=f"s{len(snippets) + 1}",
                    file=relative,
                    start_line=start,
                    end_line=end,
                    text=mask_secrets(raw),
                    suspicious=suspicious_text(raw),
                )
            )
            if len(snippets) >= MAX_SNIPPETS:
                return snippets
    return snippets
