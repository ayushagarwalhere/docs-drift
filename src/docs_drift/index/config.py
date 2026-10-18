"""Index shipped config examples, schemas, container defaults, and CI job names."""

import json
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docs_drift.ids import make_symbol_id
from docs_drift.index import register
from docs_drift.models import Symbol, SymbolKind


class ConfigIndexer:
    name = "config-surfaces"
    languages = frozenset({"yaml", "toml", "json", "dockerfile"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        result = []
        for path in files:
            relative = path.relative_to(repo).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower = path.name.casefold()
            if path.name == "Dockerfile" or lower.endswith(".dockerfile"):
                result.extend(_docker(repo, path, text))
            elif path.suffix.casefold() in {".json", ".toml", ".yaml", ".yml"}:
                if ".github/workflows/" in relative:
                    result.extend(_workflow(repo, path, text))
                if "schema" in lower:
                    result.extend(_schema(repo, path, text))
                elif any(
                    token in lower for token in ("example", "sample", "default", "values.")
                ) or relative.startswith("charts/"):
                    result.extend(_example(repo, path, text))
        return result


def _symbol(  # noqa: PLR0913, PLR0917
    repo: Path,
    path: Path,
    kind: SymbolKind,
    name: str,
    line: int,
    default: str | None,
    framework: str,
    *,
    authoritative: bool = False,
    derived: bool = False,
) -> Symbol:
    relative = path.relative_to(repo).as_posix()
    return Symbol(
        symbol_id=make_symbol_id(kind, name, relative),
        kind=kind,
        name=name,
        qualified_name=name,
        default=default,
        file=relative,
        line=max(1, line),
        language=path.suffix.lstrip(".") or "dockerfile",
        framework=framework,
        setting_group=name.casefold().replace("_", "."),
        authoritative=authoritative,
        derived=derived,
    )


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            result.extend(_flatten(child, dotted)) if isinstance(child, dict) else result.append((dotted, child))
    return result


def _simple_yaml(text: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    rows: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = re.match(r"^(\s*)([A-Za-z_][\w-]*):(?:\s*(.*))?$", line)
        if not match:
            continue
        indent, key, value = len(match.group(1)), match.group(2), (match.group(3) or "").strip().strip("\"'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        dotted = ".".join([part for _, part in stack] + [key])
        rows.append((dotted, value))
        if not value:
            stack.append((indent, key))
    return rows, {name: value for name, value in rows}


def _example(repo: Path, path: Path, text: str) -> list[Symbol]:
    try:
        if path.suffix.casefold() == ".json":
            values = _flatten(json.loads(text))
        elif path.suffix.casefold() == ".toml":
            values = _flatten(tomllib.loads(text))
        else:
            values = _simple_yaml(text)[0]
    except (ValueError, tomllib.TOMLDecodeError):
        return []
    result = []
    for name, value in values:
        line = next(
            (
                index
                for index, source in enumerate(text.splitlines(), 1)
                if re.search(rf"\b{re.escape(name.rsplit('.', 1)[-1])}\s*[:=]", source)
            ),
            1,
        )
        result.append(_symbol(repo, path, SymbolKind.config_key, name, line, str(value), "config-example"))
    return result


def _schema(repo: Path, path: Path, text: str) -> list[Symbol]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    result = []

    def visit(node: Any, prefix: str = "") -> None:
        if not isinstance(node, dict):
            return
        for key, item in node.get("properties", {}).items():
            name = f"{prefix}.{key}" if prefix else key
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.config_key,
                    name,
                    1,
                    str(item["default"]) if "default" in item else None,
                    "json-schema",
                )
            )
            visit(item, name)

    visit(data)
    return result


def _docker(repo: Path, path: Path, text: str) -> list[Symbol]:
    result = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if match := re.match(r"\s*ENV\s+([A-Za-z_][\w]*)=(.*)", line, re.I):
            result.append(
                _symbol(repo, path, SymbolKind.env_var, match.group(1), line_no, match.group(2), "dockerfile")
            )
        elif match := re.match(r"\s*ARG\s+([A-Za-z_][\w]*)(?:=(.*))?", line, re.I):
            result.append(
                _symbol(repo, path, SymbolKind.config_key, match.group(1), line_no, match.group(2), "docker-arg")
            )
        elif match := re.match(r"\s*EXPOSE\s+(\d+)", line, re.I):
            result.append(_symbol(repo, path, SymbolKind.config_key, "port", line_no, match.group(1), "docker-expose"))
        elif match := re.match(r"\s*(?:CMD|ENTRYPOINT)\s+(.+)", line, re.I):
            result.append(_symbol(repo, path, SymbolKind.file, "command", line_no, match.group(1), "docker-command"))
    return result


def _workflow(repo: Path, path: Path, text: str) -> list[Symbol]:
    result = []
    in_jobs = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
        elif in_jobs and (match := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)):
            result.append(_symbol(repo, path, SymbolKind.ci_job, match.group(1), line_no, None, "github-actions"))
    return result


@register
class _RegisteredConfigIndexer(ConfigIndexer):
    pass
