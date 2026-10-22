"""Extract claims from fenced examples as untrusted text; no snippet is executed."""

import ast
import configparser
import json
import os
import re
import shlex
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from markdown_it.token import Token

from docs_drift.docs.sections import ParsedDoc, is_historical_span
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction
from docs_drift.safe_files import metadata_text

_SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", "powershell", "ps1", "cmd"}
_LANGS = _SHELL_LANGS | {
    "python",
    "py",
    "js",
    "javascript",
    "ts",
    "typescript",
    "json",
    "yaml",
    "yml",
    "toml",
    "env",
    "dotenv",
    "http",
}
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_CURL_METHOD = re.compile(r"(?:^|\s)(?:-X|--request)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", re.I)
_URL = re.compile(r"https?://[^\s'\"]+", re.I)
ClaimCounter = Counter[tuple[str, ClaimKind, str]]
MIN_MODULE_COMMAND_TOKENS = 3
MIN_WRAPPED_COMMAND_TOKENS = 3
MIN_COMMAND_ARGUMENTS = 2
WRAPPED_COMMAND_OFFSET = 3
COMMAND_TARGET_OFFSET = 2


@dataclass(frozen=True)
class BlockExtractionResult:
    claims: list[Claim]
    historical_skipped: int


def project_command_names(repo: Path) -> tuple[set[str], set[str]]:  # noqa: PLR0912
    """Read conventional packaging metadata and return executable names and Python modules."""
    commands: set[str] = set()
    modules: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            metadata = tomllib.loads(metadata_text(pyproject))
            scripts = metadata.get("project", {}).get("scripts", {})
            if isinstance(scripts, dict):
                commands.update(str(name) for name in scripts)
            for name in metadata.get("project", {}).get("name", "").replace("-", "_").split():
                modules.add(name)
            distribution_name = metadata.get("project", {}).get("name", "")
            if distribution_name:
                commands.add(distribution_name.casefold())
        except (OSError, tomllib.TOMLDecodeError):
            pass
    package_json = repo / "package.json"
    if package_json.is_file():
        try:
            metadata = json.loads(metadata_text(package_json))
            name = metadata.get("name")
            if isinstance(name, str) and name:
                commands.add(name)
                modules.add(name)
            bin_value = metadata.get("bin", {})
            if isinstance(bin_value, str) and isinstance(name, str):
                commands.add(name)
            elif isinstance(bin_value, dict):
                commands.update(str(command) for command in bin_value)
        except (OSError, json.JSONDecodeError):
            pass
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read_string(metadata_text(setup_cfg))
            for section in parser.sections():
                if section.startswith("options.entry_points"):
                    for name, _ in parser.items(section):
                        commands.add(name.strip())
        except (OSError, configparser.Error):
            pass
    for child in repo.iterdir() if repo.is_dir() else ():
        if child.is_dir() and (child / "__init__.py").is_file():
            modules.add(child.name)
        elif child.is_file() and child.suffix == ".py":
            modules.add(child.stem)
    return commands, modules


def _block_type(token: Token) -> str:
    info = token.info.strip().casefold()
    language = info.split(maxsplit=1)[0] if info else ""
    if language in _LANGS:
        return language
    content = token.content.lstrip()
    first = content.splitlines()[0].strip() if content else ""
    if first.startswith(("$ ", "> ")) or re.match(r"(?:npm|pip|uv|poetry|make|just|npx)\s+", first):
        return "shell"
    if content.startswith(("{", "[")):
        return "json"
    if re.match(r"^[A-Za-z_][\w.-]*\s*:\s*\S", first):
        return "yaml"
    return "text"


