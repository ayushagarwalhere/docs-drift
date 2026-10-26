"""Normalize and link explicit default claims to nearby documented settings."""

import re
from collections import Counter
from dataclasses import dataclass

from docs_drift.docs.qualifiers import CONTEXT_PREFIX
from docs_drift.docs.sections import ParsedDoc, is_historical_span
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction

_DURATION = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hours?)$", re.I)
_SIZE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>MiB|MB|KiB|KB|GiB|GB|B)$", re.I)
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
# Quoted values and dotted numbers (`'127.0.0.1'`, `0.25`) are read whole; otherwise a value ends at
# punctuation or an opening parenthesis ("16777216 (16 MB)").
_VALUE = r"`[^`]+`|'[^'\n]*'|\"[^\"\n]*\"|\d+(?:\.\d+)+(?:\s*[A-Za-z]+\b)?|[^,;.()]+"
_DEFAULT_PHRASE = re.compile(
    rf"\bdefaults?\s+to\s+(?P<value>{_VALUE})|"
    rf"\bdefault\s*(?::|is)\s*(?P<colon>{_VALUE})|"
    # The value ends at its closing backtick or the first `,`/`;` ("(the default `0`, i.e., no limit)").
    r"\([^()]*?\bdefault\s*(?::|\bis\b)?\s*(?P<paren>`[^`]+`|[^()`,;]+)[^()]*\)|"
    rf"\bif\s+unset,?\s+uses?\s+(?P<unset>{_VALUE})",
    re.I,
)
_HELP_DEFAULT = re.compile(r"\[\s*default\s*:\s*(?P<value>[^\]]+)\]", re.I)
_EMPHASIS = re.compile(r"\*+|(?<!\w)_+(?=\S)|(?<=\S)_+(?!\w)")
_LOG_LEVELS = {"trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"}
MAX_DEFAULT_LINK_DISTANCE = 2
# "Defaults to `$X` if set" states a conditional source, not the default itself.
_CONDITIONAL = re.compile(r"\s*(?:if|when)\s+(?:it\s+is\s+|it's\s+)?(?:set|available|defined|present|given)\b", re.I)
# A declaration block that owns the "Default:" lines in its body (Sphinx and MyST).
_DECLARATION = re.compile(
    r"^(?P<indent>\s*)(?:\.\.\s+(?:py:|std:)?(?:data|confval|envvar|option|attribute|setting)::\s+"
    r"|```+\s*\{(?:py:|std:)?(?:data|confval|envvar|option|attribute|setting)\}\s+)(?P<name>\S+)"
)
# A heading that is itself one identifier ("### strict", "## `--port`", "### SECRET_KEY").
_IDENTIFIER_HEADING = re.compile(r"`?(?P<name>--?[A-Za-z][\w-]*|[a-z_][a-z0-9_.]*|[A-Z][A-Z0-9]*_[A-Z0-9_]+)`?")


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
    # An empty collection literal ("[]", "{}") and the word "empty" state the same thing.
    if folded in {"none", "null", "unset", "empty", '""', "''", "[]", "{}", "()"} or text == "":
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
        path = re.sub(r"/{2,}", "/", text).rstrip("/") or "/"  # The root path keeps its slash.
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


def _strip_emphasis(line: str) -> str:
    """Blank Markdown emphasis markers outside code spans, keeping every column in place."""
    parts = re.split(r"(`+[^`]*`+)", line)
    return "".join(part if part.startswith("`") else _EMPHASIS.sub(lambda m: " " * len(m[0]), part) for part in parts)


def _descriptive(stated: str) -> bool:
    """Unquoted multi-word prose or an environment reference describes a default instead of stating one."""
    if not stated:
        return False
    literal = stated.strip("`'\"")
    if literal.startswith("$"):
        return True  # `$FORWARDED_ALLOW_IPS`: the value of another variable, resolved at runtime.
    if re.fullmatch(r"--?[A-Za-z][\w-]*", literal):
        return True  # "**Default:** ``--commit``": the default behaviour is named by a flag, not a value.
    if stated[0] in "`'\"":
        return False
    words = stated.split()
    return len(words) > 1 and not stated[0].isdigit() and stated[0] not in "-+~/."


def _enclosing_declaration(lines: list[str], line_number: int, heading: str) -> tuple[str | None, bool]:
    """The name declared by the directive (or identifier heading) whose body holds this line.

    The flag says whether the declaration is explicit: a directive, or a heading that can only be
    a code name (``SECRET_KEY``, ``dev_addr``, ``--port``). A plain lowercase word heading
    ("### strict") may still be prose.
    """
    current = lines[line_number - 1]
    indent = len(current) - len(current.lstrip())
    for number in range(line_number - 1, 0, -1):
        text = lines[number - 1]
        if not text.strip():
            continue
        declaration = _DECLARATION.match(text)
        if declaration and (len(declaration.group("indent")) < indent or declaration.group(0).lstrip().startswith("`")):
            return declaration.group("name").strip("`"), True
        if len(text) - len(text.lstrip()) < indent or text.lstrip().startswith(("```", "~~~")):
            break  # Left the indented body or fenced block without finding its directive.
        if indent == 0 and (text.startswith("#") or re.fullmatch(r"\s*([=\-~^\"*+#])\1{2,}\s*", text)):
            break  # A heading ends the search; the section heading itself is checked below.
    heading_match = _IDENTIFIER_HEADING.fullmatch(heading.strip().strip("`"))
    if heading_match is None:
        return None, False
    name = heading_match.group("name")
    return name, bool(re.search(r"[_.]|^-", name))


