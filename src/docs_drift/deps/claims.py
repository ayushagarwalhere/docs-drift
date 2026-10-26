"""Extract dependency and runtime-version claims from documentation text; examples are never executed."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import unquote

from docs_drift.deps.releases import table_header
from docs_drift.docs.extract_blocks import (
    _SHELL_LANGS,
    ClaimCounter,
    _block_type,
    _claim,
    _continued_lines,
    _split_shell,
    _tokens,
)
from docs_drift.docs.sections import ParsedDoc
from docs_drift.models import Claim, ClaimKind, Extraction

PYPI = "pypi"
NPM = "npm"
PROJECT = "."
_RUNTIME = r"(?P<rt>python|cpython|node\.?js|node)"
_VERSION = r"v?(?P<ver>\d+(?:\.\d+){0,2})"
_LATER = r"(?:later|newer|higher|above|greater|up)"
_EN_DASH = chr(0x2013)
_LEFT = r"(?<![\w/.@-])"
_PATTERNS = (
    re.compile(_LEFT + _RUNTIME + r"\s*(?:version\s*)?(?P<op>>=|≥|=>)\s*" + _VERSION + r"(?!\.?\d)", re.I),
    re.compile(
        _LEFT
        + _RUNTIME
        + r"\s+(?:version\s+)?"
        + _VERSION
        + r"(?:\s*\+|\s+(?:or|and)\s+(?:any\s+)?"
        + _LATER
        + r"\b|\s+"
        + _LATER
        + r"\b)",
        re.I,
    ),
    re.compile(
        r"\b(?:requires?|requiring|needs?|minimum(?:\s+supported)?(?:\s+version)?(?:\s+of)?|at\s+least|depends\s+on)"
        r"\s+" + _RUNTIME + r"\s+(?:version\s+)?" + _VERSION + r"(?!\.?\d|\s*(?:\+|-|" + _EN_DASH + r"|to)\s*\d)",
        re.I,
    ),
    re.compile(
        _LEFT
        + _RUNTIME
        + r"\s+(?:version\s+)?"
        + _VERSION
        + r"\s*(?:-|"
        + _EN_DASH
        + r"|to|through)\s*v?\d+(?:\.\d+){0,2}\b",
        re.I,
    ),
)
_BADGE = re.compile(
    r"badge/(?P<rt>python|node(?:\.?js)?)-v?(?P<op>>=|≥)?\s*(?P<ver>\d+(?:\.\d+){0,2})(?P<plus>\+)?", re.I
)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PIP_OPTIONS_WITH_VALUE = {
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "-i",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "-t",
    "--target",
    "--prefix",
    "--root",
    "--python",
    "-p",
    "--src",
    "--platform",
    "--python-version",
    "--implementation",
    "--abi",
    "--trusted-host",
    "--with",
    "--group",
    "--optional",
    "--extra",
}
_NPM_OPTIONS_WITH_VALUE = {"-w", "--workspace", "--filter", "--registry", "--prefix", "--tag", "--cwd"}
_INSTALLER = re.compile(
    r"^(?:sudo\s+)?(?:pip3?|pipx|uv|uvx|poetry|pdm|python3?|py|npm|pnpm|yarn|bun)(?:\.exe)?\s", re.I
)
MIN_PYTHON_MAJOR = 2
MAX_PYTHON_MAJOR = 3
MAX_NODE_MAJOR = 99
MIN_NODE_MAJOR = 4
_OPERATOR_PATTERN, _REQUIRES_PATTERN, _RANGE_PATTERN = 0, 2, 3
# Wording that makes a bare "Node 18 or later" the JavaScript runtime rather than a graph or cluster node.
_NODE_CONTEXT = re.compile(
    r"\b(?:npm|npx|pnpm|yarn|bun|deno|engines?|package\.json|nvm|volta|install\w*|requir\w*|prerequisites?|"
    r"dependenc\w*|compat\w*|supported|runtimes?|toolchain|lts|upgrade|build|environment|"
    r"works?\s+(?:on|with)|runs?\s+on|javascript|typescript|python)\b",
    re.IGNORECASE,
)
_HEADING_CONTEXT = re.compile(
    r"\b(?:install\w*|setup|set\s+up|getting\s+started|requirements?|prerequisites?|compatibility|"
    r"dependenc\w*|engines?|runtimes?|toolchain|development|environment|supported\s+(?:versions|platforms|runtimes))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InstallCommand:
    """Requirements named by one documented install command, in written order."""

    registry: str
    requirements: list[str] = field(default_factory=list)
    global_install: bool = False


def _executable(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")


def _project_reference(token: str) -> str | None:
    match = re.fullmatch(r"\.?/?\.?(\[[A-Za-z0-9_.,\s-]+\])?", token)
    if match is None or token in {"", "/"}:
        return None
    return PROJECT + (re.sub(r"\s+", "", match[1]) if match[1] else "")


def _pypi_arguments(tokens: list[str]) -> list[str]:
    result = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in {"-e", "--editable"}:
            continue
        value = token.split("=", 1)[1] if token.startswith("--editable=") else token
        if value.startswith("-"):
            skip = value in _PIP_OPTIONS_WITH_VALUE
            continue
        project = _project_reference(value)
        if project is not None:
            result.append(project)
        elif not re.search(r"[/\\$]|://|\.(?:whl|zip|tar\.gz|tgz)$|^git\+", value):
            result.append(value)
    return result


def _npm_arguments(tokens: list[str]) -> tuple[list[str], bool]:
    result = []
    skip = False
    global_install = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in {"-g", "--global"}:
                global_install = True
            elif token in _NPM_OPTIONS_WITH_VALUE:
                skip = True
            continue
        if re.search(r"://|^(?:git|github|gitlab|bitbucket|file|link|workspace|npm):|^[./~\\]|\.tgz$", token):
            continue
        result.append(token)
    return result, global_install


def parse_install(tokens: list[str]) -> InstallCommand | None:  # noqa: PLR0911, PLR0912 - one branch per installer.
    """Recognise package-manager install commands and return the requirements they name."""
    if tokens and tokens[0] == "sudo":
        tokens = tokens[1:]
    if not tokens:
        return None
    executable = _executable(tokens[0])
    rest = tokens[1:]
    if executable in {"python", "python3", "py"}:
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in {"pip", "pip3"}:  # noqa: PLR2004 - "-m pip".
            executable, rest = "pip", rest[2:]
        else:
            return None
    if executable == "uv" and rest[:1] == ["pip"]:
        executable, rest = "pip", rest[1:]
    if executable == "uv" and rest[:1] == ["tool"]:
        executable, rest = "pipx", rest[1:]
    if executable in {"pip", "pip3", "pipx"}:
        if rest[:1] != ["install"]:
            return None
        return InstallCommand(PYPI, _pypi_arguments(rest[1:]))
    if executable in {"uv", "poetry", "pdm"}:
        if rest[:1] != ["add"]:
            return None
        return InstallCommand(PYPI, _pypi_arguments(rest[1:]))
    if executable == "uvx":
        for index, token in enumerate(rest[:-1]):
            if token == "--from":
                return InstallCommand(PYPI, _pypi_arguments([rest[index + 1]]))
            if token.startswith("--from="):
                return InstallCommand(PYPI, _pypi_arguments([token.split("=", 1)[1]]))
        return None
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        verbs = {"npm": {"install", "i", "add"}, "pnpm": {"add", "install", "i"}, "yarn": {"add"}, "bun": {"add"}}
        if executable == "yarn" and rest[:1] == ["global"]:
            rest = rest[1:]
            requirements, _ = _npm_arguments(rest[1:]) if rest[:1] == ["add"] else ([], False)
            return InstallCommand(NPM, requirements, True) if requirements else None
        if not rest or rest[0] not in verbs[executable]:
            return None
        requirements, global_install = _npm_arguments(rest[1:])
        return InstallCommand(NPM, requirements, global_install) if requirements else None
    return None


def install_commands(command_line: str, language: str = "shell") -> list[InstallCommand]:
    """Parse every install command in one shell line without invoking a shell."""
    commands = []
    for part in _split_shell(command_line):
        parsed = parse_install(_tokens(part, language))
        if parsed is not None and parsed.requirements:
            commands.append(parsed)
    return commands


def _runtime_value(runtime: str, operator: str, version: str) -> str | None:
    family = "python" if "python" in runtime.casefold() else "node"
    numbers = [int(part) for part in version.split(".")]
    if family == "python":
        if len(numbers) < 2 or not MIN_PYTHON_MAJOR <= numbers[0] <= MAX_PYTHON_MAJOR:  # noqa: PLR2004 - X.Y.
            return None
    elif numbers[0] > MAX_NODE_MAJOR or not _plausible_node(numbers):
        return None
    return f"{family}{operator}{version}"


def _plausible_node(numbers: list[int]) -> bool:
    """Node.js went from 0.12 straight to 4; majors 1-3 (and a bare 0) are never Node.js releases."""
    if numbers[0] == 0:
        return len(numbers) > 1
    return numbers[0] >= MIN_NODE_MAJOR


def _node_context(text: str) -> bool:
    return _NODE_CONTEXT.search(text) is not None


def _bare_node_accepted(match: re.Match[str], pattern_index: int, text: str, context: str = "") -> bool:
    """'node' alone is also a graph or cluster word ("requires node 2", "Node 5 and up are leaves").

    'Node.js'/'NodeJS' always count. A bare 'node' counts only with an explicit version marker
    ('node >= 16', 'Node v18', 'Node version 18') or, when spelled 'Node' or written 'node 18+', inside
    runtime context: install/requirements/compatibility wording or npm/engines on the line, or such a
    section heading or table header (`context`). The "requires/needs node N" phrasing never qualifies alone.
    """
    runtime = match["rt"]
    if runtime.casefold() != "node":
        return True
    if pattern_index == _REQUIRES_PATTERN:
        return False
    prefix = text[match.start("ver") - 1 : match.start("ver")]
    if pattern_index == _OPERATOR_PATTERN or prefix in {"v", "V"} or re.search(r"(?i)\bversion\b", match[0]):
        return True
    if pattern_index == _RANGE_PATTERN or not (runtime == "Node" or "+" in match[0]):
        return False
    rest = text[: match.start()] + " " + text[match.end() :]
    return _node_context(rest) or _HEADING_CONTEXT.search(context) is not None


def runtime_claims_in_text(text: str, context: str = "") -> list[tuple[str, str, int]]:
    """Return (value, matched text, column offset) for runtime-version statements in one line of prose.

    `context` is the enclosing section heading path and table header, used only to accept a bare "Node".
    """
    found: list[tuple[str, str, int]] = []
    families: set[str] = set()
    decoded = unquote(text) if "badge/" in text else text
    for match in _BADGE.finditer(decoded):
        if not (match["op"] or match["plus"]):
            continue
        value = _runtime_value(match["rt"], ">=", match["ver"])
        if value and value.split(">=")[0] not in families:
            families.add(value.split(">=")[0])
            found.append((value, match[0], max(0, text.find("badge/"))))
    for index, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            if not _bare_node_accepted(match, index, text, context):
                continue
            value = _runtime_value(match["rt"], ">=", match["ver"])
            if value is None:
                continue
            family = value.split(">=")[0]
            if family in families:
                continue
            families.add(family)
            found.append((value, match[0].strip(), match.start("rt")))
    return found


def _fenced_lines(parsed: ParsedDoc) -> set[int]:
    lines: set[int] = set()
    for token in parsed.tokens:
        if token.type in {"fence", "code_block"} and token.map is not None:
            lines.update(range(token.map[0] + 1, token.map[1] + 1))
    return lines


def _install_claims(  # noqa: PLR0913, PLR0917 - claim location context.
    parsed: ParsedDoc, command_line: str, line: int, column: int, extraction: Extraction, counts: ClaimCounter
) -> list[Claim]:
    claims = []
    for command in install_commands(command_line):
        raw = " ".join([command.registry, *command.requirements])
        for requirement in command.requirements:
            item = _claim(
                parsed,
                ClaimKind.dependency,
                requirement,
                command_line.strip() or raw,
                line,
                column,
                extraction,
                counts,
                qualifier=command.registry + (":global" if command.global_install else ""),
                confidence=0.8,
            )
            if item:
                claims.append(item)
    return claims


def _context(parsed: ParsedDoc, line: int, *, table: bool) -> str:
    """Section headings, plus the header row of a pipe table, around one 1-based line."""
    section = parsed.line_to_section.get(line)
    parts = list(section.heading_path) if section is not None else []
    if table:
        header = table_header(parsed.lines, line - 1)
        if header:
            parts.extend(header)
    return " / ".join(parts)


def extract_dependency_claims(parsed: ParsedDoc) -> list[Claim]:
    """Dependency installs (code blocks and inline code) and runtime minimums (prose, tables, badges)."""
    counts: ClaimCounter = Counter()
    claims: list[Claim] = []
    fenced = _fenced_lines(parsed)
    for token in parsed.tokens:
        if token.type != "fence" or token.map is None:
            continue
        language = _block_type(token)
        if language not in _SHELL_LANGS and language not in {"text", "shell"}:
            continue
        prompt_mode = any(line.lstrip().startswith(("$ ", "> ")) for line in token.content.splitlines())
        for local_line, source in _continued_lines(token.content, language):
            stripped = source.strip()
            if prompt_mode and not stripped.startswith(("$ ", "> ")):
                continue
            command = re.sub(r"^(?:\$\s|>\s|PS>\s?)", "", stripped)
            if not _INSTALLER.match(command + " "):
                continue
            line = token.map[0] + local_line + 1
            text_line = parsed.lines[line - 1] if line <= len(parsed.lines) else command
            column = max(0, text_line.find(command.split()[0])) + 1
            claims.extend(_install_claims(parsed, command, line, column, Extraction.code_block, counts))
    for number, text in enumerate(parsed.lines, 1):
        if number in fenced or not text.strip():
            continue
        for match in _INLINE_CODE.finditer(text):
            if _INSTALLER.match(match[1].strip() + " "):
                claims.extend(
                    _install_claims(parsed, match[1], number, match.start(1) + 1, Extraction.inline_code, counts)
                )
        table = text.lstrip().startswith("|")
        prose = re.sub(r"\s*\|\s*", "  ", text) if table else text
        for value, raw, offset in runtime_claims_in_text(prose, _context(parsed, number, table=table)):
            item = _claim(
                parsed,
                ClaimKind.runtime_version,
                value,
                raw,
                number,
                (text.find(raw) if text.find(raw) >= 0 else offset) + 1,
                Extraction.table if table else Extraction.prose_pattern,
                counts,
                qualifier=value.split(">=")[0],
                confidence=0.7 if table or "badge/" in raw.casefold() else 0.75,
            )
            if item:
                claims.append(item)
    # Release-history rows and older side-by-side minimums are set aside in check.py, which knows the manifests.
    return claims
