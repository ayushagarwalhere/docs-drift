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

MAKEFILE_NAMES = frozenset({"makefile", "gnumakefile"})
JUSTFILE_NAMES = frozenset({"justfile", ".justfile"})
# A rule line: one or more targets, then ':' or '::' that is not an assignment (':=' / '::=').
_MAKE_RULE = re.compile(r"(?m)^([^\s:#=][^:#=\n]*?)[ \t]*::?(?!=)")
_MAKE_TARGET = re.compile(r"[A-Za-z0-9_.%/+-]+")
# Recipe header: optional quiet '@', a name, optional parameters, then ':' that is not ':='.
_JUST_RECIPE = re.compile(
    r"(?m)^@?([A-Za-z_][A-Za-z0-9_-]*)((?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s:\"'])+)*)[ \t]*:(?!=)"
)
_JUST_ALIAS = re.compile(r"(?m)^alias[ \t]+([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:=[ \t]*([A-Za-z_][A-Za-z0-9_-]*)")
_JUST_KEYWORDS = frozenset({"alias", "export", "import", "mod", "set", "unexport"})
# Sphinx's generated Makefile routes every unknown target to `sphinx-build -M <target>`.
_SPHINX_CATCH_ALL = re.compile(r"(?:SPHINXBUILD\)|sphinx-build)[^\n]*-M\s+\$@")
SPHINX_BUILDERS = (
    "changes",
    "clean",
    "coverage",
    "devhelp",
    "dirhtml",
    "doctest",
    "dummy",
    "epub",
    "gettext",
    "help",
    "html",
    "htmlhelp",
    "info",
    "json",
    "latex",
    "latexpdf",
    "latexpdfja",
    "linkcheck",
    "man",
    "pickle",
    "pseudoxml",
    "qthelp",
    "singlehtml",
    "texinfo",
    "text",
    "xml",
)
# Commands npm, pnpm and Yarn provide themselves: `pnpm install` never reads package.json "scripts", so a
# documented built-in must not be reported as a missing project script. Script-backed verbs (test, start,
# run) are deliberately absent: they fail without a matching script, so their absence is still real drift.
PACKAGE_MANAGER_BUILTINS = (
    "add",
    "audit",
    "bin",
    "cache",
    "ci",
    "config",
    "create",
    "dedupe",
    "dlx",
    "doctor",
    "env",
    "exec",
    "fetch",
    "help",
    "i",
    "import",
    "info",
    "init",
    "install",
    "licenses",
    "link",
    "list",
    "login",
    "logout",
    "ls",
    "outdated",
    "pack",
    "patch",
    "patch-commit",
    "prune",
    "publish",
    "rebuild",
    "remove",
    "rm",
    "root",
    "setup",
    "store",
    "uninstall",
    "unlink",
    "up",
    "update",
    "upgrade",
    "version",
    "view",
    "whoami",
    "why",
    "workspace",
    "workspaces",
)


class TaskIndexer:
    name = "task-runners"
    languages = frozenset({"python", "javascript", "typescript", "toml", "json", "yaml", "make", "just", "ini"})
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
                symbols.extend(_package_scripts(relative, text))
            elif path.name.casefold() in MAKEFILE_NAMES or path.suffix.casefold() == ".mk":
                symbols.extend(_make_targets(relative, text))
            elif path.name.casefold() in JUSTFILE_NAMES:
                symbols.extend(_just_recipes(relative, text))
            elif path.name == "Taskfile.yml":
                for match in re.finditer(r"(?m)^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", text):
                    symbols.append(_make(relative, SymbolKind.make_target, match.group(1), None, "task"))
            elif path.name == "tox.ini":
                parser = configparser.ConfigParser()
                parser.read_string(text)
                if parser.has_option("tox", "envlist"):
                    envs = re.split(r"[,\s]+", parser.get("tox", "envlist"))
                    symbols.extend(_make(relative, SymbolKind.make_target, item, None, "tox") for item in envs if item)
            elif path.name == "pyproject.toml":
                try:
                    data = tomllib.loads(text)
                except tomllib.TOMLDecodeError:
                    continue
                project = data.get("project", {})
                scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
                symbols.extend(
                    _make(relative, SymbolKind.cli_command, str(name), str(target), "python", aliases=[str(target)])
                    for name, target in scripts.items()
                )
                poetry = data.get("tool", {}).get("poetry", {}).get("scripts", {})
                symbols.extend(
                    _make(relative, SymbolKind.cli_command, str(name), str(target), "python", aliases=[str(target)])
                    for name, target in poetry.items()
                )
                for path_name, table in data.get("tool", {}).items():
                    if path_name in {"poe", "pdm"} and isinstance(table, dict):
                        tasks = table.get("tasks", table.get("scripts", {}))
                        if isinstance(tasks, dict):
                            symbols.extend(
                                _make(relative, SymbolKind.package_script, str(name), str(value), "python")
                                for name, value in tasks.items()
                            )
                hatch = data.get("tool", {}).get("hatch", {}).get("envs", {})
                if isinstance(hatch, dict):
                    for env, settings in hatch.items():
                        if isinstance(settings, dict):
                            scripts = settings.get("scripts", {})
                            if isinstance(scripts, dict):
                                symbols.extend(
                                    _make(relative, SymbolKind.package_script, f"{env}:{name}", str(value), "python")
                                    for name, value in scripts.items()
                                )
        return symbols


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _recipe(text: str, rule_end: int) -> str:
    """The rest of the rule line plus its tab-indented recipe lines."""
    lines = text[rule_end:].splitlines()
    body = lines[:1]
    for item in lines[1:]:
        if not item.startswith("\t"):
            break
        body.append(item)
    return "\n".join(body)


