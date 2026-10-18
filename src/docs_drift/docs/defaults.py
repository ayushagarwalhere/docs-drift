"""Normalize and link explicit default claims to nearby documented settings."""

import re
from collections import Counter
from dataclasses import dataclass

from docs_drift.docs.sections import ParsedDoc, is_historical_span
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction

_DURATION = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hours?)$", re.I)
_SIZE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>MiB|MB|KiB|KB|GiB|GB|B)$", re.I)
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_DEFAULT_PHRASE = re.compile(
    r"\bdefaults?\s+to\s+(?P<value>`[^`]+`|[^,;.)]+)|"
    r"\bdefault\s*(?::|is)\s*(?P<colon>`[^`]+`|[^,;.)]+)|"
    r"\([^()]*?\bdefault\s*:?\s*(?P<paren>`[^`]+`|[^()]+)\)|"
    r"\bif\s+unset,?\s+uses?\s+(?P<unset>`[^`]+`|[^,;.)]+)",
    re.I,
)
_HELP_DEFAULT = re.compile(r"\[\s*default\s*:\s*(?P<value>[^\]]+)\]", re.I)
_LOG_LEVELS = {"trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"}
MAX_DEFAULT_LINK_DISTANCE = 2


@dataclass(frozen=True)
class NormalizedValue:
    """Typed comparison form; ``None`` from equals means the units are insufficient."""

    kind: str
    value: str
    unit: str | None
    raw: str

    def equals(self, other: "NormalizedValue") -> bool | None:
        if self.kind == "unknown" or other.kind == "unknown":
            return None
        if self.kind != other.kind:
            if {self.kind, other.kind} <= {"number", "duration", "size"}:
                return None
            return False
        if self.kind in {"duration", "size"} and (self.unit is None or other.unit is None):
            return None
        return self.value == other.value and self.unit == other.unit


