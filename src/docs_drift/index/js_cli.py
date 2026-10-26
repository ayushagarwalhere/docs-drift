"""Static CLI declarations for Commander, yargs, oclif, cac, meow and arg."""

import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

from tree_sitter import Node

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
_FRAMEWORK_PACKAGES = {
    "commander": "commander",
    "yargs": "yargs",
    "yargs/yargs": "yargs",
    "@oclif/core": "oclif",
    "cac": "cac",
    "meow": "meow",
    "arg": "arg",
}
_IMPORT = re.compile(
    r"""\bimport\s+(?:(?P<default>[\w$]+)\s*(?:,\s*\{[^}]*\})?|\{[^}]*\}|\*\s+as\s+[\w$]+)\s+from\s+"""
    r"""['"](?P<module>[^'"]+)['"]"""
    r"""|(?:const|let|var)\s+(?P<required>[\w$]+)\s*=\s*require\(\s*['"](?P<rmodule>[^'"]+)['"]\s*\)"""
)
# Positional dispatch such as `cli.input[0] === 'release-draft'` or `cli.input.at(0) === "x"`.
_INPUT_DISPATCH = re.compile(r"\.input(?:\[0\]|\.at\(\s*0\s*\))\s*===?\s*(['\"])([A-Za-z][\w-]*)\1")
_HELP_COMMAND = re.compile(r"^\s+([a-z][\w-]*)(?:\s{2,}\S.*)?$")
_STRING_TYPES = frozenset({"string", "template_string"})
# (flag, aliases, source pattern or None, whether the pattern must be present) per framework.
_IMPLICIT_FLAGS: dict[str, tuple[tuple[str, tuple[str, ...], str | None, bool], ...]] = {
    "meow": (("--help", (), None, True), ("--version", (), None, True)),
    "commander": (
        ("--help", ("-h",), r"\.helpOption\(\s*false\s*\)", False),
        ("--version", ("-V",), r"\.version\(", True),
    ),
    "yargs": (
        ("--help", (), r"\.help\(\s*false\s*\)", False),
        ("--version", (), r"\.version\(\s*false\s*\)", False),
    ),
    "cac": (("--help", ("-h",), r"\.help\(", True), ("--version", ("-v",), r"\.version\(", True)),
    "oclif": (("--help", (), None, True),),
}


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
                tree = parser.parse(source.encode())
                if tree.root_node.has_error:
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
            context = _SpecContext(repo, path, module, root_symbol, tree.root_node, source)
            context.known([item for item in result if item.file == root_symbol.file])
            result.extend(_spec_symbols(context, framework))
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


class _SpecContext:
    """Per-file state shared by the object-spec extractors (meow, arg) and implicit-flag rules."""

    def __init__(  # noqa: PLR0913, PLR0917
        self, repo: Path, path: Path, module: str, root: Symbol, tree: Node, source: str
    ) -> None:
        self.repo, self.path, self.module, self.root, self.tree, self.source = repo, path, module, root, tree, source
        self.imports = _imports(source)
        self.seen: set[str] = set()
        self.commands: set[str] = set()

    def known(self, symbols: Sequence[Symbol]) -> None:
        """Skip names the regex extractors already emitted, so symbol ids stay unique."""
        for item in symbols:
            if item.kind == SymbolKind.cli_flag:
                self.seen.update((item.name, *item.aliases))
            elif item.kind == SymbolKind.cli_command:
                self.commands.add(item.name)

    def flag(  # noqa: PLR0913
        self,
        name: str,
        line: int,
        *,
        aliases: Sequence[str] = (),
        default: str | None = None,
        framework: str,
        derived: bool = False,
    ) -> Symbol | None:
        if name in self.seen:
            return None
        self.seen.add(name)
        return _symbol(
            self.repo,
            self.path,
            SymbolKind.cli_flag,
            f"{self.module}.root.{name}".strip("."),
            line,
            name=name,
            aliases=[alias for alias in dict.fromkeys(aliases) if alias != name],
            default=default,
            framework=framework,
            derived=derived,
            language=self.root.language,
        )

    def symbol(self, kind: SymbolKind, qualified: str, name: str, line: int, framework: str) -> Symbol:
        symbol = _symbol(
            self.repo,
            self.path,
            kind,
            f"{self.module}.{qualified}".strip("."),
            line,
            name=name,
            framework=framework,
            derived=True,
            authoritative=False,
            language=self.root.language,
        )
        if kind == SymbolKind.cli_command:
            symbol.parent_id = self.root.symbol_id
        return symbol


