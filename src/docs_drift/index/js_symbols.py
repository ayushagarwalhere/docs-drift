"""Tree-sitter-backed JavaScript/TypeScript indexer with conservative text extraction."""

import re
from collections.abc import Sequence
from pathlib import Path

from tree_sitter import Node, Parser

from docs_drift.index import register
from docs_drift.index.python_symbols import _symbol
from docs_drift.index.ts_parser import parser_for
from docs_drift.models import Symbol, SymbolKind

_ENV = re.compile(
    r"(?:process\.env\.(\w+)|process\.env\[['\"](\w+)['\"]\]|import\.meta\.env\.(\w+)|Deno\.env\.get\(['\"](\w+)['\"]\))"
)
_DEFAULT_ENV = re.compile(r"(?:process\.env\.(\w+)|process\.env\[['\"](\w+)['\"]\])\s*(?:\?\?|\|\|)\s*(['\"])(.*?)\3")
_FUNC = re.compile(r"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)")
_ARROW = re.compile(r"(?m)^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")
_CLASS = re.compile(r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")
_TYPE = re.compile(r"(?m)^\s*export\s+(?:interface|type)\s+(\w+)")
_EXPORT_LIST = re.compile(r"(?m)^\s*export\s*(?:type\s*)?\{([^}]+)\}(\s*from\s*['\"][^'\"]+['\"])?")
_EXPORT_VALUE = re.compile(r"(?m)^\s*export\s+(?:const|let|var)\s+(\w+)")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_FUNCTION_VALUES = frozenset(
    {"function", "function_expression", "arrow_function", "generator_function", "async_function"}
)
_IMPORT_ENV = re.compile(r"import\s+\{\s*([^}]+)\s*\}\s+from\s+['\"]([^'\"]+)['\"]")
_SECRETISH = re.compile(r"(?=[A-Za-z0-9_+/=-]{21,}$)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9_+/=-]+$")
MAX_SOURCE_BYTES = 1_000_000
MINIFIED_LINE_CHARS = 1000
ENV_KEY_GROUP_COUNT = 2
EXPORT_ALIAS_PARTS = 2


def _module(repo: Path, path: Path) -> str:
    package_root = repo
    for parent in path.parents:
        if (parent / "package.json").is_file():
            package_root = parent
            break
    rel = path.relative_to(package_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] in {"index", "route", "page"}:
        parts.pop()
    return ".".join(parts)


def _safe_default(value: str) -> str:
    # Example env files can accidentally contain credentials; never expose long random-looking samples.
    return "[REDACTED]" if _SECRETISH.fullmatch(value) else value


class JavaScriptIndexer:
    name = "javascript-typescript"
    languages = frozenset({"javascript", "typescript", "dotenv"})
    batch_files = True

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def applies_to(self, repo: Path) -> bool:
        return True

    def _parser(self, repo: Path, language: str) -> Parser:
        del repo  # Parser instances and native grammar cache are shared across indexed files.
        return parser_for(language)

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        self.warnings = []
        result: list[Symbol] = []
        for path in files:
            relative = path.relative_to(repo).as_posix()
            if path.name in {".env.example", ".env.sample", ".env.template"}:
                result.extend(_dotenv(repo, path))
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    self.warnings.append(f"Skipped source {relative}: larger than 1 MB.")
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                minified = any(len(line) > MINIFIED_LINE_CHARS for line in source.splitlines())
                if path.name.endswith(".min.js") or minified:
                    self.warnings.append(f"Skipped minified source {relative}.")
                    continue
                language = "typescript" if path.suffix.casefold() in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
                parser = self._parser(repo, language)
                tree = parser.parse(source.encode("utf-8"))
                if tree.root_node.has_error:
                    self.warnings.append(
                        f"Tree-sitter found syntax errors in {relative}; extracting recognizable declarations only."
                    )
                symbols = _source_symbols(repo, path, source)
                symbols.extend(_member_symbols(repo, path, tree.root_node, {item.qualified_name for item in symbols}))
                result.extend(symbols)
            except Exception as exc:
                # No grammar/cache/network failure can abort unrelated file or Python indexing.
                self.warnings.append(f"Skipped parse setup for {relative}: {type(exc).__name__}: {exc}")
        return result


def _source_symbols(repo: Path, path: Path, source: str) -> list[Symbol]:
    module = _module(repo, path)
    result: list[Symbol] = []
    exported_names = set(
        re.findall(
            r"(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+(\w+)",
            source,
        )
    )
    for match in (*_FUNC.finditer(source), *_ARROW.finditer(source)):
        name = match.group(1)
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.function,
                f"{module}.{name}".strip("."),
                source.count("\n", 0, match.start()) + 1,
                exported=name in exported_names,
            )
        )
    for match in _CLASS.finditer(source):
        name = match.group(1)
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.class_,
                f"{module}.{name}".strip("."),
                source.count("\n", 0, match.start()) + 1,
                exported=name in exported_names,
            )
        )
    for match in _TYPE.finditer(source):
        name = match.group(1)
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.class_,
                f"{module}.{name}".strip("."),
                source.count("\n", 0, match.start()) + 1,
                exported=True,
                framework="typescript",
                ts_type=True,
            )
        )
    result.extend(_export_symbols(repo, path, source, {item.name for item in result}))
    for match in _DEFAULT_ENV.finditer(source):
        name = next(value for value in match.groups()[:ENV_KEY_GROUP_COUNT] if value)
        default = _safe_default(match.group(4))
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.env_var,
                f"{module}.{name}",
                source.count("\n", 0, match.start()) + 1,
                name=name,
                default=default,
            )
        )
    for match in _ENV.finditer(source):
        name = next(value for value in match.groups() if value)
        if any(
            item.kind == SymbolKind.env_var
            and item.name == name
            and item.line == source.count("\n", 0, match.start()) + 1
            for item in result
        ):
            continue
        result.append(
            _symbol(
                repo, path, SymbolKind.env_var, f"{module}.{name}", source.count("\n", 0, match.start()) + 1, name=name
            )
        )
    for match in _IMPORT_ENV.finditer(source):
        for binding in match.group(1).split(","):
            value = binding.strip().split(" as ")[-1]
            if value == "process" or not value:
                continue
    return result