def normalize_value(raw: str, *, subject: str = "") -> NormalizedValue:  # noqa: PLR0911
    """Convert only unambiguous literal forms; keep unknown text comparable as text."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "`"}:  # noqa: PLR2004
        text = text[1:-1].strip()
    folded = text.casefold()
    if folded in {"none", "null", "unset", "empty", '""', "''"} or text == "":
        return NormalizedValue("none", "", None, raw)
    boolean = {
        "true": "true",
        "yes": "true",
        "on": "true",
        "enabled": "true",
        "1": "true",
        "false": "false",
        "no": "false",
        "off": "false",
        "disabled": "false",
        "0": "false",
    }
    if folded in boolean and (
        folded not in {"0", "1"}
        or re.search(r"(?:debug|enabled|active|secure|verbose|disabled|feature|flag|bool)", subject, re.I)
    ):
        return NormalizedValue("boolean", boolean[folded], None, raw)
    duration = _DURATION.fullmatch(text)
    if duration:
        number = float(duration.group("number"))
        unit = duration.group("unit").casefold()
        multiplier = 1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600
        return NormalizedValue("duration", _number_text(number * multiplier), "s", raw)
    size = _SIZE.fullmatch(text)
    if size:
        unit = size.group("unit")
        factors = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
        return NormalizedValue("size", _number_text(float(size.group("number")) * factors[unit.casefold()]), "B", raw)
    if _NUMBER.fullmatch(text):
        number = float(text)
        if number in {0.0, 1.0} and folded in {"0", "0.0", "1", "1.0"}:
            return NormalizedValue("boolean", "true" if number == 1 else "false", None, raw)
        return NormalizedValue("number", _number_text(number), None, raw)
    # Path normalization is limited to path-like strings; it does not resolve or touch disk.
    if text.startswith(("~/", "/", "./", "../")) or "/" in text or "\\" in text:
        path = re.sub(r"/{2,}", "/", text).rstrip("/")
        return NormalizedValue("path", path, None, raw)
    string = (
        text.casefold()
        if (subject and subject.casefold().endswith(("log_level", "loglevel"))) or folded in _LOG_LEVELS
        else text
    )
    return NormalizedValue("string", string, None, raw)


def _number_text(number: float) -> str:
    return str(int(number)) if number.is_integer() else format(number, ".12g")


@dataclass(frozen=True)
class DefaultsResult:
    claims: list[Claim]
    debug_notes: list[str]


def _default_claim(  # noqa: PLR0913, PLR0917
    parsed: ParsedDoc,
    subject: Claim,
    raw_value: str,
    raw_text: str,
    line: int,
    column: int,
    extraction: Extraction,
    occurrence: int,
) -> Claim:
    normalized = normalize_value(raw_value, subject=subject.value)
    section = parsed.line_to_section[line]
    return Claim(
        claim_id=make_claim_id(
            parsed.doc.path,
            section.heading_path,
            ClaimKind.default_value,
            f"{subject.kind.value}:{subject.value}:{normalized.kind}:{normalized.value}:{normalized.unit}",
            occurrence,
        ),
        kind=ClaimKind.default_value,
        raw_text=raw_text.strip() or raw_value,
        value=subject.value,
        qualifier=subject.kind.value,
        expected_default=_display_value(normalized),
        doc_path=parsed.doc.path,
        line=line,
        column=column,
        section_id=section.section_id,
        extraction=extraction,
        extraction_confidence=0.8 if extraction == Extraction.table else 0.55,
    )


def _display_value(value: NormalizedValue) -> str:
    if value.kind == "duration":
        return f"{value.value}s"
    if value.kind == "size":
        return f"{value.value}B"
    if value.kind == "none":
        return "none"
    return value.value


def extract_defaults(parsed: ParsedDoc, claims: list[Claim]) -> DefaultsResult:  # noqa: PLR0912, PLR0915
    """Link prose, table, and help defaults only to a unique nearby subject."""
    subjects = [
        claim for claim in claims if claim.kind in {ClaimKind.cli_flag, ClaimKind.env_var, ClaimKind.config_key}
    ]
    found: list[tuple[Claim, str, str, int, int, Extraction]] = []
    notes: list[str] = []
    for subject in subjects:
        if subject.expected_default is not None:
            found.append(
                (subject, subject.expected_default, subject.raw_text, subject.line, subject.column, subject.extraction)
            )
    lines = parsed.lines
    for line_number, line in enumerate(lines, 1):
        section = parsed.line_to_section.get(line_number)
        if section is None or is_historical_span(section, line_number):
            continue
        same_section = [item for item in subjects if item.section_id == section.section_id]
        for match in _DEFAULT_PHRASE.finditer(line):
            raw_value = (
                next((part for part in match.groupdict().values() if part), "").strip().strip("`\"'").rstrip("].")
            )
            sentence = next(
                (
                    item
                    for item in re.finditer(r"[^.!?]+(?:[.!?](?=\s|$)|$)", line)
                    if item.start() <= match.start() <= item.end()
                ),
                None,
            )
            start = sentence.start() if sentence else 0
            end = sentence.end() if sentence else len(line)
            nearby = [item for item in same_section if item.line == line_number and start <= item.column - 1 <= end]
            # Fixture and prose docs often introduce a setting in one bullet and state its default in the next.
            # Cross-line linking is allowed only for a unique subject within two lines, keeping ambiguity quiet.
            if not nearby:
                nearby = [item for item in same_section if abs(item.line - line_number) <= MAX_DEFAULT_LINK_DISTANCE]
                if len(nearby) > 1:
                    notes.append(f"{parsed.doc.path}:{line_number}: multiple nearby default subjects; skipped")
                    continue
            if not raw_value:
                continue
            if not nearby:
                continue
            distances = sorted(
                ((abs(item.column - 1 - match.start()), item) for item in nearby), key=lambda pair: pair[0]
            )
            if len(distances) > 1 and distances[0][0] == distances[1][0]:
                notes.append(f"{parsed.doc.path}:{line_number}: equally near default subjects; skipped")
                continue
            subject = distances[0][1]
            found.append((subject, raw_value, match.group(), line_number, match.start() + 1, Extraction.prose_pattern))
        # A port number has no safe subject unless the section documents exactly one port-like name.
        if not line.lstrip().startswith("|") and re.search(
            r"\b(?:listens?\s+on\s+port|localhost|127\.0\.0\.1):?\s*\d+", line, re.I
        ):
            port_match = re.search(r"(?:port\s+|localhost:|127\.0\.0\.1:)(\d{2,5})", line, re.I)
            port_subjects = [
                item for item in same_section if "port" in item.value.casefold() or item.value.upper().endswith("_PORT")
            ]
            if port_match and len(port_subjects) == 1:
                found.append(
                    (
                        port_subjects[0],
                        port_match.group(1),
                        port_match.group(),
                        line_number,
                        port_match.start() + 1,
                        Extraction.prose_pattern,
                    )
                )
            elif port_match and len(port_subjects) > 1:
                notes.append(f"{parsed.doc.path}:{line_number}: multiple port subjects; default skipped")
        for match in _HELP_DEFAULT.finditer(line):
            raw_value = match.group("value").strip().strip("`\"'")
            flags = [item for item in same_section if item.kind == ClaimKind.cli_flag and item.line == line_number]
            flag_match = re.search(r"--?[A-Za-z][\w-]*", line)
            if flags and flag_match and raw_value:
                subject = next((item for item in flags if item.value == flag_match.group()), flags[0])
                found.append((subject, raw_value, match.group(), line_number, match.start() + 1, Extraction.code_block))
    counts: Counter[tuple[str, str, str]] = Counter()
    result = []
    unique: set[tuple[str, str, str, str]] = set()
    for subject, raw_value, raw_text, claim_line, column, extraction in found:
        section = parsed.line_to_section.get(claim_line)
        if section is None or is_historical_span(section, claim_line):
            continue
        normalized = normalize_value(raw_value, subject=subject.value)
        identity = (subject.section_id, subject.kind.value, subject.value, _display_value(normalized))
        if identity in unique:
            continue
        unique.add(identity)
        key = (subject.section_id, subject.kind.value, subject.value)
        occurrence = counts[key]
        counts[key] += 1
        result.append(_default_claim(parsed, subject, raw_value, raw_text, claim_line, column, extraction, occurrence))
    return DefaultsResult(result, notes)