def _spec_symbols(context: _SpecContext, framework: str) -> list[Symbol]:
    result: list[Symbol] = []
    meow_names = context.imports.get("meow", set())
    arg_names = context.imports.get("arg", set())
    for call in _calls(context.tree, meow_names | arg_names):
        callee = _text(call.child_by_field_name("function"))
        result.extend(_meow_symbols(context, call) if callee in meow_names else _arg_symbols(context, call))
    result.extend(_implicit_flags(context, framework))
    dispatch = [
        (match.group(2), context.source.count("\n", 0, match.start()) + 1)
        for match in _INPUT_DISPATCH.finditer(context.source)
    ]
    if meow_names:
        dispatch.extend(_help_commands(context))
        bins = {context.root.name, *context.root.aliases}
        arguments = {name: line for name, line in reversed(_usage_arguments(context, bins))}
        result.extend(
            context.symbol(SymbolKind.cli_argument, f"root.{name}", name, line, framework)
            for name, line in arguments.items()
        )
    for name, line in dispatch:
        if name in context.commands:
            continue
        context.commands.add(name)
        result.append(context.symbol(SymbolKind.cli_command, name, name, line, framework))
    return result


def _meow_symbols(context: _SpecContext, call: Node) -> list[Symbol]:
    """meow(help, {flags}) or meow({flags}); camelCase keys become --kebab flags, booleans gain --no-<flag>."""
    result: list[Symbol] = []
    options = next(
        (
            resolved
            for item in _arguments(call)
            if (resolved := _resolve(context.tree, item)) is not None and resolved.type == "object"
        ),
        None,
    )
    flags = _resolve(context.tree, _property(options, "flags"))
    if flags is None or flags.type != "object":
        return result
    loads_config = _loads_config(context)
    for key, value, line in _pairs(flags):
        spec = _resolve(context.tree, value)
        kebab = _kebab(key)
        aliases: list[str] = []
        flag_type = None
        default = None
        if spec is not None and spec.type == "object":
            flag_type = _literal(_property(spec, "type"))
            default = _literal(_property(spec, "default"))
            short = _literal(_property(spec, "shortFlag"))
            if short:
                aliases.append(f"-{short}")
            for alias in (*_strings(_property(spec, "alias")), *_strings(_property(spec, "aliases"))):
                aliases.append(f"-{alias}" if len(alias) == 1 else f"--{_kebab(alias)}")
        elif spec is not None:
            flag_type = _text(spec).casefold()
        if flag_type == "boolean":
            aliases.append(f"--no-{kebab}")
        if kebab != key:
            aliases.append(f"--{key}")
        symbol = context.flag(f"--{kebab}", line, aliases=aliases, default=default, framework="meow")
        if symbol is not None:
            result.append(symbol)
        if loads_config:
            # Tools such as np merge a config file over the same option names.
            result.append(context.symbol(SymbolKind.config_key, f"config.{key}", key, line, "meow-config"))
    return result


def _arg_symbols(context: _SpecContext, call: Node) -> list[Symbol]:
    """arg({'--name': Type, '-n': '--name'}): string values are aliases of another declared flag."""
    spec = next((_resolve(context.tree, item) for item in _arguments(call)), None)
    if spec is None or spec.type != "object":
        return []
    declared: dict[str, tuple[int, list[str]]] = {}
    aliases: list[tuple[str, str]] = []
    for key, value, line in _pairs(spec):
        if not key.startswith("-"):
            continue
        target = _literal(value) if value is not None and value.type in _STRING_TYPES else None
        if target and target.startswith("-"):
            aliases.append((key, target))
        else:
            declared[key] = (line, [])
    for alias, target in aliases:
        if target in declared:
            declared[target][1].append(alias)
    result = []
    for name, (line, names) in declared.items():
        symbol = context.flag(name, line, aliases=names, framework="arg")
        if symbol is not None:
            result.append(symbol)
    return result


def _implicit_flags(context: _SpecContext, framework: str) -> list[Symbol]:
    """Help and version flags that the framework adds without an explicit declaration."""
    source = context.source
    markers = ("new Command", "yargs(", "cac(", "@oclif/core")
    if not context.imports and not any(marker in source for marker in markers):
        # The framework was only guessed; implicit flags need evidence that a parser actually runs here.
        return []
    if "meow" in context.imports:
        framework = "meow"
    result = []
    for name, aliases, pattern, wanted in _IMPLICIT_FLAGS.get(framework, ()):
        if pattern is not None and bool(re.search(pattern, source)) != wanted:
            continue
        symbol = context.flag(name, 1, aliases=aliases, framework=framework, derived=True)
        if symbol is not None:
            result.append(symbol)
    return result


def _help_texts(context: _SpecContext) -> list[list[tuple[str, int]]]:
    """(text, line) pairs of each meow help string literal."""
    texts = []
    for call in _calls(context.tree, context.imports.get("meow", set())):
        help_text = next(
            (
                resolved
                for item in _arguments(call)
                if (resolved := _resolve(context.tree, item)) is not None and resolved.type in _STRING_TYPES
            ),
            None,
        )
        if help_text is not None:
            start = help_text.start_point[0] + 1
            texts.append([(line, start + index) for index, line in enumerate(_text(help_text).splitlines())])
    return texts


