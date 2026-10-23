"""Conservative extraction of checkable claims from prose and Markdown tokens."""

import re
from collections import Counter
from dataclasses import dataclass

from markdown_it.token import Token

from docs_drift.docs.sections import ParsedDoc, is_historical_span
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction

# Require all-caps separators or a known suffix for environment names; the stoplist
# removes common headings and HTTP vocabulary that happen to look capitalized.
_ENV_STOP = {"HTTP", "JSON", "API", "README", "TODO", "GET", "POST", "NOTE", "MIT"}
_ENV_SUFFIX = ("URL", "KEY", "TOKEN", "PATH", "HOST", "PORT", "DIR")
MIN_SUBCOMMAND_HEADING_LEVEL = 2
_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^[A-Z][A-Z0-9]*(?:URL|KEY|TOKEN|PATH|HOST|PORT|DIR)$")
# Flag assignments are accepted so the value can be separated as example data.
_FLAG = re.compile(r"^--?[A-Za-z][\w-]*(?:=.*)?$")
# Endpoints are limited to common HTTP methods and absolute paths; parameter
# names are normalized separately so `/users/{id}` matches `/users/:user_id`.
_ENDPOINT = re.compile(r"^(?:(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(/[^\s]*)$", re.IGNORECASE)
_PATH_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_]\w*|<[A-Za-z_]\w*>")
# This short extension set avoids treating arbitrary dotted prose as a file path.
_FILE_EXT = re.compile(r"\.(?:py|pyi|js|jsx|ts|tsx|json|toml|ya?ml|mdx?|rst|txt|html?|env)(?:$|[?#])", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^(?:[A-Za-z_]\w*(?:\(\))?|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)$")
_CAMEL_OR_SNAKE = re.compile(r"^(?:[a-z]+(?:_[a-z0-9]+)+|[A-Z][a-zA-Z0-9]+)$")
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|$)")
_DEFAULT = re.compile(
    r"\bdefaults?\s+to\s+`?(?P<to>[^``,;)]+)`?|"
    r"\bdefault\s*(?::|is)\s*`?(?P<colon>[^``,;)]+)`?|"
    r"\(\s*default\s+`?(?P<paren>[^`)]+)`?\s*\)",
    re.IGNORECASE,
)
_SUBCOMMAND = re.compile(r"\b(?:the\s+)?`?([a-z][\w-]*)`?\s+(?:sub)?command\b", re.IGNORECASE)
_COMMAND_NAME = re.compile(r"`([a-z][\w-]*)`\s+subcommand\b", re.IGNORECASE)
_TOOL_COMMAND = re.compile(
    r"^(?P<tool>npm\s+run|pnpm|yarn|make|just|uv\s+run|poetry\s+run)\s+(?P<target>[\w:.-]+)(?:\s+.*)?$", re.IGNORECASE
)
_NPX_COMMAND = re.compile(r"^npx\s+[^\s]+(?:\s+.*)?$", re.IGNORECASE)
_WRAPPED_ENV = re.compile(
    r"^(?:process\.env\.(?P<process>[A-Za-z_]\w*)|os\.environ\[\s*['\"](?P<environ>[A-Za-z_]\w*)['\"]\s*\]|os\.getenv\(\s*['\"](?P<getenv>[A-Za-z_]\w*)['\"]\s*\)|\$(?:\{(?P<braced>[A-Za-z_]\w*)\}|(?P<shell>[A-Za-z_]\w*)))$"
)


@dataclass(frozen=True)
class ExtractionResult:
    """Claims plus the number of candidate mentions suppressed as historical."""

    claims: list[Claim]
    historical_skipped: int


@dataclass(frozen=True)
class _Candidate:
    kind: ClaimKind
    value: str
    line: int
    column: int
    raw_text: str
    extraction: Extraction
    confidence: float
    expected_default: str | None = None
    qualifier: str | None = None