def _export_symbols(repo: Path, path: Path, source: str, declared: set[str]) -> list[Symbol]:
    """Names made public by export lists, re-exports and exported value bindings."""
    module = _module(repo, path)
    result: list[Symbol] = []
    for match in _EXPORT_LIST.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        for part in match.group(1).split(","):
            names = re.split(r"\s+as\s+", re.sub(r"^\s*type\s+", "", part.strip()))
            if len(names) == EXPORT_ALIAS_PARTS:
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.function,
                        f"{module}.{names[0]}".strip("."),
                        line,
                        name=names[1],
                        aliases=[names[0]],
                        exported=True,
                    )
                )
            elif len(names) == 1 and _IDENTIFIER.fullmatch(names[0]) and names[0] not in declared:
                # `export { name } from './impl'` (or of a local value binding) is public API of this module.
                declared.add(names[0])
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.function,
                        f"{module}.{names[0]}".strip("."),
                        line,
                        exported=True,
                        derived=bool(match.group(2)),
                    )
                )
    for match in _EXPORT_VALUE.finditer(source):
        name = match.group(1)
        if name in declared:
            continue
        declared.add(name)
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.function,
                f"{module}.{name}".strip("."),
                source.count("\n", 0, match.start()) + 1,
                exported=True,
            )
        )
    return result


def _member_symbols(repo: Path, path: Path, root: Node, known: set[str]) -> list[Symbol]:
    """Class methods and function-valued object-literal properties (`name: function () {}`, `name() {}`).

    Plugin APIs are often plain objects (`return {getValue: function () {...}}`), so documentation names these
    members as often as top-level functions.
    """
    module = _module(repo, path)
    result: list[Symbol] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        name = None
        if node.type == "method_definition":
            name = _key_text(node.child_by_field_name("name"))
        elif node.type == "pair":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                name = _key_text(node.child_by_field_name("key"))
        elif node.type == "assignment_expression":
            # `api.setValue = function () {}` and `Widget.prototype.show = () => {}`
            left, value = node.child_by_field_name("left"), node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "member_expression"
                and value is not None
                and value.type in _FUNCTION_VALUES
            ):
                name = _key_text(left.child_by_field_name("property"))
        if not name or name == "constructor":
            continue
        qualified = f"{module}.{_owner(node)}.{name}".strip(".")
        if qualified in known:
            continue
        known.add(qualified)
        result.append(_symbol(repo, path, SymbolKind.method, qualified, node.start_point[0] + 1, name=name))
    return result


def _key_text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    text = node.text.decode("utf-8", errors="replace")
    if node.type == "string":
        text = text[1:-1]
    elif node.type not in {"property_identifier", "private_property_identifier", "identifier"}:
        return None
    text = text.lstrip("#")
    return text if _IDENTIFIER.fullmatch(text) else None


def _owner(node: Node) -> str:
    """Nearest named container: class name, variable or property holding the object, else 'object'."""
    current = node.parent
    while current is not None:
        if current.type in {"class_declaration", "class", "abstract_class_declaration"}:
            name = current.child_by_field_name("name")
            return _key_text(name) or "class" if name is not None else "class"
        if current.type == "object":
            holder = current.parent
            if holder is not None and holder.type == "variable_declarator":
                return _key_text(holder.child_by_field_name("name")) or "object"
            if holder is not None and holder.type == "pair":
                return _key_text(holder.child_by_field_name("key")) or "object"
            return "object"
        if current.type == "member_expression" and node.type == "assignment_expression":
            break
        current = current.parent
    if node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        target = left.child_by_field_name("object") if left is not None else None
        text = target.text.decode("utf-8", errors="replace") if target is not None and target.text else ""
        text = text.removesuffix(".prototype")
        return text if re.fullmatch(r"[\w$.]+", text) else "object"
    return "object"


def _dotenv(repo: Path, path: Path) -> list[Symbol]:
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.env_var,
                f"dotenv-example.{key}",
                line_no,
                name=key,
                default=_safe_default(value.strip().strip("\"'")),
                framework="dotenv-example",
                derived=True,
                authoritative=False,
            )
        )
    return result


@register
class _RegisteredJavaScriptIndexer(JavaScriptIndexer):
    pass
