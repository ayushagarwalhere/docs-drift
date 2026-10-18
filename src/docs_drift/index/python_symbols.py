"""AST-only Python symbol and environment indexer. No scanned module is imported."""

import ast
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from docs_drift.ids import make_symbol_id
from docs_drift.index import register
from docs_drift.index.python_source import read_python
from docs_drift.models import Symbol, SymbolKind

_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_SOURCE_BYTES = 1_000_000


def _literal(node: ast.AST | None, source: str) -> str | None:
    if node is None:
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return str(value)


@lru_cache(maxsize=8192)
def _module_name(repo: Path, path: Path) -> str:
    relative = path.relative_to(repo)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    while parts and not (
        repo.joinpath(*parts, "__init__.py").is_file() or repo.joinpath("src", *parts, "__init__.py").is_file()
    ):
        if len(parts) == 1:
            break
        parts.pop(0)
    return ".".join(parts)


@register
class PythonIndexer:
    name = "python-symbols"
    languages = frozenset({"python"})
    batch_files = True

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        result: list[Symbol] = []
        self.warnings = []
        for path in files:
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    self.warnings.append(f"Skipped Python source {path}: larger than 1 MB.")
                    continue
                source, tree = read_python(path)
            except (OSError, SyntaxError, ValueError) as exc:
                self.warnings.append(f"Skipped Python source {path}: {type(exc).__name__}: {exc}")
                continue
            result.extend(_index_module(repo, path, source, tree))
        return result


def _symbol(  # noqa: PLR0913
    repo: Path,
    path: Path,
    kind: SymbolKind,
    qualified: str,
    line: int,
    *,
    name: str | None = None,
    aliases: list[str] | None = None,
    default: str | None = None,
    decorators: list[str] | None = None,
    exported: bool = False,
    framework: str | None = None,
    derived: bool = False,
    language: str = "python",
    authoritative: bool = True,
    ts_type: bool = False,
) -> Symbol:
    relative = path.relative_to(repo).as_posix()
    return Symbol(
        symbol_id=make_symbol_id(kind, qualified, relative),
        kind=kind,
        name=name or qualified.rsplit(".", 1)[-1],
        qualified_name=qualified,
        aliases=aliases or [],
        default=default,
        file=relative,
        line=max(1, line),
        language=language,
        decorators=decorators or [],
        exported=exported,
        framework=framework,
        derived=derived,
        authoritative=authoritative,
        ts_type=ts_type,
    )


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, source: str) -> list[str]:
    return [ast.get_source_segment(source, decorator) or "" for decorator in node.decorator_list]


def _exports(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                if node.value is None:
                    return None
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return None
                return (
                    set(value)
                    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)
                    else None
                )
    return None


def _defined_nodes(nodes: Sequence[ast.stmt]) -> list[ast.stmt]:
    """Visit definitions through conditionals and import fallbacks without walking nested function bodies."""
    result = []
    for node in nodes:
        if isinstance(node, (ast.If, ast.Try)):
            for branch in (
                [node.body, node.orelse]
                if isinstance(node, ast.If)
                else [node.body, node.handlers[0].body if node.handlers else [], node.orelse, node.finalbody]
            ):
                result.extend(_defined_nodes(branch))
        else:
            result.append(node)
    return result