def _fence_lines(parsed: ParsedDoc) -> set[int]:
    lines: set[int] = set()
    for token in parsed.tokens:
        if token.type == "fence" and token.map is not None:
            lines.update(range(token.map[0] + 1, token.map[1] + 1))
    return lines


def _bullet_subject(line: str, candidates: list[Claim]) -> Claim | None:
    """In a list item, the first code span names the item; later spans are cross-references."""
    bullet = re.match(r"\s*(?:[-*+]|\d+[.)])\s+`", line)
    if not bullet:
        return None
    column = bullet.end()  # 1-based column of the opening backtick
    return next((item for item in candidates if item.column == column), None)


def extract_defaults(parsed: ParsedDoc, claims: list[Claim]) -> DefaultsResult:  # noqa: PLR0912, PLR0915
    """Link prose, table, and help defaults only to a unique nearby subject."""
    subjects = [
        claim
        for claim in claims
        if claim.kind in {ClaimKind.cli_flag, ClaimKind.env_var, ClaimKind.config_key}
        and not (claim.qualifier or "").startswith(CONTEXT_PREFIX)
    ]
    found: list[tuple[Claim, str, str, int, int, Extraction]] = []
    notes: list[str] = []
    for subject in subjects:
        if subject.expected_default is not None:
            found.append(
                (subject, subject.expected_default, subject.raw_text, subject.line, subject.column, subject.extraction)
            )
    lines = parsed.lines
    fence_lines = _fence_lines(parsed)
    for line_number, source_line in enumerate(lines, 1):
        section = parsed.line_to_section.get(line_number)
        if section is None or is_historical_span(section, line_number):
            continue
        line = _strip_emphasis(source_line)
        same_section = [item for item in subjects if item.section_id == section.section_id]
        for match in _DEFAULT_PHRASE.finditer(line):
            stated = next((part for part in match.groupdict().values() if part), "").strip()
            raw_value = stated.strip("`\"'").rstrip("].")
            if _descriptive(stated) or _CONDITIONAL.match(line, match.end()):
                # "Current working directory", "the `$WEB_CONCURRENCY` variable", "`x` if set": not a
                # comparable literal.
                continue
            # A span inside the stated value ("The default is `--help`") is the value, not its subject.
            subject_pool = [
                item
                for item in same_section
                if not (item.line == line_number and match.start() <= item.column - 1 < match.end())
            ]
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
            nearby = [item for item in subject_pool if item.line == line_number and start <= item.column - 1 <= end]
            if not nearby and raw_value:
                # "**Default:** 2048" closing a list item belongs to the item's own leading span,
                # not to a flag cross-referenced earlier in the item.
                bullet = _bullet_subject(source_line, [item for item in subject_pool if item.line == line_number])
                if bullet is not None:
                    found.append(
                        (bullet, raw_value, match.group(), line_number, match.start() + 1, Extraction.prose_pattern)
                    )
                    continue
            if not nearby:
                # A "Default:" line in a declaration block (`.. py:data:: NAME`, a `### name` heading)
                # belongs to the declared name, never to a name its body cross-references.
                owner, explicit = _enclosing_declaration(lines, line_number, section.heading)
                # The declared name may be extracted anywhere in the document (an intro list, the heading).
                declared = [item for item in (*subject_pool, *subjects) if owner is not None and item.value == owner]
                if declared and raw_value:
                    found.append(
                        (
                            declared[0],
                            raw_value,
                            match.group(),
                            line_number,
                            match.start() + 1,
                            Extraction.prose_pattern,
                        )
                    )
                    continue
                if owner is not None and explicit:
                    notes.append(f"{parsed.doc.path}:{line_number}: default of unextracted subject {owner}; skipped")
                    continue
                if owner is not None:
                    # Under "### strict", the `--strict` flag the body mentions is a namesake of another kind
                    # (a CLI override of the setting), not the subject of the setting's default.
                    subject_pool = [item for item in subject_pool if item.value.lstrip("-") != owner]
            # Fixture and prose docs often introduce a setting in one bullet and state its default in the next.
            # Cross-line linking is allowed only for a unique subject within two lines, keeping ambiguity quiet.
            if not nearby:
                nearby = [item for item in subject_pool if abs(item.line - line_number) <= MAX_DEFAULT_LINK_DISTANCE]
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
        # Example commands (`blackd --bind-port 9090 &`, `curl localhost:9090`) pass values; they state no default.
        if (
            not line.lstrip().startswith("|")
            and line_number not in fence_lines
            and (
                re.search(r"\blistens?\s+on\s+port\s*\d+", line, re.I)
                or (re.search(r"\bdefault", line, re.I) and re.search(r"\b(?:localhost|127\.0\.0\.1):\d+", line, re.I))
            )
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
