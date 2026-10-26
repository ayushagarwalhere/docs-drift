"""Recognise project release labels next to runtime minimums.

"2.x | Python 3.7+", "| 1.x (legacy) | Python 2.7+ |" and "- demo 1.x supports Python 2.7+." describe what
an older release needed. Such a statement is checked only when its label covers the release the manifest
declares; otherwise it is compatibility history and is never reported or rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_DASHES = "-" + chr(0x2013) + chr(0x2014)
_NUM = r"v?\d+(?:\.(?:\d+|[xX*]))*"
_CORE = re.compile(
    r"(?P<op><=|>=|==|[<>~^=])?\s*(?P<low>" + _NUM + r")(?P<plus>\s*\+)?"
    r"(?:\s*(?:[" + _DASHES + r"]|\bto\b|\bthrough\b|\bthru\b|\buntil\b|\.\.\.?)\s*(?P<high>" + _NUM + r"))?"
    r"(?:\s+(?:and|or)\s+(?:(?P<down>earlier|older|below|lower|prior)|(?P<up>later|newer|above|up|higher|onwards?))\b)?",
    re.IGNORECASE,
)
_QUALIFIER = (
    r"(?:legacy|series|line|branch(?:es)?|releases?|versions?|lts|stable|current|latest|maintenance|maintained|eol|"
    r"end[- ]of[- ]life|unsupported|supported|deprecated|old|older|previous|beta|alpha|rc\d*|preview|nightly|dev|"
    r"security(?:[- ](?:fix(?:es)?|only|updates?|releases?))?|bug[- ]?fix(?:es)?|fixes|only|onwards?|present)"
)
_LEAD_WORD = r"(?:(?:version|release|branch|series|line|tag)s?\s*)?"
_CELL_TRAIL = re.compile(r"(?:[\s,;:/" + _DASHES + r"]*(?:\([^()]*\)|\[[^\]]*\]|" + _QUALIFIER + r"\b))*\s*", re.I)
_LEAD = re.compile(_LEAD_WORD, re.IGNORECASE)
_NOT_RELEASE = re.compile(
    r"\b(?:min(?:imum)?|max(?:imum)?|requir\w*|recommend\w*|prefer\w*|tested|default|optional|runtime|interpreter|"
    r"python|cpython|py|node(?:\.?js)?)\b",
    re.IGNORECASE,
)
_RUNTIME_WORD = re.compile(r"(?:python|cpython|py|node(?:\.?js)?)\s*(?:versions?\s*)?$", re.IGNORECASE)
_RELEASE_HEADER = re.compile(
    r"(?:(?:project|package|library|lib|app|tool|plugin)\s+)?(?:release|version|series|branch|tag)s?"
    r"(?:\s+(?:line|series|branch))?",
    re.IGNORECASE,
)
_RUNTIME_HEADING = re.compile(
    r"\b(?:support\w*|tested|compatib\w*|target\w*|platforms?|engines?|environments?|dependenc\w*)\b", re.I
)
_BULLET = re.compile(r"\s*(?:>\s*)*(?:[-*+]\s+|\d+[.)]\s+)?[*_`\s]*")
_BOUNDARY = re.compile(r"[\s*_`:,;)\]" + _DASHES + r"]|$")
_PRECEDED = re.compile(
    r"\b(?P<word>in|for|from|since|until|till|before|after|through|prior\s+to|up\s+to|(?:version|release|branch|series)s?)"
    r"\s+(?:(?:version|release)s?\s+)?(?=[<>=~^]*\s*v?\d)",
    re.IGNORECASE,
)
_UPPER = re.compile(r"(?<![\w.])(?=" + _NUM + r"\s+(?:and|or)\s+(?:earlier|older|below|lower|prior)\b)", re.IGNORECASE)
_HISTORY = re.compile(
    r"\b(?:legacy|older|previous|earlier|past|historical|prior)\s+(?:releases?|versions?|branch(?:es)?|series|lines?)\b"
    r"(?!\s+of\s+(?:python|cpython|node))",
    re.IGNORECASE,
)
_SUBJECT = re.compile(r"\b(?:tested|supported|works?|working|runs?|compatible|verified|validated|checked)\s*$", re.I)
_ADVICE = re.compile(r"\s*(?:is\s+|are\s+)?(?:recommended|preferred|tested|required|minimum)\b", re.IGNORECASE)
_BELOW_WORDS = {"until", "till", "before", "prior to"}
_AT_MOST_WORDS = {"up to", "through"}
_AT_LEAST_WORDS = {"from", "since", "after"}
# The row for the release the manifest declares: "latest", "current (main)", "main branch", "HEAD".
_CURRENT_CELL = re.compile(
    r"(?:the\s+)?(?:latest|current|main|master|trunk|head)(?:\s+(?:stable\s+)?(?:release|version|series|line|branch))?"
    r"(?:\s*\([^()]*\))?",
    re.IGNORECASE,
)
_MIN_YEAR = 1900
_MAX_PLAIN_MAJOR = 999


@dataclass(frozen=True)
class ReleaseLabel:
    """A release named next to a runtime minimum; `low`/`high` stop at the first wildcard ("2.x" is (2,))."""

    text: str
    kind: str = "unknown"  # exact, at_least, at_most, below, range, current, unknown
    low: tuple[int, ...] = ()
    high: tuple[int, ...] = ()
    project: str | None = None

    def covers(self, version: str | None) -> bool:
        """Whether the label includes `version`, the release the manifest declares."""
        if self.kind == "current":
            return True
        current = numbers(version or "")
        if not current or not self.low or self.kind == "unknown":
            return False

        def prefix(size: int) -> tuple[int, ...]:
            return (*current, *([0] * size))[:size]

        low = prefix(len(self.low))
        checks = {
            "exact": low == self.low,
            "at_least": low >= self.low,
            "at_most": low <= self.low,
            "below": low < self.low,
            "range": low >= self.low and bool(self.high) and prefix(len(self.high)) <= self.high,
        }
        return checks.get(self.kind, False)


def numbers(text: str) -> tuple[int, ...]:
    """Leading release numbers: 'v2.4.x' -> (2, 4); '3.1.0rc1' -> (3, 1, 0)."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text)
    return tuple(int(part) for part in match[1].split(".")) if match else ()