def _env_symbols(repo: Path, path: Path, module: str, source: str, tree: ast.Module) -> list[Symbol]:
    result = []
    environ_names = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "os"
        for alias in node.names
        if alias.name == "environ"
    }
    os_names = {
        alias.asname or "os"
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        key_node = node.args[0] if node.args else None
        is_environ = (
            isinstance(owner, ast.Attribute)
            and owner.attr == "environ"
            and isinstance(owner.value, ast.Name)
            and owner.value.id in os_names
        ) or (isinstance(owner, ast.Name) and owner.id in environ_names)
        is_getenv = isinstance(owner, ast.Name) and owner.id in os_names and node.func.attr == "getenv"
        if (is_environ and node.func.attr in {"get", "setdefault"}) or is_getenv:
            key = _literal(key_node, source)
            if key:
                default_node = node.args[1] if len(node.args) > 1 else None
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.env_var,
                        f"{module}.{key}".strip("."),
                        node.lineno,
                        name=key,
                        default=_literal(default_node, source),
                    )
                )
        if is_environ and node.func.attr in {"get", "setdefault"}:
            continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            base = node.value.value
            if isinstance(base, ast.Name) and base.id in os_names:
                key = _literal(node.slice, source)
                if key:
                    result.append(
                        _symbol(repo, path, SymbolKind.env_var, f"{module}.{key}".strip("."), node.lineno, name=key)
                    )
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in environ_names:
            key = _literal(node.slice, source)
            if key:
                result.append(
                    _symbol(repo, path, SymbolKind.env_var, f"{module}.{key}".strip("."), node.lineno, name=key)
                )
    return result


def _index_module(repo: Path, path: Path, source: str, tree: ast.Module) -> list[Symbol]:  # noqa: PLR0912
    module = _module_name(repo, path)
    exports = _exports(tree)
    symbols: list[Symbol] = []
    classes: dict[str, ast.ClassDef] = {}
    for node in _defined_nodes(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.function,
                    f"{module}.{node.name}".strip("."),
                    node.lineno,
                    decorators=_decorators(node, source),
                    exported=exports is None or node.name in exports,
                )
            )
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = node
            class_name = f"{module}.{node.name}".strip(".")
            bases = {ast.unparse(base).split(".")[-1] for base in node.bases}
            # Conventional config classes are supported even without a Pydantic base class;
            # this keeps plain BaseModel/dataclass settings visible without importing them.
            settings = bool(bases & {"BaseSettings"}) or node.name.endswith(("Config", "Settings", "Options"))
            symbols.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.class_,
                    class_name,
                    node.lineno,
                    decorators=_decorators(node, source),
                    exported=exports is None or node.name in exports,
                    framework="pydantic-settings"
                    if "BaseSettings" in bases
                    else "python-settings"
                    if settings
                    else None,
                )
            )
            fields: list[tuple[str, ast.AST | None, int]] = []
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.method,
                            f"{class_name}.{member.name}",
                            member.lineno,
                            decorators=_decorators(member, source),
                            exported=True,
                        )
                    )
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    fields.append((member.target.id, member.value, member.lineno))
            if settings:
                prefix = ""
                for member in node.body:
                    if isinstance(member, ast.Assign):
                        for target in member.targets:
                            if (
                                isinstance(target, ast.Name)
                                and target.id == "model_config"
                                and isinstance(member.value, ast.Call)
                            ):
                                for keyword in member.value.keywords:
                                    if keyword.arg == "env_prefix":
                                        prefix = _literal(keyword.value, source) or ""
                for field_name, value, line in fields:
                    default_node = value
                    alias: str | None = None
                    if isinstance(value, ast.Call) and ast.unparse(value.func).endswith("Field"):
                        default_node = value.args[0] if value.args else None
                        for keyword in value.keywords:
                            if keyword.arg in {"alias", "validation_alias"}:
                                alias = _literal(keyword.value, source)
                    field_qn = f"{class_name}.{field_name}"
                    symbols.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.config_key,
                            field_qn,
                            line,
                            name=field_name,
                            default=_literal(default_node, source),
                            framework="pydantic-settings",
                        )
                    )
                    env_name = alias or f"{prefix}{field_name.upper()}"
                    symbols.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.env_var,
                            f"{class_name}.{env_name}",
                            line,
                            name=env_name,
                            default=_literal(default_node, source),
                            framework="pydantic-settings",
                            derived=True,
                        )
                    )
    for node in _defined_nodes(tree.body):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and _UPPER.fullmatch(target.id):
                    symbols.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.config_key,
                            f"{module}.{target.id}".strip("."),
                            node.lineno,
                            default=_literal(value, source),
                        )
                    )
    symbols.extend(_env_symbols(repo, path, module, source, tree))
    return symbols