def _continued_lines(content: str, language: str) -> list[tuple[int, str]]:
    """Join only the shell-specific continuation marker, retaining the first physical line."""
    marker = "`" if language in {"powershell", "ps1"} else "^" if language == "cmd" else "\\"
    lines = content.splitlines()
    result: list[tuple[int, str]] = []
    pending = ""
    start = 1
    for number, source in enumerate(lines, 1):
        stripped = source.rstrip()
        if not pending:
            start = number
        if stripped.endswith(marker):
            pending += stripped[:-1] + " "
        else:
            result.append((start, pending + source))
            pending = ""
    if pending:
        result.append((start, pending))
    return result


def _split_shell(line: str) -> list[str]:
    """Split common shell operators without invoking a shell or expanding variables."""
    return [part.strip() for part in re.split(r"&&|\|\||[;|]", line) if part.strip()]


def _tokens(command: str, language: str) -> list[str]:
    try:
        return shlex.split(command, posix=language not in {"powershell", "ps1", "cmd"}, comments=True)
    except ValueError:
        # Malformed quoting should be ignored as an uncertain example, not guessed into a claim.
        return []


def _is_project_command(  # noqa: PLR0911
    tokens: list[str], repo: Path, names: set[str], modules: set[str]
) -> bool:
    if not tokens:
        return False
    executable = PurePosixPath(tokens[0].replace("\\", "/")).name.casefold()
    names_folded = {name.casefold() for name in names}
    if executable in names_folded or executable.removesuffix(".exe") in names_folded:
        return True
    if executable in {"python", "python3", "py"} and len(tokens) >= MIN_MODULE_COMMAND_TOKENS and tokens[1] == "-m":
        return tokens[2].split(".", 1)[0] in modules
    if executable == "npx" and len(tokens) >= MIN_COMMAND_ARGUMENTS:
        return tokens[1].casefold() in names_folded
    if executable in {"uv", "poetry"} and len(tokens) >= MIN_MODULE_COMMAND_TOKENS and tokens[1] == "run":
        return True
    if executable in {"npm", "pnpm", "yarn", "make", "just"} and len(tokens) >= MIN_COMMAND_ARGUMENTS:
        return tokens[1] == "run" and len(tokens) >= MIN_MODULE_COMMAND_TOKENS if executable == "npm" else True
    if tokens[0].startswith(("./", ".\\")) or "/" in tokens[0] or "\\" in tokens[0]:
        candidate = (repo / tokens[0].replace("\\", "/")).resolve()
        if candidate.is_relative_to(repo.resolve()) and candidate.exists():
            return True
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-pager",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-C",
                    str(repo),
                    "ls-files",
                    "--deleted",
                    "--",
                    tokens[0],
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _claim(  # noqa: PLR0913, PLR0917
    parsed: ParsedDoc,
    kind: ClaimKind,
    value: str,
    raw: str,
    line: int,
    column: int,
    extraction: Extraction,
    counts: ClaimCounter,
    *,
    qualifier: str | None = None,
    expected_default: str | None = None,
    confidence: float = 0.85,
) -> Claim | None:
    section = parsed.line_to_section.get(line)
    if section is None:
        return None
    key = (section.section_id, kind, value)
    occurrence = counts[key]
    counts[key] += 1
    return Claim(
        claim_id=make_claim_id(parsed.doc.path, section.heading_path, kind, value, occurrence),
        kind=kind,
        raw_text=raw.strip() or value,
        value=value,
        qualifier=qualifier,
        expected_default=expected_default,
        doc_path=parsed.doc.path,
        line=line,
        column=max(1, column),
        section_id=section.section_id,
        extraction=extraction,
        extraction_confidence=confidence,
    )


