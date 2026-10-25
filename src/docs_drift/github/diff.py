"""Map PR file patches to the head-side lines GitHub accepts review comments on."""

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from docs_drift.history import _run

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(patch: str) -> set[int]:
    """Return RIGHT-side line numbers that appear in a hunk (added or context lines).

    GitHub rejects the whole review with 422 if any comment targets a line outside
    the diff, so callers must check membership before posting.
    """
    lines: set[int] = set()
    current: int | None = None
    for row in patch.splitlines():
        header = _HUNK.match(row)
        if header:
            current = int(header.group(1))
            continue
        if current is None or row.startswith("\\"):
            continue
        if row.startswith("+"):
            lines.add(current)
            current += 1
        elif row.startswith("-"):
            continue
        else:  # Context line (leading space, or an empty line for a blank context row).
            lines.add(current)
            current += 1
    return lines


def merge_base(repo: Path, base: str, head: str = "HEAD") -> str:
    """The PR diff starts at the merge base (GitHub's three-dot diff); shallow clones fall back to ``base``."""
    result = _run(repo, ["merge-base", base, head])
    return result.stdout.strip() if result and result.stdout.strip() else base


def changed_lines(repo: Path, base: str, head: str = "HEAD", paths: Iterable[str] | None = None) -> dict[str, set[int]]:
    """Head-side lines the PR added or modified, keyed by path relative to ``repo``.

    Like GitHub's PR diff this compares against the merge base, so commits that landed on
    the base branch after the PR branched are not attributed to the PR. Paths come from a
    NUL-separated listing (no quoting of spaces or non-ASCII names) and each file's hunks
    are read separately, so no patch header is ever mistaken for content.
    """
    start = merge_base(repo, base, head)
    listing = _run(
        repo, ["diff", "--no-ext-diff", "--no-renames", "--relative", "--name-only", "-z", start, head, "--"]
    )
    if listing is None:
        return {}
    names = [name for name in listing.stdout.split("\0") if name]
    if paths is not None:
        wanted = set(paths)
        names = [name for name in names if name in wanted]
    lines: dict[str, set[int]] = {}
    for name in names:
        patch = _run(
            repo,
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--relative",
                "-U0",
                start,
                head,
                "--",
                f":(literal){name}",
            ],
        )
        if patch is not None:
            lines[name] = commentable_lines(patch.stdout)
    return lines


def diff_map(files: Iterable[Mapping[str, Any]]) -> dict[str, set[int]]:
    """Build ``{path: commentable lines}`` from ``GET /pulls/{n}/files`` entries.

    Removed files and files without a textual patch (binary or too large) map to
    no lines, so their findings fall back to the summary comment.
    """
    result: dict[str, set[int]] = {}
    for entry in files:
        name = entry.get("filename")
        patch = entry.get("patch")
        if not isinstance(name, str) or entry.get("status") == "removed":
            continue
        result[name] = commentable_lines(patch) if isinstance(patch, str) else set()
    return result
