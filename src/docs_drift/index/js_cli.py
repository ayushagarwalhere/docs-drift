"""Static CLI declarations for Commander, yargs, oclif, and cac."""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from docs_drift.index import register
from docs_drift.index.js_symbols import _module
from docs_drift.index.python_symbols import _symbol
from docs_drift.index.ts_parser import parser_for
from docs_drift.models import Symbol, SymbolKind

_COMMAND = re.compile(r"\.command\s*\(\s*(['\"])(.*?)\1", re.S)
_OPTION = re.compile(
    r"\.(?:requiredOption|option)\s*\(\s*(['\"])(.*?)\1"
    r"(?:\s*,\s*(['\"])(.*?)\3)?(?:\s*,\s*([^)]*?))?\s*\)",
    re.S,
)
_MAX_SOURCE_BYTES = 1_000_000


class JavaScriptCLIIndexer:
    name = "javascript-cli"
    languages = frozenset({"javascript", "typescript"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return (repo / "package.json").is_file()

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:  # noqa: PLR0912, PLR0915
        package_name, bins = _bin_names(repo)
        result = []
        for path in files:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    continue
                parser = parser_for("typescript" if path.suffix in {".ts", ".tsx", ".mts", ".cts"} else "javascript")
                if parser.parse(source.encode()).root_node.has_error:
                    continue
            except Exception:
                continue
            module = _module(repo, path)
            if not _is_cli_file(repo, path, source):
                continue
            root_name = package_name or next(iter(bins), "cli")
            root_symbol = _symbol(
                repo,
                path,
                SymbolKind.cli_command,
                f"{module}.{root_name}".strip("."),
                1,
                name=root_name,
                aliases=sorted(bins),
                framework=_framework(repo, source),
                language="typescript" if path.suffix in {".ts", ".tsx"} else "javascript",
            )
            result.append(root_symbol)
            command_symbols: dict[str, Symbol] = {}
            framework = root_symbol.framework or "commander"
            for match in _COMMAND.finditer(source):
                shape = match.group(2).split()
                if not shape:
                    continue
                name = shape[0]
                line = source.count("\n", 0, match.start()) + 1
                symbol = _symbol(
                    repo,
                    path,
                    SymbolKind.cli_command,
                    f"{module}.{name}".strip("."),
                    line,
                    name=name,
                    framework=framework,
                    language=root_symbol.language,
                )
                symbol.parent_id = root_symbol.symbol_id
                command_symbols[name] = symbol
                result.append(symbol)
                for positional in re.findall(r"[<\[]([\w-]+)[>\]]", " ".join(shape[1:])):
                    result.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.cli_argument,
                            f"{module}.{name}.{positional}",
                            line,
                            name=positional,
                            framework=framework,
                            language=root_symbol.language,
                        )
                    )
            for match in _OPTION.finditer(source):
                flag_text = match.group(2)
                pieces = [part.strip() for part in flag_text.split(",")]
                flags = []
                for part in pieces:
                    parsed_flag = re.match(r"(--?[\w-]+)(?:\s*([<\[])([^>\]]+)[>\]])?", part)
                    if parsed_flag is not None:
                        flags.append(parsed_flag)
                if not flags:
                    continue
                long_flag = next((flag for flag in flags if flag.group(1).startswith("--")), flags[0])
                name = long_flag.group(1)
                aliases = [flag.group(1) for flag in flags if flag is not long_flag]
                placeholder = long_flag.group(3)
                default = None
                if match.group(5):
                    value = re.search(r"(?:default|defaultValue)\s*:\s*([^,}]+)", match.group(5))
                    default = value.group(1).strip().strip("\"'") if value else None
                if framework == "commander" and match.group(4) is not None and placeholder is None:
                    default = match.group(4)
                if name.startswith("--no-"):
                    aliases.append(name)
                    name = "--" + name[5:]
                command_name = _nearest_command(source, match.start(), command_symbols)
                qn = f"{module}.{command_name or 'root'}.{name}".strip(".")
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_flag,
                        qn,
                        source.count("\n", 0, match.start()) + 1,
                        name=name,
                        aliases=aliases,
                        default=default,
                        framework=framework,
                        language=root_symbol.language,
                    )
                )
            if package_name:
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_command,
                        f"{module}.npx:{package_name}",
                        1,
                        name=package_name,
                        aliases=sorted(bins),
                        framework=framework,
                        language=root_symbol.language,
                    )
                )
        return result


def _bin_names(repo: Path) -> tuple[str | None, set[str]]:
    try:
        data = json.loads((repo / "package.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None, set()
    name = data.get("name") if isinstance(data.get("name"), str) else None
    bin_value = data.get("bin", {})
    if isinstance(bin_value, str):
        bins = {name} if name else set()
    elif isinstance(bin_value, dict):
        bins = {str(key) for key in bin_value}
    else:
        bins = set()
    return name, bins


def _framework(repo: Path, source: str) -> str:
    if "commander" in source:
        return "commander"
    if "yargs" in source:
        return "yargs"
    if "@oclif/core" in source:
        return "oclif"
    if "cac(" in source:
        return "cac"
    return "commander"


def _is_cli_file(repo: Path, path: Path, source: str) -> bool:
    _, bins = _bin_names(repo)
    package = path.relative_to(repo).as_posix()
    metadata = _read_package(repo)
    bin_info = metadata.get("bin", {})
    bin_paths = (
        {bin_info} if isinstance(bin_info, str) else set(bin_info.values()) if isinstance(bin_info, dict) else set()
    )
    detected = any(name in source for name in ("new Command", "yargs", "cac(", "@oclif/core"))
    return package in bin_paths or detected or (bool(bins) and "command(" in source)


def _read_package(repo: Path) -> dict[str, object]:
    try:
        data = json.loads((repo / "package.json").read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nearest_command(source: str, position: int, commands: dict[str, Symbol]) -> str | None:
    candidates = [
        (match.start(), match.group(2).split()[0])
        for match in _COMMAND.finditer(source[:position])
        if match.group(2).split()
    ]
    if candidates:
        return candidates[-1][1]
    return None if not commands else next(iter(commands))


@register
class _RegisteredJavaScriptCLIIndexer(JavaScriptCLIIndexer):
    pass