def _environment(value: str) -> str | None:
    if re.fullmatch(r"[A-Z][A-Z0-9_]+=\S+", value):
        value = value.partition("=")[0]
    wrapped = _WRAPPED_ENV.fullmatch(value)
    if wrapped:
        return next(part for part in wrapped.groupdict().values() if part is not None)
    if value in _ENV_STOP:
        return None
    if value in _ENV_SUFFIX:
        return value
    if not _ENV_PATTERN.fullmatch(value):
        return None
    # Exclude unseparated acronym-like words; suffix recognition is handled above.
    if "_" not in value and not value.endswith(_ENV_SUFFIX):
        return None
    return value


def _explicit_api_reference(value: str, sentence: str) -> bool:
    """Disambiguate dotted API names using their own local grammatical context."""
    if "." not in value or not _IDENTIFIER.fullmatch(value):
        return False
    quoted = r"[`'\"]*" + re.escape(value) + r"[`'\"]*"
    role = r"(?:module|attribute|property|object|instance)"
    return bool(
        re.search(r"\b" + role + r"\s+(?:named\s+)?" + quoted, sentence, re.I)
        or re.search(quoted + r"\s+" + role + r"\b", sentence, re.I)
        or re.search(
            r"\b(?:functions?|methods?)\s+(?:provided\s+by|available\s+on|in|on|of|from)\s+" + quoted,
            sentence,
            re.I,
        )
    )


def _classify(  # noqa: PLR0911
    value: str, *, api_reference: bool = False, sentence: str = ""
) -> tuple[ClaimKind, str, float] | None:
    """Prefer narrow lexical shapes; prose context only upgrades clear identifiers to symbols."""
    command = _TOOL_COMMAND.fullmatch(value)
    if command:
        tool = command.group("tool").casefold().split()[0]
        target = command.group("target")
        kind = ClaimKind.make_target if tool in {"make", "just"} else ClaimKind.package_script
        return kind, target, 0.9
    if _NPX_COMMAND.fullmatch(value):
        # Preserve the full invocation so command-specific flags remain attached to their owning tool.
        return ClaimKind.code_command, value, 0.8
    env = _environment(value)
    if env:
        # The explicit 'set' extractor already retains its name/value pair.
        if "=" in value:
            return (
                None
                if re.search(r"\bset\s+`" + re.escape(value) + r"`", sentence, re.I)
                else (ClaimKind.env_var, env, 0.85)
            )
        return ClaimKind.env_var, env, 0.95 if env == value else 0.9
    if re.search(r"\bsubcommand\b", sentence, re.IGNORECASE) and re.fullmatch(r"[a-z][\w-]*", value):
        return ClaimKind.cli_command, value, 0.8
    endpoint = _ENDPOINT.fullmatch(value)
    if endpoint:
        method, path = endpoint.groups()
        normalized = _PATH_PARAM.sub("{param}", path)
        return ClaimKind.endpoint, f"{method.upper()} {normalized}" if method else normalized, 0.95
    if _FLAG.fullmatch(value):
        name = value.split("=", 1)[0]
        # A short single-dash option is weaker because prose often uses hyphenated examples.
        return ClaimKind.cli_flag, name, 0.9 if name.startswith("--") else 0.65
    if value.startswith(("http://", "https://", "www.", "#!")):
        return None
    return _path_or_symbol(value, api_reference=api_reference, sentence=sentence)