def _help_commands(context: _SpecContext) -> list[tuple[str, int]]:
    """(name, line) for commands listed under a 'Commands' heading in meow's help text."""
    found: list[tuple[str, int]] = []
    for lines in _help_texts(context):
        in_commands = False
        listed = 0
        for line, line_no in lines:
            stripped = line.strip()
            if stripped.casefold() == "commands":
                in_commands = True
                continue
            if not in_commands:
                continue
            if not stripped:
                if listed:
                    break
                continue
            match = _HELP_COMMAND.match(line)
            if match is None:
                break
            listed += 1
            found.append((match.group(1), line_no))
    return found


def _usage_arguments(context: _SpecContext, bins: set[str]) -> list[tuple[str, int]]:
    """Root positionals from usage lines such as `$ np <version>` (values, not subcommands)."""
    found: list[tuple[str, int]] = []
    for lines in _help_texts(context):
        for line, line_no in lines:
            usage = re.match(r"\s*\$\s+([\w@./-]+)\s+(.*)$", line)
            if usage is None or usage.group(1) not in bins:
                continue
            found.extend((name, line_no) for name in re.findall(r"[<\[]([\w-]+)(?:\.\.\.)?[>\]]", usage.group(2)))
    return found


def _loads_config(context: _SpecContext) -> bool:
    metadata = _read_package(context.repo)
    dependencies = metadata.get("dependencies", {})
    names = set(dependencies) if isinstance(dependencies, dict) else set()
    return bool(names & {"cosmiconfig", "lilconfig", "rc", "find-config"})


def _imports(source: str) -> dict[str, set[str]]:
    """Local binding names for each CLI framework package imported or required by the file."""
    found: dict[str, set[str]] = {}
    for match in _IMPORT.finditer(source):
        module = match.group("module") or match.group("rmodule")
        framework = _FRAMEWORK_PACKAGES.get(module or "")
        if framework is None:
            continue
        names = found.setdefault(framework, set())
        local = match.group("default") or match.group("required")
        if local:
            names.add(local)
    return found


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _calls(tree: Node, names: set[str]) -> list[Node]:
    if not names:
        return []
    return [
        node
        for node in _walk(tree)
        if node.type == "call_expression" and _text(node.child_by_field_name("function")) in names
    ]


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return [] if arguments is None else [item for item in arguments.named_children if item.type != "comment"]


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type in {
        "as_expression",
        "satisfies_expression",
        "parenthesized_expression",
        "type_assertion",
        "non_null_expression",
    }:
        node = node.named_children[0] if node.named_children else None
    return node


def _resolve(tree: Node, node: Node | None) -> Node | None:
    """Follow an identifier to the object literal it was initialised with in the same file."""
    node = _unwrap(node)
    if node is None or node.type != "identifier":
        return node
    name = _text(node)
    for item in _walk(tree):
        if item.type == "variable_declarator" and _text(item.child_by_field_name("name")) == name:
            return _unwrap(item.child_by_field_name("value"))
    return None


def _pairs(node: Node) -> list[tuple[str, Node | None, int]]:
    result: list[tuple[str, Node | None, int]] = []
    for child in node.named_children:
        if child.type == "pair":
            key_node = child.child_by_field_name("key")
            key = _literal(key_node) if key_node is not None and key_node.type in _STRING_TYPES else _text(key_node)
            if key:
                result.append((key, child.child_by_field_name("value"), child.start_point[0] + 1))
        elif child.type == "shorthand_property_identifier":
            result.append((_text(child), child, child.start_point[0] + 1))
    return result


def _property(node: Node | None, name: str) -> Node | None:
    if node is None or node.type != "object":
        return None
    return next((value for key, value, _ in _pairs(node) if key == name), None)


def _literal(node: Node | None) -> str | None:
    node = _unwrap(node)
    if node is None:
        return None
    if node.type in _STRING_TYPES:
        if any(child.type == "template_substitution" for child in node.named_children):
            return None
        return _text(node)[1:-1]
    if node.type in {"number", "true", "false", "null"}:
        return _text(node)
    return None


def _strings(node: Node | None) -> list[str]:
    node = _unwrap(node)
    if node is None:
        return []
    if node.type == "array":
        return [value for item in node.named_children if (value := _literal(item))]
    value = _literal(node)
    return [value] if value else []


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", errors="replace")


def _kebab(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).replace("_", "-").lower()


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
    imported = _imports(source)
    for framework in ("commander", "yargs", "oclif", "cac", "meow", "arg"):
        if framework in imported:
            return framework
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
    spec_parser = bool({"meow", "arg"} & set(_imports(source)))
    normalized = {item.removeprefix("./") for item in bin_paths if isinstance(item, str)}
    return package in normalized or detected or spec_parser or (bool(bins) and "command(" in source)


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