def _package_scripts(relative: str, text: str) -> list[Symbol]:
    """Declared scripts, plus the package manager's own commands as non-authoritative derived names."""
    try:
        scripts = json.loads(text).get("scripts", {})
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(scripts, dict):
        scripts = {}
    symbols = [
        _make(relative, SymbolKind.package_script, str(key), str(value), "javascript") for key, value in scripts.items()
    ]
    symbols.extend(
        _make(
            relative,
            SymbolKind.package_script,
            name,
            None,
            "javascript",
            qualified=f"package-manager:{name}",
            framework="package-manager",
            derived=True,
        )
        for name in PACKAGE_MANAGER_BUILTINS
        if name not in scripts
    )
    return symbols


def _make_targets(relative: str, text: str) -> list[Symbol]:
    """Explicit targets, plus the builders a Sphinx-style `%:` catch-all forwards to sphinx-build."""
    symbols: list[Symbol] = []
    seen: set[str] = set()
    for match in _MAKE_RULE.finditer(text):
        line = _line(text, match.start())
        for target in match.group(1).split():
            if not _MAKE_TARGET.fullmatch(target) or target.startswith(".") or target in seen:
                continue
            seen.add(target)
            if "%" not in target:
                symbols.append(_make(relative, SymbolKind.make_target, target, None, "make", line=line))
            # Pattern rules are not invocable names; only a known catch-all forwarder yields concrete targets.
            elif target == "%" and _SPHINX_CATCH_ALL.search(_recipe(text, match.end())):
                builders = [builder for builder in SPHINX_BUILDERS if builder not in seen]
                seen.update(builders)
                symbols.extend(
                    _make(
                        relative,
                        SymbolKind.make_target,
                        builder,
                        None,
                        "make",
                        qualified=f"%:{builder}",
                        line=line,
                        framework="sphinx",
                        derived=True,
                    )
                    for builder in builders
                )
    return symbols


def _just_recipes(relative: str, text: str) -> list[Symbol]:
    """Recipe names (quiet '@' recipes and recipes with parameters included) and their aliases."""
    recipes: dict[str, Symbol] = {}
    for match in _JUST_RECIPE.finditer(text):
        name = match.group(1)
        if name in _JUST_KEYWORDS or name in recipes:
            continue
        parameters = [part.split("=", 1)[0].lstrip("+*$") for part in match.group(2).split()]
        recipes[name] = _make(
            relative,
            SymbolKind.make_target,
            name,
            " ".join(parameters) or None,
            "just",
            line=_line(text, match.start()),
            framework="just",
        )
    for match in _JUST_ALIAS.finditer(text):
        alias, target = match.groups()
        if target in recipes and alias not in recipes[target].aliases:
            recipes[target].aliases.append(alias)
    return list(recipes.values())


def _make(  # noqa: PLR0913
    relative: str,
    kind: SymbolKind,
    name: str,
    default: str | None,
    language: str,
    *,
    aliases: list[str] | None = None,
    qualified: str | None = None,
    line: int = 1,
    framework: str | None = None,
    derived: bool = False,
) -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(kind, qualified or name, relative),
        kind=kind,
        name=name,
        qualified_name=qualified or name,
        aliases=aliases or [],
        default=default,
        file=relative,
        line=line,
        language=language,
        framework=framework,
        derived=derived,
        authoritative=not derived,
    )


@register
class _RegisteredTaskIndexer(TaskIndexer):
    pass