def _path_or_symbol(value: str, *, api_reference: bool, sentence: str) -> tuple[ClaimKind, str, float] | None:
    if _explicit_api_reference(value, sentence):
        return ClaimKind.symbol, value, 0.8
    if re.search(r"\b(?:setting|config(?:uration)?\s+key|field)\b", sentence, re.IGNORECASE) and re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value
    ):
        return ClaimKind.config_key, value, 0.75
    call = re.fullmatch(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(.*\)", value)
    if call:
        return ClaimKind.symbol, call[1], 0.8 if api_reference else 0.72
    # Known source and documentation extensions distinguish paths from dotted symbols.
    file_value = re.sub(r":\d+(?::\d+)?$", "", value)
    if (
        not re.search(r"\s|[*?]|^\.[A-Za-z]+$", value)
        and not re.search(r"\b(?:never commit|do not commit|glob|ignore)\b", sentence, re.I)
        and (value.startswith(".env.") or "/" in value or _FILE_EXT.search(file_value))
        and not value.startswith(("http://", "https://"))
    ):
        return ClaimKind.file_path, file_value, 0.88
    # A code command's own target is useful; flags belonging to unrelated tools are not.
    symbol_context = api_reference or re.search(r"\b(?:function|class|method|call|returns?)\b", sentence, re.IGNORECASE)
    # Factory context establishes the role independently of the identifier's verb prefix.
    application_factory = re.search(
        r"\b(?:create|build|construct)\s+the\s+application\s+with\b", sentence, re.IGNORECASE
    )
    if _IDENTIFIER.fullmatch(value) and (
        "(" in value
        or "." in value
        or (symbol_context and _CAMEL_OR_SNAKE.fullmatch(value))
        or (application_factory and _CAMEL_OR_SNAKE.fullmatch(value))
    ):
        return ClaimKind.symbol, value.removesuffix("()"), 0.72 if not api_reference else 0.8
    return None


def path_context(sentence: str, value: str = "") -> str | None:
    """Retain explicitly illustrative/output paths without asserting checkout existence."""
    if re.match(r"^[A-Za-z][\w+.-]*://|^@(?:[\w.-]+/|/)", value):
        return "non-checkout-path"
    quoted = r"[`'\"]" + re.escape(value) + r"[`'\"]"
    if value and re.search(
        r"\b(?:create|generate|write|save)\s+(?:(?:a|an|the|new)\s+)?"
        r"(?:(?:file|directory|directory tree|folder)\s+)?" + quoted,
        sentence,
        re.I,
    ):
        return "non-checkout-path"
    if re.search(
        r"\b(?:generated(?=\s+`)|generated (?:reports?|files?|artifacts?|(?:debug )?logs?)|output (?:files?|paths?)|"
        r"example (?:manifest|filename|path)|placeholder|shorthand|upstream source|external repository)\b",
        sentence,
        re.I,
    ):
        return "non-checkout-path"
    if (
        "/" in value
        and not re.search(r"\.[A-Za-z0-9]{1,8}$", value)
        and not re.search(r"\b(?:file|directory|folder|path)\b", sentence, re.I)
    ):
        return "ambiguous-path"
    return None


def _line_column(text: str, raw: str, cursor: list[int]) -> tuple[int, int]:
    """Recover positions from source text because markdown-it inline tokens omit maps."""
    needle = f"`{raw}`"
    start = text.find(needle, cursor[0])
    if start < 0:
        start = text.find(raw, cursor[0])
    if start < 0:
        return 1, 1
    cursor[0] = start + len(needle)
    return text.count("\n", 0, start) + 1, start - text.rfind("\n", 0, start)


def _sentence_for_line(text: str | list[str], line: int, column: int) -> str:
    lines = text.splitlines() if isinstance(text, str) else text
    if not (1 <= line <= len(lines)):
        return ""
    source = lines[line - 1]
    return next(
        (match.group() for match in _SENTENCE.finditer(source) if match.start() <= column <= match.end()), source
    )


def _qualifier(section_heading: str, sentence: str, heading_level: int) -> str | None:
    """Use only explicit command labels in the heading or sentence to avoid guessing ownership."""
    heading_match = re.fullmatch(r"\s*([\w-]+)\s*", section_heading)
    if heading_match and heading_level >= MIN_SUBCOMMAND_HEADING_LEVEL:
        return heading_match.group(1)
    match = _SUBCOMMAND.search(sentence)
    if match:
        return match.group(1)
    command = _COMMAND_NAME.search(sentence)
    if command:
        return command.group(1)
    # In compact bullets, an explicit command name directly before "accepts" is still clear ownership.
    accepts = re.search(r"`([a-z][\w-]*)`\s+accepts\b", sentence, re.IGNORECASE)
    return accepts.group(1) if accepts else None


def _inline_candidates(parsed: ParsedDoc) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    cursor = [0]
    block_line: int | None = None
    for token in parsed.tokens:
        if token.type == "paragraph_open" and token.map is not None:
            block_line = token.map[0] + 1
            continue
        if token.type == "paragraph_close":
            block_line = None
            continue
        if token.type != "inline" or token.children is None:
            continue
        for child in token.children:
            if child.type != "code_inline":
                continue
            raw = child.content
            line, column = _line_column(parsed.text, raw, cursor)
            if token.map is not None and line < token.map[0] + 1:
                line = token.map[0] + 1
            elif block_line is None:
                continue
            section = parsed.line_to_section.get(line)
            if section is None:
                continue
            sentence = _sentence_for_line(parsed.text, line, column)
            api_reference = any("api reference" in heading.casefold() for heading in section.heading_path)
            kind_and_value = _classify(raw, api_reference=api_reference, sentence=sentence)
            if kind_and_value is None:
                continue
            kind, value, confidence = kind_and_value
            qualifier = (
                _qualifier(section.heading, sentence, section.heading_level) if kind == ClaimKind.cli_flag else None
            )
            if kind == ClaimKind.file_path:
                qualifier = path_context(sentence, value)
                if qualifier is None and line > 1 and parsed.lines[line - 2].rstrip().endswith(":"):
                    qualifier = path_context(parsed.lines[line - 2], value)
            candidates.append(
                _Candidate(kind, value, line, column, raw, Extraction.inline_code, confidence, qualifier=qualifier)
            )
    return candidates


def _table_cells(tokens: list[Token]) -> list[tuple[list[tuple[str, int]], list[list[tuple[str, int]]]]]:
    """Read Markdown table cell inline text and source lines without interpreting HTML."""
    tables = []
    index = 0
    while index < len(tokens):
        if tokens[index].type != "table_open":
            index += 1
            continue
        index += 1
        header: list[tuple[str, int]] = []
        rows: list[list[tuple[str, int]]] = []
        current: list[tuple[str, int]] | None = None
        row_line = 1
        in_head = False
        while index < len(tokens) and tokens[index].type != "table_close":
            token = tokens[index]
            if token.type == "thead_open":
                in_head = True
            elif token.type == "thead_close":
                in_head = False
            elif token.type == "tr_open":
                current = []
                row_line = token.map[0] + 1 if token.map is not None else 1
            elif token.type == "inline" and current is not None:
                line = token.map[0] + 1 if token.map is not None else row_line
                current.append((token.content.strip(), line))
            elif token.type == "tr_close" and current is not None:
                if in_head:
                    header = current
                else:
                    rows.append(current)
                current = None
            index += 1
        if header:
            tables.append((header, rows))
        index += 1
    return tables


def _table_candidates(parsed: ParsedDoc) -> list[_Candidate]:
    candidates = []
    header_words = re.compile(r"\b(?:flag|option|argument|variable|env|setting|key|default|endpoint)\b", re.IGNORECASE)
    for header, rows in _table_cells(parsed.tokens):
        names = [cell.casefold() for cell, _ in header]
        if not any(header_words.search(cell) for cell in names):
            continue
        # The first column is the documented identifier; only a headed default column is a claimed value.
        default_index = next((i for i, name in enumerate(names) if name.strip() in {"default", "default value"}), None)
        first_heading = names[0] if names else ""
        for row in rows:
            if not row:
                continue
            raw, line = row[0]
            raw = raw.strip("`")
            normalized_value: str | None = None
            section = parsed.line_to_section.get(line)
            if section is None:
                continue
            sentence = " ".join(cell for cell, _ in row)
            if "flag" in first_heading or "option" in first_heading or "argument" in first_heading:
                classified = _classify(raw, sentence=sentence)
                kind = ClaimKind.cli_flag if classified and classified[0] == ClaimKind.cli_flag else None
            elif any(term in first_heading for term in ("env", "variable")):
                env = _environment(raw.strip("`"))
                kind = ClaimKind.env_var if env else None
                raw = raw.strip("`")
            elif "endpoint" in first_heading:
                classified = _classify(raw.strip("`"))
                kind = ClaimKind.endpoint if classified and classified[0] == ClaimKind.endpoint else None
                normalized_value = classified[1] if classified else None
            elif "setting" in first_heading or "key" in first_heading:
                config_key = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", raw.strip("`"))
                kind = ClaimKind.config_key if config_key else None
                raw = raw.strip("`")
            else:
                classified = _classify(raw.strip("`"), sentence=sentence)
                kind = classified[0] if classified else None
            if kind is None:
                continue
            value = normalized_value or raw.strip("`")
            default = row[default_index][0].strip() if default_index is not None and default_index < len(row) else None
            if default and default.startswith("`") and not re.fullmatch(r"`[^`]+`", default):
                default = None
            default = default.strip("` ") if default else None
            default = default if default and default.casefold() not in {"none", "n/a", "â€”", "-"} else None
            candidates.append(_Candidate(kind, value, line, 1, raw, Extraction.table, 0.88, expected_default=default))
    return candidates


def _code_block_candidates(parsed: ParsedDoc) -> list[_Candidate]:
    """Recognize only known project-command shapes in shell-labeled fences; never run them."""
    candidates = []
    for token in parsed.tokens:
        if token.type != "fence" or token.map is None:
            continue
        language = token.info.strip().casefold().split(maxsplit=1)[0] if token.info.strip() else ""
        if language not in {"sh", "shell", "bash", "zsh", "console"}:
            continue
        for offset, source in enumerate(token.content.splitlines()):
            command = source.strip()
            classified = _classify(command)
            if classified is None:
                continue
            kind, value, confidence = classified
            candidates.append(
                _Candidate(
                    kind,
                    value,
                    token.map[0] + offset + 2,
                    len(source) - len(source.lstrip()) + 1,
                    command,
                    Extraction.code_block,
                    confidence,
                )
            )
    return candidates


def _prose_candidates(parsed: ParsedDoc, candidates: list[_Candidate]) -> list[_Candidate]:
    result = []
    by_line: dict[int, list[_Candidate]] = {}
    for candidate in candidates:
        by_line.setdefault(candidate.line, []).append(candidate)
    for line, source in enumerate(parsed.text.splitlines(), 1):
        section = parsed.line_to_section.get(line)
        if section is None:
            continue
        for match in _DEFAULT.finditer(source):
            value = next(group for group in match.groupdict().values() if group is not None).strip()
            if not value:
                continue  # RST double-backtick defaults can otherwise yield a whitespace-only span.
            sentence = next(
                (item for item in _SENTENCE.finditer(source) if item.start() <= match.start() <= item.end()), None
            )
            sentence_start = sentence.start() if sentence else 0
            nearby = [
                candidate
                for candidate in by_line.get(line, [])
                if sentence_start <= candidate.column - 1 <= match.start()
            ]
            if not nearby:
                # Defaults without a named claim have weak value but remain useful for downstream linking.
                result.append(
                    _Candidate(
                        ClaimKind.default_value,
                        value,
                        line,
                        match.start() + 1,
                        match.group(),
                        Extraction.prose_pattern,
                        0.45,
                    )
                )
            else:
                result.append(
                    _Candidate(
                        ClaimKind.default_value,
                        value,
                        line,
                        match.start() + 1,
                        match.group(),
                        Extraction.prose_pattern,
                        0.5,
                        qualifier=nearby[-1].value,
                    )
                )
        for match in re.finditer(r"\bset\s+`([^`=]+)=([^`]+)`(?:\s+to\b)?", source, re.IGNORECASE):
            name, example = match.groups()
            # This targeted pattern preserves the setting name while treating its assignment as an example.
            result.append(
                _Candidate(
                    ClaimKind.env_var,
                    name,
                    line,
                    match.start(1) + 1,
                    match.group(1),
                    Extraction.prose_pattern,
                    0.55,
                )
            )
            result.append(
                _Candidate(
                    ClaimKind.default_value,
                    example,
                    line,
                    match.start(2) + 1,
                    example,
                    Extraction.prose_pattern,
                    0.4,
                    qualifier=name,
                )
            )
        for match in re.finditer(r"\bset\s+`([^`=]+)`\s+to\s+`([^`]+)`", source, re.IGNORECASE):
            name, example = match.groups()
            # A stated assignment is an example value, so preserve the setting name as its qualifier.
            result.append(
                _Candidate(
                    ClaimKind.default_value,
                    example,
                    line,
                    match.start(2) + 1,
                    example,
                    Extraction.prose_pattern,
                    0.4,
                    qualifier=name,
                )
            )
    return result


def extract_claims(parsed: ParsedDoc) -> ExtractionResult:
    """Extract high-signal spans first, tables next, then only explicit low-confidence prose forms."""
    candidates = _inline_candidates(parsed)
    # API-reference context is inherited from the section path and checked per span during classification.
    candidates.extend(_table_candidates(parsed))
    candidates.extend(_code_block_candidates(parsed))
    candidates.extend(_prose_candidates(parsed, candidates))
    table_mentions = {
        (candidate.line, candidate.raw_text.strip("`").strip())
        for candidate in candidates
        if candidate.extraction == Extraction.table
    }
    # A table's header supplies stronger kind context than the same first-column inline span.
    candidates = [
        candidate
        for candidate in candidates
        if not (
            candidate.extraction == Extraction.inline_code and (candidate.line, candidate.raw_text) in table_mentions
        )
    ]
    selected: list[_Candidate] = []
    skipped = 0
    seen: set[tuple[ClaimKind, str, int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: (item.line, item.column, item.kind.value, item.value)):
        section = parsed.line_to_section.get(candidate.line)
        if section is None:
            continue
        if is_historical_span(section, candidate.line):
            skipped += 1
            continue
        key = (candidate.kind, candidate.value, candidate.line, candidate.column)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
    occurrences: Counter[tuple[str, ClaimKind, str]] = Counter()
    claims = []
    for candidate in selected:
        section = parsed.line_to_section[candidate.line]
        occurrence_key = (section.section_id, candidate.kind, candidate.value)
        occurrence = occurrences[occurrence_key]
        occurrences[occurrence_key] += 1
        sentence = _sentence_for_line(parsed.lines, candidate.line, candidate.column)
        qualifier = candidate.qualifier
        if qualifier is None and candidate.kind == ClaimKind.cli_flag:
            qualifier = _qualifier(section.heading, sentence, section.heading_level)
        claims.append(
            Claim(
                claim_id=make_claim_id(
                    parsed.doc.path, section.heading_path, candidate.kind, candidate.value, occurrence
                ),
                kind=candidate.kind,
                raw_text=candidate.raw_text,
                value=candidate.value,
                qualifier=qualifier,
                expected_default=candidate.expected_default,
                doc_path=parsed.doc.path,
                line=candidate.line,
                column=candidate.column,
                section_id=section.section_id,
                extraction=candidate.extraction,
                extraction_confidence=candidate.confidence,
            )
        )
    return ExtractionResult(claims, skipped)