def _from_core(match: re.Match[str], text: str, project: str | None = None, word: str = "") -> ReleaseLabel:
    low = numbers(match["low"])
    high = numbers(match["high"]) if match["high"] else ()
    op = match["op"] or ""
    kind = "exact"
    if high:
        kind = "range"
    elif op == "<" or word in _BELOW_WORDS:
        kind = "below"
    elif op == "<=" or match["down"] or word in _AT_MOST_WORDS:
        kind = "at_most"
    elif op in {">", ">="} or match["plus"] or match["up"] or word in _AT_LEAST_WORDS:
        kind = "at_least"
    elif op == "^":
        low = low[:1]
    elif op == "~":
        low = low[:2]
    return ReleaseLabel(text.strip(), kind, low, high, project)


def _has_release_shape(match: re.Match[str]) -> bool:
    """A bare integer ('3 workers') is not a release; '2.x', '2.4', 'v2', '>=2' and '2+' are."""
    low = match["low"]
    if low[:1] in {"v", "V"} or "." in low:
        return True
    return bool(match["op"] or match["plus"] or match["high"] or match["down"] or match["up"])


def _plain_year(match: re.Match[str]) -> bool:
    value = numbers(match["low"])
    return len(value) == 1 and (value[0] >= _MIN_YEAR or value[0] > _MAX_PLAIN_MAJOR)


def release_cell(cell: str, *, release_column: bool = False) -> ReleaseLabel | None:
    """'1.x (legacy)', '2.x series', '2.4 LTS', '1.0 - 1.9', 'v1', '<2.0', 'Release 2.x', 'latest'.

    A cell that begins with a release ('2.4 LTS (until 2025)', '2.x - maintained until 2027') is a label
    whatever qualifier follows; under a Version/Release/Series/Branch header (`release_column`) even a
    qualifier naming the runtime ('2.x (Python 3.7 compatible)') or a bare major ('2') is accepted.
    """
    text = cell.strip().strip("`*_ ").strip()
    if _CURRENT_CELL.fullmatch(text):
        return ReleaseLabel(text, "current")
    lead = _LEAD.match(text)
    core = _CORE.match(text, lead.end() if lead else 0) if text else None
    if core is None:
        return None
    trail = _CELL_TRAIL.match(text, core.end())
    if trail is not None and trail.end() == len(text) and not _NOT_RELEASE.search(text):
        return _from_core(core, text)
    leading = (
        not _plain_year(core)
        and _BOUNDARY.match(text, core.end()) is not None
        and _ADVICE.match(text, core.end()) is None
        and (
            release_column
            or ((bool(lead and lead.end() > 0) or _has_release_shape(core)) and not _NOT_RELEASE.search(text))
        )
    )
    return _from_core(core, text[: core.end()]) if leading else None


def _name_pattern(name: str) -> re.Pattern[str] | None:
    parts = [part for part in re.split(r"[-_.\s]+", name.strip()) if part]
    if not parts or len("".join(parts)) < 2 or "".join(parts).casefold() in {"python", "node", "nodejs"}:  # noqa: PLR2004
        return None
    body = r"[-_.\s]?".join(re.escape(part) for part in parts)
    return re.compile(
        r"(?<![\w.@/-])" + body + r"(?:\s+(?:(?:version|release)s?\s+)?|\s*(?:@|==)\s*)(?=[<>=~^]*\s*v?\d)",
        re.IGNORECASE,
    )