def _shell_block(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917
    parsed: ParsedDoc, token: Token, language: str, repo: Path, names: set[str], modules: set[str], counts: ClaimCounter
) -> list[Claim]:
    if token.map is None:
        return []
    claims: list[Claim] = []
    source_lines = token.content.splitlines()
    prompt_mode = any(line.lstrip().startswith(("$ ", "> ")) for line in source_lines)
    for local_line, source in _continued_lines(token.content, language):
        stripped = source.lstrip()
        if prompt_mode and not stripped.startswith(("$ ", "> ")):
            continue
        command_line = re.sub(r"^(?:\$\s|>\s)", "", stripped)
        # Captured --help rows are evidence too, even though they are output rather than runnable commands.
        help_flag = re.match(r"^\s*(--?[A-Za-z][\w-]*)\s+.*\[\s*default\s*:", source, re.I)
        if help_flag:
            line = token.map[0] + local_line + 1
            item = _claim(
                parsed,
                ClaimKind.cli_flag,
                help_flag.group(1),
                source.strip(),
                line,
                len(source) - len(source.lstrip()) + 1,
                Extraction.code_block,
                counts,
                confidence=0.75,
            )
            if item:
                claims.append(item)
        for part in _split_shell(command_line):
            tokens = _tokens(part, language)
            assignments: list[tuple[str, str]] = []
            while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
                match = _ASSIGNMENT.fullmatch(tokens.pop(0))
                assert match is not None
                assignments.append((match.group(1), match.group(2)))
            if not _is_project_command(tokens, repo, names, modules):
                continue
            line = token.map[0] + local_line + 1
            for key, _value in assignments:
                item = _claim(
                    parsed, ClaimKind.env_var, key, key, line, 1, Extraction.code_block, counts, confidence=0.85
                )
                if item:
                    claims.append(item)
            full = " ".join(tokens)
            item = _claim(parsed, ClaimKind.code_command, full, full, line, 1, Extraction.code_block, counts)
            if item:
                claims.append(item)
            wrapper = tokens[0].casefold()
            if wrapper in {"npm", "pnpm", "yarn", "make", "just"}:
                target_index = COMMAND_TARGET_OFFSET if wrapper == "npm" else 1
                if len(tokens) > target_index:
                    target_kind = (
                        ClaimKind.package_script if wrapper in {"npm", "pnpm", "yarn"} else ClaimKind.make_target
                    )
                    target_claim = _claim(
                        parsed,
                        target_kind,
                        tokens[target_index],
                        " ".join(tokens[: target_index + 1]),
                        line,
                        1,
                        Extraction.code_block,
                        counts,
                    )
                    if target_claim:
                        claims.append(target_claim)
            positional: list[str] = []
            positional_seen = False
            command_start = 1
            program = tokens[0].casefold()
            if (
                program in {"python", "python3", "py"}
                and len(tokens) >= MIN_MODULE_COMMAND_TOKENS
                and tokens[1] == "-m"
            ) or (program in {"uv", "poetry"} and len(tokens) >= MIN_MODULE_COMMAND_TOKENS and tokens[1] == "run"):
                command_start = WRAPPED_COMMAND_OFFSET
            elif program == "npx" and len(tokens) >= MIN_COMMAND_ARGUMENTS:
                command_start = MIN_COMMAND_ARGUMENTS
            elif program == "npm" and len(tokens) >= MIN_MODULE_COMMAND_TOKENS and tokens[1] == "run":
                command_start = WRAPPED_COMMAND_OFFSET
            elif program in {"pnpm", "yarn", "make", "just"} and len(tokens) >= MIN_COMMAND_ARGUMENTS:
                command_start = MIN_COMMAND_ARGUMENTS
            index = command_start
            while index < len(tokens):
                value = tokens[index]
                if value.startswith("-"):
                    flag = value.split("=", 1)[0]
                    item = _claim(
                        parsed,
                        ClaimKind.cli_flag,
                        flag,
                        value,
                        line,
                        1,
                        Extraction.code_block,
                        counts,
                        qualifier=positional[-1] if positional else None,
                    )
                    if item:
                        claims.append(item)
                    if "=" not in value and index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                        index += 1
                elif not positional_seen and value not in {"run", "exec"}:
                    positional_seen = True
                    # Paths, URLs and numeric arguments are not subcommand names.
                    # Keep them in the complete command for separate validation.
                    if re.fullmatch(r"[A-Za-z][\w-]*", value):
                        positional.append(value)
                        item = _claim(
                            parsed, ClaimKind.cli_command, value, value, line, 1, Extraction.code_block, counts
                        )
                        if item:
                            claims.append(item)
                index += 1
    return claims


