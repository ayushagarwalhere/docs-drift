"""Read package and task-runner metadata as text; no task command is executed."""

import configparser
import json
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

from docs_drift.ids import make_symbol_id
from docs_drift.index import register
from docs_drift.models import Symbol, SymbolKind


class TaskIndexer:
    name = "task-runners"
    languages = frozenset({"python", "javascript", "typescript", "toml", "json", "yaml", "make", "ini"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:  # noqa: PLR0912
        symbols: list[Symbol] = []
        for path in files:
            relative = path.relative_to(repo).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if path.name == "package.json":
                try:
                    scripts = json.loads(text).get("scripts", {})
                except (json.JSONDecodeError, AttributeError):
                    scripts = {}
                if isinstance(scripts, dict):
                    symbols.extend(
                        _make(repo, relative, SymbolKind.package_script, str(key), str(value), "javascript")
                        for key, value in scripts.items()
                    )
            elif path.name in {"Makefile", "GNUmakefile"}:
                symbols.extend(
                    _make(repo, relative, SymbolKind.make_target, match.group(1), None, "make")
                    for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+):(?!=)", text)
                    if not match.group(1).startswith(".")
                )
            elif path.name == "justfile":
                symbols.extend(
                    _make(repo, relative, SymbolKind.make_target, match.group(1), None, "just")
                    for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+):(?:\s|$)", text)
                    if match.group(1) != "set"
                )
            elif path.name == "Taskfile.yml":
                for match in re.finditer(r"(?m)^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", text):
                    symbols.append(_make(repo, relative, SymbolKind.make_target, match.group(1), None, "task"))
            elif path.name == "tox.ini":
                parser = configparser.ConfigParser()
                parser.read_string(text)
                if parser.has_option("tox", "envlist"):
                    envs = re.split(r"[,\s]+", parser.get("tox", "envlist"))
                    symbols.extend(
                        _make(repo, relative, SymbolKind.make_target, item, None, "tox") for item in envs if item
                    )
            elif path.name == "pyproject.toml":
                try:
                    data = tomllib.loads(text)
                except tomllib.TOMLDecodeError:
                    continue
                project = data.get("project", {})
                scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
                symbols.extend(
                    _make(
                        repo, relative, SymbolKind.cli_command, str(name), str(target), "python", aliases=[str(target)]
                    )
                    for name, target in scripts.items()
                )
                poetry = data.get("tool", {}).get("poetry", {}).get("scripts", {})
                symbols.extend(
                    _make(
                        repo, relative, SymbolKind.cli_command, str(name), str(target), "python", aliases=[str(target)]
                    )
                    for name, target in poetry.items()
                )
                for path_name, table in data.get("tool", {}).items():
                    if path_name in {"poe", "pdm"} and isinstance(table, dict):
                        tasks = table.get("tasks", table.get("scripts", {}))
                        if isinstance(tasks, dict):
                            symbols.extend(
                                _make(repo, relative, SymbolKind.package_script, str(name), str(value), "python")
                                for name, value in tasks.items()
                            )
                hatch = data.get("tool", {}).get("hatch", {}).get("envs", {})
                if isinstance(hatch, dict):
                    for env, settings in hatch.items():
                        if isinstance(settings, dict):
                            scripts = settings.get("scripts", {})
                            if isinstance(scripts, dict):
                                symbols.extend(
                                    _make(
                                        repo, relative, SymbolKind.package_script, f"{env}:{name}", str(value), "python"
                                    )
                                    for name, value in scripts.items()
                                )
        return symbols


def _make(  # noqa: PLR0913, PLR0917
    repo: Path,
    relative: str,
    kind: SymbolKind,
    name: str,
    default: str | None,
    language: str,
    aliases: list[str] | None = None,
) -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(kind, name, relative),
        kind=kind,
        name=name,
        qualified_name=name,
        aliases=aliases or [],
        default=default,
        file=relative,
        line=1,
        language=language,
    )


@register
class _RegisteredTaskIndexer(TaskIndexer):
    pass