def _project_label(text: str, projects: Mapping[str, str | None]) -> ReleaseLabel | None:
    for name in projects:
        pattern = _name_pattern(name)
        if pattern is None:
            continue
        for found in pattern.finditer(text):
            core = _CORE.match(text, found.end())
            if core is not None and not _plain_year(core):
                return _from_core(core, text[found.start() : core.end()], name)
    return None


def _runtime_before(text: str, position: int) -> bool:
    return _RUNTIME_WORD.search(text[:position].rstrip()) is not None


def prose_label(line: str, raw: str, projects: Mapping[str, str | None]) -> ReleaseLabel | None:
    """A release named in the same line as a prose runtime minimum, outside the runtime phrase itself."""
    text = line.replace(raw, "\0", 1) if raw and raw in line else line
    label = _project_label(text, projects)
    if label is not None:
        return label
    bullet = _BULLET.match(text)
    start = bullet.end() if bullet else 0
    lead = _LEAD.match(text, start)
    lead_word = bool(lead and lead.end() > start)
    core = _CORE.match(text, lead.end() if lead else start)
    if (
        core is not None
        and _BOUNDARY.match(text, core.end())
        and (lead_word or _has_release_shape(core))
        and not _plain_year(core)
        and not _ADVICE.match(text, core.end())
    ):
        return _from_core(core, text[start : core.end()])
    for found in _PRECEDED.finditer(text):
        if _runtime_before(text, found.start()) or _SUBJECT.search(text[: found.start()].rstrip()):
            continue
        core = _CORE.match(text, found.end())
        word = re.sub(r"\s+", " ", found["word"].casefold())
        release_word = word.rstrip("s") in {"version", "release", "branch", "serie", "series"}
        if (
            core is None
            or not _BOUNDARY.match(text, core.end())
            or _plain_year(core)
            or (not release_word and not _has_release_shape(core))
            or _ADVICE.match(text, core.end())
        ):
            continue
        return _from_core(core, text[found.start() : core.end()], word=word)
    for found in _UPPER.finditer(text):
        core = _CORE.match(text, found.end())
        if core is not None and not _runtime_before(text, found.start()):
            return _from_core(core, text[found.start() : core.end()])
    history = _HISTORY.search(text)
    return ReleaseLabel(history[0]) if history else None


def split_row(row: str) -> list[str]:
    body = row.strip()
    body = body.removeprefix("|")
    body = body.removesuffix("|") if not body.endswith("\\|") else body
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def table_header(lines: Sequence[str], index: int) -> list[str] | None:
    """Header cells of the pipe table holding 0-based line `index`, when its delimiter row is found."""
    start = index
    while start > 0 and lines[start - 1].lstrip().startswith("|"):
        start -= 1
    for position in range(start, index):
        if re.fullmatch(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*(?::?-{3,}:?\s*)?", lines[position]) and position > start:
            return split_row(lines[position - 1])
    return None


def _release_heading(heading: str) -> bool:
    """'Version', 'Release', 'Series', 'Branch', 'demo version', 'Release line' - never 'Python version'."""
    if not heading or _NOT_RELEASE.search(heading) or _RUNTIME_HEADING.search(heading):
        return False
    if _RELEASE_HEADER.fullmatch(heading):
        return True
    return re.fullmatch(r"[\w.@/-]+(?:\s+[\w.@/-]+)?\s+" + _RELEASE_HEADER.pattern, heading, re.IGNORECASE) is not None


def table_label(
    row: str, raw: str, header: Sequence[str] | None, projects: Mapping[str, str | None]
) -> ReleaseLabel | None:
    """A cell other than the runtime cell that names a release ('2.x | Python 3.7+')."""
    flattened = re.sub(r"\s+", " ", raw).strip()
    for column, cell in enumerate(split_row(row)):
        text = cell.strip().strip("`*_ ").strip()
        # Skip the runtime cell itself, including '3.8+ (3.12 recommended)' beside a 'Python' cell.
        if not text or text in flattened or flattened.split()[-1:] == text.split()[:1]:
            continue
        heading = header[column].strip().strip("`*_ ").strip() if header and column < len(header) else ""
        named = bool(heading) and any(
            re.fullmatch(r"[-_.\s]?".join(map(re.escape, re.split(r"[-_.\s]+", name))), heading, re.I)
            for name in projects
        )
        release_column = named or _release_heading(heading)
        if re.search(r"(?i)python|node", text):
            # A runtime cell, unless a release column opens with the release ("2.x (Python 3.7 compatible)").
            label = release_cell(text, release_column=True) if release_column else None
            if label is not None:
                return label
            continue
        label = release_cell(text, release_column=release_column) or _project_label(text, projects)
        if label is not None:
            return label
        if release_column and not _NOT_RELEASE.search(text):
            core = _CORE.search(text)
            return _from_core(core, text) if core is not None else ReleaseLabel(text)
    return None
