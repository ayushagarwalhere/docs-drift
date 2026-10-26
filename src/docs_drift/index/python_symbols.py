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
# Keyword arguments that name an environment variable (click ``envvar=``, custom argparse actions ``env=``).
_ENV_KEYWORDS = frozenset({"env", "envvar", "env_var", "env_name", "environ", "environment_variable"})
# Module-level dicts whose string keys are configuration keys (``default_config = ImmutableDict({...})``).
_PROXY_READERS = frozenset({"getproxies", "getproxies_environment", "proxy_bypass", "proxy_bypass_environment"})
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_CONFIG_DICT = re.compile(r"config|defaults|settings", re.IGNORECASE)
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
    """Dotted import name: the file's stem plus every enclosing directory that is a package."""
    if path.name == "__init__.py":
        if path.parent == repo:
            return ""
        parts, directory = [path.parent.name], path.parent.parent
    else:
        parts, directory = [path.stem], path.parent
    while directory != repo and directory.is_relative_to(repo) and (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
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
    result.extend(_env_keywords(repo, path, module, tree))
    result.extend(_proxy_env(repo, path, module, tree))
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


def _env_keywords(repo: Path, path: Path, module: str, tree: ast.Module) -> list[Symbol]:
    """Variables named by declarations such as ``click.option(envvar="X")`` or ``add_argument(env="X")``."""
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in _ENV_KEYWORDS:
                continue
            values = keyword.value.elts if isinstance(keyword.value, (ast.List, ast.Tuple)) else [keyword.value]
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and _UPPER.fullmatch(value.value):
                    result.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.env_var,
                            f"{module}.{value.value}".strip("."),
                            node.lineno,
                            name=value.value,
                        )
                    )
    return result


def _proxy_env(repo: Path, path: Path, module: str, tree: ast.Module) -> list[Symbol]:
    """``urllib.request.getproxies()``/``proxy_bypass()`` read the standard ``*_PROXY`` variables."""
    call = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).rsplit(".", 1)[-1] in _PROXY_READERS
        ),
        None,
    )
    if call is None:
        return []
    return [
        _symbol(
            repo,
            path,
            SymbolKind.env_var,
            f"{module}.{name}".strip("."),
            call.lineno,
            name=name,
            aliases=[name.lower()],
            framework="urllib",
            derived=True,
        )
        for name in _PROXY_VARIABLES
    ]


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        items = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
        names.extend(item.id for item in items if isinstance(item, ast.Name))
    return names


def _parameters(  # noqa: PLR0913 - static declaration context.
    repo: Path, path: Path, owner: str, node: ast.FunctionDef | ast.AsyncFunctionDef, *, method: bool, exported: bool
) -> list[Symbol]:
    """Named parameters of public callables, so documented keyword arguments resolve."""
    if node.name.startswith("_") and node.name != "__init__":
        return []
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    if method and arguments and arguments[0].arg in {"self", "cls"}:
        arguments = arguments[1:]
    qualified = owner if node.name == "__init__" else f"{owner}.{node.name}"
    return [
        _symbol(
            repo,
            path,
            SymbolKind.parameter,
            f"{qualified}.{argument.arg}".strip("."),
            argument.lineno,
            name=argument.arg,
            exported=exported,
        )
        for argument in arguments
        if not argument.arg.startswith("_")
    ]


def _class_attributes(repo: Path, path: Path, class_name: str, node: ast.ClassDef, source: str) -> list[Symbol]:
    """Class-body assignments (attributes, dataclass fields, enum members) and ``self.x`` in methods."""
    seen: dict[str, int] = {}
    defaults: dict[str, str | None] = {}
    for member in node.body:
        if isinstance(member, (ast.Assign, ast.AnnAssign)):
            for name in _target_names(member):
                seen.setdefault(name, member.lineno)
                defaults.setdefault(name, _literal(member.value, source))
    for member in node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in ast.walk(member):
            if isinstance(item, ast.Assign):
                targets = list(item.targets)
            elif isinstance(item, (ast.AnnAssign, ast.AugAssign)):
                targets = [item.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    seen.setdefault(target.attr, item.lineno)
    return [
        _symbol(
            repo, path, SymbolKind.attribute, f"{class_name}.{name}", line, default=defaults.get(name), exported=True
        )
        for name, line in seen.items()
    ]


def _config_dict_keys(repo: Path, path: Path, module: str, node: ast.Assign | ast.AnnAssign) -> list[Symbol]:
    """String keys of ``default_config = ImmutableDict({...})``-style module dicts are configuration keys."""
    names = [name for name in _target_names(node) if _CONFIG_DICT.search(name)]
    value = node.value
    if not names or value is None:
        return []
    if isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
        value = value.args[0]
    if not isinstance(value, ast.Dict):
        return []
    result = []
    for key, item in zip(value.keys, value.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value:
            symbol = _symbol(
                repo,
                path,
                SymbolKind.config_key,
                f"{module}.{names[0]}.{key.value}".strip("."),
                key.lineno,
                name=key.value,
                default=_literal(item, ""),
            )
            symbol.setting_group = names[0]
            result.append(symbol)
    return result


def _reexports(repo: Path, path: Path, module: str, node: ast.ImportFrom) -> list[Symbol]:
    """``from .hookspecs import hookimpl`` in a package makes ``package.hookimpl`` importable.

    Only project-internal imports count: relative imports, or absolute imports of this package.
    Re-exported third-party names are not project API, so they stay unresolved.
    """
    root = module.split(".", 1)[0]
    internal = node.level > 0 or (node.module or "").split(".", 1)[0] == root
    if not internal or not module:
        return []
    return [
        _symbol(repo, path, SymbolKind.variable, f"{module}.{alias.asname or alias.name}", node.lineno, exported=True)
        for alias in node.names
        if alias.name != "*"
    ]


def _index_module(repo: Path, path: Path, source: str, tree: ast.Module) -> list[Symbol]:  # noqa: PLR0912, PLR0915
    module = _module_name(repo, path)
    exports = _exports(tree)
    symbols: list[Symbol] = []
    classes: dict[str, ast.ClassDef] = {}
    if module:
        # ``from package import submodule`` is valid for every module file.
        symbols.append(_symbol(repo, path, SymbolKind.module, module, 1, exported=True))
    for node in _defined_nodes(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exported = exports is None or node.name in exports
            symbols.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.function,
                    f"{module}.{node.name}".strip("."),
                    node.lineno,
                    decorators=_decorators(node, source),
                    exported=exported,
                )
            )
            symbols.extend(_parameters(repo, path, module, node, method=False, exported=exported))
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
            symbols.extend(_class_attributes(repo, path, class_name, node, source))
            for member in node.body:
                if isinstance(member, (ast.Assign, ast.AnnAssign)):
                    symbols.extend(_config_dict_keys(repo, path, class_name, member))
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
                    symbols.extend(_parameters(repo, path, class_name, member, method=True, exported=True))
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
        if isinstance(node, ast.ImportFrom):
            symbols.extend(_reexports(repo, path, module, node))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in dict.fromkeys(_target_names(node)):
                symbols.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.variable,
                        f"{module}.{name}".strip("."),
                        node.lineno,
                        exported=exports is None or name in exports,
                    )
                )
            symbols.extend(_config_dict_keys(repo, path, module, node))
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
