"""Version-range arithmetic for PEP 440 specifiers and npm semver ranges, without network access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

VersionTuple = tuple[int, int, int]
_INFINITY: VersionTuple = (10**9, 0, 0)
_ZERO: VersionTuple = (0, 0, 0)
# Candidate interpreter versions used to find a declared minimum; bounded and deterministic.
PYTHON_CANDIDATES = tuple(f"2.{minor}" for minor in range(8)) + tuple(f"3.{minor}" for minor in range(41))
PYTHON_MAX_MICRO = 99
MAX_RANGE_CHARS = 256
_NPM_VERSION = re.compile(
    r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_NPM_COMPARATOR = re.compile(r"^(\^|~>?|>=|<=|>|<|=)?\s*(.+)$")


def parse_specifier(text: str) -> SpecifierSet | None:
    """Parse a PEP 440 specifier set, also accepting Poetry caret/tilde shorthands."""
    text = text.strip()
    if not text or text == "*":
        return SpecifierSet()
    if len(text) > MAX_RANGE_CHARS:
        return None
    parts = []
    for raw in text.split(","):
        part = raw.strip()
        poetry = re.fullmatch(r"([\^~])\s*(\d+(?:\.\d+){0,2})", part)
        if poetry:
            numbers = [int(n) for n in poetry[2].split(".")]
            lower = ".".join(map(str, numbers))
            if poetry[1] == "^":
                index = next((i for i, n in enumerate(numbers) if n), len(numbers) - 1)
                upper = [*numbers[:index], numbers[index] + 1]
            else:
                upper = [numbers[0], numbers[1] + 1] if len(numbers) > 1 else [numbers[0] + 1]
            parts.extend([f">={lower}", "<" + ".".join(map(str, upper))])
        elif re.fullmatch(r"\d+(?:\.\d+){0,2}(?:\.\*)?", part):
            parts.append("==" + part)
        else:
            parts.append(part)
    try:
        return SpecifierSet(",".join(parts))
    except InvalidSpecifier:
        return None


def python_minimum(specifier: SpecifierSet) -> Version | None:
    """Return the oldest interpreter release admitted by a requires-python specifier.

    Patch-level bounds are kept: `>=3.8.1` and `>3.8` both give 3.8.1, never the next minor.
    """
    return _python_minimum(str(specifier))


@lru_cache(maxsize=256)
def _python_minimum(text: str) -> Version | None:
    specifier = SpecifierSet(text)
    for candidate in PYTHON_CANDIDATES:
        for micro in range(PYTHON_MAX_MICRO + 1):
            version = f"{candidate}.{micro}"
            if specifier.contains(version, prereleases=True):
                return Version(candidate if micro == 0 else version)
    return None


def specifiers_overlap(first: SpecifierSet, second: SpecifierSet) -> bool:
    """Approximate satisfiability by probing versions named by either specifier and their neighbours."""
    if not str(first) or not str(second):
        return True
    probes: set[Version] = set()
    for spec in [*first, *second]:
        try:
            base = Version(spec.version.removesuffix(".*"))
        except InvalidVersion:
            return True
        release = (*base.release, 0, 0)[:3]
        major, minor, micro = release
        for parts in (
            release,
            (major, minor, micro + 1),
            (major, minor + 1, 0),
            (major + 1, 0, 0),
            (major, max(minor - 1, 0), 0),
            (max(major - 1, 0), 0, 0),
        ):
            probes.add(Version(".".join(map(str, parts))))
        probes.add(base)
    return any(first.contains(probe, prereleases=True) and second.contains(probe, prereleases=True) for probe in probes)


@dataclass(frozen=True)
class Interval:
    """A half-open semver interval [low, high); prerelease tags are ignored."""

    low: VersionTuple
    high: VersionTuple

    def empty(self) -> bool:
        return self.low >= self.high


def _npm_parts(text: str) -> tuple[list[int | None], bool] | None:
    match = _NPM_VERSION.match(text.strip())
    if not match:
        return None
    parts: list[int | None] = []
    wildcard = False
    for group in match.groups():
        if group is None or group in {"x", "X", "*"}:
            wildcard = True
            parts.append(None)
        else:
            parts.append(None if wildcard else int(group))
    return parts, wildcard


def _bump(parts: list[int | None]) -> VersionTuple:
    """Upper bound of an x-range such as 1.2.x or 1.x."""
    known = [p for p in parts if p is not None]
    if not known:
        return _INFINITY
    if len(known) == 1:
        return (known[0] + 1, 0, 0)
    if len(known) == 2:  # noqa: PLR2004 - major.minor partial version.
        return (known[0], known[1] + 1, 0)
    return (known[0], known[1], known[2] + 1)


def _tuple(parts: list[int | None]) -> VersionTuple:
    values = [p or 0 for p in parts]
    return (values[0], values[1], values[2])


def _comparator(operator: str, text: str) -> Interval | None:  # noqa: PLR0911 - one branch per operator.
    parsed = _npm_parts(text)
    if parsed is None:
        return None
    parts, _ = parsed
    known = [p for p in parts if p is not None]
    low = _tuple(parts)
    if operator in {"", "="}:
        return Interval(low, _bump(parts)) if len(known) < 3 else Interval(low, (low[0], low[1], low[2] + 1))  # noqa: PLR2004
    if operator == "^":
        if not known:
            return Interval(_ZERO, _INFINITY)
        if known[0] > 0 or len(known) == 1:
            return Interval(low, (known[0] + 1, 0, 0))
        if len(known) == 2 or known[1] > 0:  # noqa: PLR2004
            return Interval(low, (0, known[1] + 1, 0))
        return Interval(low, (0, 0, known[2] + 1))
    if operator in {"~", "~>"}:
        if len(known) >= 2:  # noqa: PLR2004
            return Interval(low, (known[0], known[1] + 1, 0))
        return Interval(low, _bump(parts))
    if operator == ">=":
        return Interval(low, _INFINITY)
    if operator == ">":
        return Interval(_bump(parts) if len(known) < 3 else (low[0], low[1], low[2] + 1), _INFINITY)  # noqa: PLR2004
    if operator == "<":
        return Interval(_ZERO, low)
    if operator == "<=":
        return Interval(_ZERO, _bump(parts) if len(known) < 3 else (low[0], low[1], low[2] + 1))  # noqa: PLR2004
    return None


def npm_range(text: str) -> list[Interval] | None:  # noqa: PLR0911 - one exit per unsupported form.
    """Parse an npm range into a union of intervals; protocols, tags and URLs return None."""
    text = re.sub(r"(?<![\w.])v(?=\d)", "", text.strip())
    if len(text) > MAX_RANGE_CHARS:
        return None
    if text in {"", "*", "x", "X", "latest"}:
        return [Interval(_ZERO, _INFINITY)] if text != "latest" else None
    if re.search(r"[:/]|^[A-Za-z]", text.replace("x", "0").replace("X", "0")):
        return None
    result: list[Interval] = []
    for raw_alternative in text.split("||"):
        alternative = raw_alternative.strip()
        hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", alternative)
        if hyphen:
            low = _comparator(">=", hyphen[1])
            high = _comparator("<=", hyphen[2])
            if low is None or high is None:
                return None
            result.append(Interval(low.low, high.high))
            continue
        current = Interval(_ZERO, _INFINITY)
        tokens = re.findall(r"(?:\^|~>?|>=|<=|>|<|=)?\s*[^\s<>=^~]+", alternative)
        if not tokens:
            return None
        for token in tokens:
            match = _NPM_COMPARATOR.match(token.strip())
            if not match:
                return None
            interval = _comparator(match[1] or "", match[2])
            if interval is None:
                return None
            current = Interval(max(current.low, interval.low), min(current.high, interval.high))
        result.append(current)
    return result


def npm_overlap(first: list[Interval], second: list[Interval]) -> bool:
    return any(not Interval(max(a.low, b.low), min(a.high, b.high)).empty() for a in first for b in second)


def npm_minimum(intervals: list[Interval]) -> VersionTuple | None:
    candidates = [interval.low for interval in intervals if not interval.empty()]
    return min(candidates) if candidates else None


def npm_contains(intervals: list[Interval], version: str) -> bool | None:
    parsed = _npm_parts(version)
    if parsed is None or parsed[1]:
        return None
    value = _tuple(parsed[0])
    return any(interval.low <= value < interval.high for interval in intervals)


def format_tuple(value: VersionTuple, width: int) -> str:
    """Render the first `width` components, e.g. (18, 0, 0) with width 1 -> '18'."""
    return ".".join(str(part) for part in value[: max(1, min(width, 3))])