def _config_claims(parsed: ParsedDoc, token: Token, language: str, counts: ClaimCounter) -> list[Claim]:
    if token.map is None:
        return []
    section = parsed.line_to_section.get(token.map[0] + 1)
    info = token.info.casefold()
    filename_hint = bool(re.search(r"(?:title|filename|file)=['\"]?[^\s'\"]+\.(?:toml|json|ya?ml)", info))
    if not filename_hint and (
        section is None
        or not any(word in heading.casefold() for heading in section.heading_path for word in ("config", "setting"))
    ):
        return []
    try:
        if language == "json":
            data = json.loads(token.content)
        elif language == "toml":
            data = tomllib.loads(token.content)
        else:
            data = _simple_yaml(token.content)
    except (ValueError, tomllib.TOMLDecodeError):
        return []
    line_start = token.map[0] + 2
    document_lines = parsed.text.splitlines()
    config_lines = token.content.splitlines()
    prose_default = any(
        "default" in line.casefold() for line in document_lines[max(0, token.map[0] - 2) : token.map[0]]
    )
    claims: list[Claim] = []
    for dotted, line, value in _flatten_config(data, config_lines, line_start):
        item = _claim(
            parsed,
            ClaimKind.config_key,
            dotted,
            dotted,
            line,
            1,
            Extraction.code_block,
            counts,
            expected_default=str(value) if prose_default else None,
        )
        if item:
            claims.append(item)
    return claims


def _simple_yaml(text: str) -> dict[str, Any]:
    """Read ordinary key/value YAML indentation without tags, aliases, or constructors."""
    root: dict[str, Any] = {}
    parents: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line in text.splitlines():
        match = re.match(r"^(\s*)([A-Za-z_][\w-]*):(?:\s*(.*))?$", line)
        if not match:
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3) or ""
        while parents and indent <= parents[-1][0]:
            parents.pop()
        container = parents[-1][1] if parents else root
        if value.strip():
            container[key] = value.strip().strip("'\"")
        else:
            child: dict[str, Any] = {}
            container[key] = child
            parents.append((indent, child))
    return root


def _flatten_config(data: Any, lines: list[str], first_line: int, prefix: str = "") -> list[tuple[str, int, Any]]:
    result: list[tuple[str, int, Any]] = []
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        line = next(
            (first_line + i for i, source in enumerate(lines) if re.match(rf"\s*{re.escape(str(key))}\s*[:=]", source)),
            first_line,
        )
        if isinstance(value, dict):
            result.extend(_flatten_config(value, lines, first_line, dotted))
        else:
            result.append((dotted, line, value))
    return result


def _dotenv_claims(parsed: ParsedDoc, token: Token, counts: ClaimCounter) -> list[Claim]:
    if token.map is None:
        return []
    result: list[Claim] = []
    for offset, line in enumerate(token.content.splitlines()):
        match = _ASSIGNMENT.fullmatch(line.strip())
        if not match:
            continue
        item = _claim(
            parsed,
            ClaimKind.env_var,
            match.group(1),
            match.group(1),
            token.map[0] + offset + 2,
            1,
            Extraction.code_block,
            counts,
        )
        if item:
            result.append(item)
    return result


def _http_claims(parsed: ParsedDoc, token: Token, counts: ClaimCounter, language: str) -> list[Claim]:
    if token.map is None:
        return []
    result: list[Claim] = []
    pending_method = "GET"
    for offset, line in enumerate(token.content.splitlines()):
        stripped = line.strip()
        method = re.search(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(https?://[^\s]+|/[^\s]+)", stripped, re.I)
        curl = _URL.search(stripped) if language in _SHELL_LANGS else None
        curl_method = _CURL_METHOD.search(stripped) if curl else None
        if method:
            pending_method, path = method.group(1).upper(), method.group(2)
            if path.startswith(("http://", "https://")):
                path = urlsplit(path).path or "/"
        elif curl:
            pending_method = curl_method.group(1).upper() if curl_method else "GET"
            path = urlsplit(curl.group(0)).path or "/"
        else:
            continue
        item = _claim(
            parsed,
            ClaimKind.endpoint,
            f"{pending_method} {path}",
            stripped,
            token.map[0] + offset + 2,
            1,
            Extraction.code_block,
            counts,
        )
        if item:
            result.append(item)
    return result


def _import_claims(  # noqa: PLR0912
    parsed: ParsedDoc, token: Token, language: str, modules: set[str], counts: ClaimCounter
) -> list[Claim]:
    if token.map is None:
        return []
    imports: set[str] = set()
    claims = []
    if language in {"python", "py"}:
        try:
            tree = ast.parse(token.content)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".", 1)[0] in modules:
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports.add(name)
                    item = _claim(
                        parsed,
                        ClaimKind.symbol,
                        name,
                        node.module + "." + alias.name,
                        token.map[0] + node.lineno + 1,
                        1,
                        Extraction.code_block,
                        counts,
                    )
                    if item:
                        claims.append(item)
    else:
        for match in re.finditer(r"import\s+(?:\{([^}]+)\}\s+from\s+)?['\"]([^'\"]+)['\"]", token.content):
            package = match.group(2).split("/", 1)[0]
            if package in modules:
                imports.update(name.strip().split(" as ")[-1] for name in (match.group(1) or package).split(","))
                for imported in (match.group(1) or package).split(","):
                    symbol = imported.strip().split(" as ")[0].strip()
                    if symbol:
                        item = _claim(
                            parsed,
                            ClaimKind.symbol,
                            symbol,
                            symbol,
                            token.map[0] + 2 + token.content.count("\n", 0, match.start()),
                            1,
                            Extraction.code_block,
                            counts,
                        )
                        if item:
                            claims.append(item)
    for name in imports:
        for match in re.finditer(rf"\b{re.escape(name)}(?:\.([A-Za-z_]\w*))?\s*\(", token.content):
            value = name + ("." + match.group(1) if match.group(1) else "")
            line = token.map[0] + token.content.count("\n", 0, match.start()) + 2
            item = _claim(
                parsed, ClaimKind.symbol, value, match.group(0).strip(), line, 1, Extraction.code_block, counts
            )
            if item:
                claims.append(item)
    return claims


def extract_block_claims(
    parsed: ParsedDoc, repo: Path, *, command_context: tuple[set[str], set[str]] | None = None
) -> BlockExtractionResult:
    """Extract claims using only fenced text and repository metadata."""
    names, modules = command_context if command_context is not None else project_command_names(repo)
    counts: ClaimCounter = Counter()
    claims = []
    for token in parsed.tokens:
        if token.type != "fence":
            continue
        language = _block_type(token)
        if language in _SHELL_LANGS:
            claims.extend(_shell_block(parsed, token, language, repo, names, modules, counts))
            if "curl" in token.content:
                claims.extend(_http_claims(parsed, token, counts, language))
        elif language == "http":
            claims.extend(_http_claims(parsed, token, counts, language))
        elif language in {"env", "dotenv"}:
            claims.extend(_dotenv_claims(parsed, token, counts))
        elif language in {"json", "yaml", "yml", "toml"}:
            claims.extend(_config_claims(parsed, token, language, counts))
        elif language in {"python", "py", "js", "javascript", "ts", "typescript"}:
            claims.extend(_import_claims(parsed, token, language, modules, counts))
    current = []
    historical = 0
    for claim in claims:
        section = parsed.line_to_section.get(claim.line)
        if section is not None and is_historical_span(section, claim.line):
            historical += 1
        else:
            current.append(claim)
    return BlockExtractionResult(current, historical)
