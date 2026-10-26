"""Static Rust CLI declarations: clap derive and builder APIs, structopt and argh basics.

Derive names follow clap's documented defaults: ``long``/``short`` without a
value use the case-converted field name (kebab-case unless ``rename_all`` says
otherwise), subcommand variants become kebab-case command names, and ``env``
without a value uses the SCREAMING_SNAKE_CASE field name. Builder chains are
read only when ``Arg::new``/``Command::new`` and their ``.long``/``.short``/
``.alias`` calls use literals. Defaults are recorded only for literal
``default_value``/``default_value_t`` values. Executables come from the nearest
``Cargo.toml`` (``[[bin]]`` names, then the package name). Code under tests,
examples, benches or ``#[cfg(test)]`` is skipped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

from tree_sitter import Node, Query, QueryCursor

from docs_drift.index import register
from docs_drift.index.native_common import (
    Crate,
    cargo_crates,
    char_literal,
    convert_case,
    dedupe,
    env_prefix_symbol,
    in_error,
    is_product_path,
    language_object,
    line_of,
    make_symbol,
    node_text,
    parser_for,
    read_source,
    rust_executable,
    scope_prefix,
    string_literal,
    unresolved_parent,
)
from docs_drift.models import Symbol, SymbolKind

LANGUAGE = "rust"
_ARG_ATTRS = frozenset({"arg", "clap", "structopt", "argh"})
_COMMAND_ATTRS = frozenset({"command", "clap", "structopt", "argh"})
_CLAP_DERIVES = frozenset({"Parser", "Args", "Subcommand", "StructOpt", "Clap", "FromArgs"})
_ROOT_CALLS = ("parse", "try_parse", "parse_from", "try_parse_from", "from_args", "from_iter", "from_env")
_SKIP_FIELD = frozenset({"subcommand", "flatten", "skip", "external_subcommand", "positional"})
_NAME = re.compile(r"[A-Za-z0-9][\w.-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_TABLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_ENV_MODULES = frozenset({"env", "envs", "env_vars", "environment"})
_ENV_REGISTRY = re.compile(r"Env(?:Var(?:iable)?s?)?")
_ENV_READ = re.compile(r"(?:^|::)env::var(?:_os)?$")
_COMMAND_CONSTRUCTORS = frozenset({"Command::new", "App::new", "SubCommand::with_name", "clap::Command::new"})
_ARG_CONSTRUCTORS = frozenset({"Arg::new", "Arg::with_name", "clap::Arg::new"})
_CRATE_NAME_MACROS = frozenset({"crate_name", "command"})


@dataclass(frozen=True)
class _Computed:
    """``key = <expression>``: a value is given but it is not a literal, so names derived from it are unknown."""

    text: str


Value = str | list[str] | _Computed | None


@dataclass
class _Attr:
    """One ``#[name(key = value, flag, ...)]`` attribute, flattened to ordered key/value pairs."""

    name: str
    items: list[tuple[str, Value, Node]] = field(default_factory=list)

    def has(self, key: str) -> bool:
        return any(item_key == key for item_key, _, _ in self.items)

    def value(self, key: str) -> str | list[str] | None:
        """The literal value for ``key``; computed values are never returned as names."""
        for item_key, value, _ in self.items:
            if item_key == key and isinstance(value, (str, list)):
                return value
        return None

    def computed(self, key: str) -> str | None:
        """Expression text when ``key`` is set only to a non-literal value."""
        if self.value(key) is not None:
            return None
        for item_key, value, _ in self.items:
            if item_key == key and isinstance(value, _Computed):
                return value.text
        return None


@dataclass
class _Command:
    name: str | None
    node: Node
    framework: str
    aliases: list[str] = field(default_factory=list)
    parent: _Command | None = None
    root: bool = False
    version: bool = False
    help_flag: bool = True
    symbol: Symbol | None = None


def _literal_value(nodes: list[Node]) -> str | list[str] | None:  # noqa: PLR0911 - one return per literal shape.
    """Literal attribute values: strings, chars, numbers, booleans and ``[..]`` lists of those."""
    if not nodes:
        return None
    if len(nodes) == 1:
        node = nodes[0]
        text = string_literal(node)
        if text is not None:
            return text
        char = char_literal(node)
        if char is not None:
            return char
        if node.type in {"integer_literal", "float_literal", "boolean_literal"} or node_text(node) in {"true", "false"}:
            return node_text(node).replace("_", "")
        if node.type == "token_tree" and node_text(node)[:1] in {"[", "("}:
            values = []
            for child in node.named_children:
                item = string_literal(child) or char_literal(child)
                if item is None:
                    return None
                values.append(item)
            return values
        return None
    if len(nodes) == 2 and node_text(nodes[0]) == "-" and nodes[1].type in {"integer_literal", "float_literal"}:  # noqa: PLR2004
        return "-" + node_text(nodes[1]).replace("_", "")
    if len(nodes) == 2 and node_text(nodes[0]) == "&":  # noqa: PLR2004 - ``&["a", "b"]``.
        return _literal_value(nodes[1:])
    return None


def _segments(tree: Node) -> list[list[Node]]:
    """Split a token tree's contents on top-level commas."""
    children = list(tree.children)[1:-1]  # strip the surrounding delimiters
    segments: list[list[Node]] = [[]]
    for child in children:
        if child.type in {"line_comment", "block_comment"}:
            continue
        if node_text(child) == "," and not child.is_named:
            segments.append([])
        else:
            segments[-1].append(child)
    return [segment for segment in segments if segment]


def _parse_items(tree: Node | None) -> list[tuple[str, Value, Node]]:
    if tree is None:
        return []
    items: list[tuple[str, Value, Node]] = []
    for segment in _segments(tree):
        head = segment[0]
        if head.type != "identifier":
            continue
        key = node_text(head)
        if len(segment) >= 2 and node_text(segment[1]) == "=" and not segment[1].is_named:  # noqa: PLR2004
            literal = _literal_value(segment[2:])
            text = " ".join(node_text(node) for node in segment[2:])
            items.append((key, literal if literal is not None else _Computed(text), head))
        elif len(segment) == 1:
            items.append((key, None, head))
        else:
            items.append((key, None, head))
    return items


def _attributes(item: Node) -> list[_Attr]:
    """Attributes written directly before an item, field or enum variant (``cfg_attr`` unwrapped)."""
    result: list[_Attr] = []
    sibling = item.prev_sibling
    while sibling is not None and sibling.type in {"attribute_item", "line_comment", "block_comment"}:
        if sibling.type == "attribute_item":
            result.extend(_attribute_nodes(sibling))
        sibling = sibling.prev_sibling
    # Field and variant attributes may also be children of the item node itself.
    for child in item.children:
        if child.type == "attribute_item":
            result.extend(_attribute_nodes(child))
    result.reverse()
    return result


def _attribute_nodes(item: Node) -> list[_Attr]:
    attribute = next((child for child in item.named_children if child.type == "attribute"), None)
    if attribute is None:
        return []
    path = next(
        (child for child in attribute.named_children if child.type in {"identifier", "scoped_identifier"}), None
    )
    name = node_text(path).rsplit("::", 1)[-1]
    tree = attribute.child_by_field_name("arguments")
    if name == "cfg_attr" and tree is not None:
        nested: list[_Attr] = []
        for segment in _segments(tree)[1:]:
            if len(segment) == 2 and segment[0].type == "identifier" and segment[1].type == "token_tree":  # noqa: PLR2004
                nested.append(_Attr(node_text(segment[0]), _parse_items(segment[1])))
        return nested
    return [_Attr(name, _parse_items(tree))]


def _derive_names(item: Node) -> set[str]:
    """Derive identifiers including ``clap::Parser`` path forms (last segment)."""
    names: set[str] = set()
    for attr_item in _attribute_items(item):
        attribute = next((child for child in attr_item.named_children if child.type == "attribute"), None)
        if attribute is None:
            continue
        path = next((child for child in attribute.named_children if child.type == "identifier"), None)
        tree = attribute.child_by_field_name("arguments")
        if path is None or tree is None:
            continue
        if node_text(path) == "derive":
            for segment in _segments(tree):
                identifiers = [node_text(node) for node in segment if node.type == "identifier"]
                if identifiers:
                    names.add(identifiers[-1])
        elif node_text(path) == "cfg_attr":
            for segment in _segments(tree)[1:]:
                if len(segment) == 2 and node_text(segment[0]) == "derive":  # noqa: PLR2004
                    for inner in _segments(segment[1]):
                        identifiers = [node_text(node) for node in inner if node.type == "identifier"]
                        if identifiers:
                            names.add(identifiers[-1])
    return names


def _attribute_items(item: Node) -> list[Node]:
    result = []
    sibling = item.prev_sibling
    while sibling is not None and sibling.type in {"attribute_item", "line_comment", "block_comment"}:
        if sibling.type == "attribute_item":
            result.append(sibling)
        sibling = sibling.prev_sibling
    return result


def _merged(attrs: list[_Attr], names: frozenset[str]) -> _Attr:
    merged = _Attr("merged")
    for attr in attrs:
        if attr.name in names:
            merged.items.extend(attr.items)
    return merged


def _strings(value: str | list[str] | None) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    return []


def _type_name(node: Node | None) -> str:
    """The last path segment of a type, looking through ``Option<T>``/``Box<T>`` wrappers."""
    text = node_text(node)
    text = re.sub(r"\s+", "", text)
    while True:
        match = re.fullmatch(r"(?:[\w:]+::)?(?:Option|Box)<(.+)>", text)
        if not match:
            break
        text = match.group(1)
    return text.rsplit("::", 1)[-1]


@cache
def _query() -> Query:
    source = resources.files("docs_drift.index").joinpath("queries/rust.scm").read_text(encoding="utf-8")
    return Query(language_object(LANGUAGE), source)


def _is_test_item(item: Node) -> bool:
    for attr in _attributes(item):
        if attr.name == "test" or (attr.name == "cfg" and any(key == "test" for key, _, _ in attr.items)):
            return True
    return False


def _call_path(node: Node) -> str:
    function = node.child_by_field_name("function")
    if function is None:
        return ""
    if function.type in {"scoped_identifier", "identifier"}:
        return node_text(function)
    if function.type == "generic_function":
        inner = function.child_by_field_name("function")
        return node_text(inner)
    return ""


def _method(node: Node) -> tuple[Node | None, str, list[Node]]:
    """(receiver, method, args) for ``recv.method(args)``."""
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    args = (
        [child for child in arguments.named_children if child.type not in {"line_comment", "block_comment"}]
        if arguments is not None
        else []
    )
    if function is not None and function.type == "field_expression":
        return function.child_by_field_name("value"), node_text(function.child_by_field_name("field")), args
    return None, "", args


def _single_literal(args: list[Node]) -> str | None:
    if len(args) != 1:
        return None
    return string_literal(args[0])


def _literal_list(args: list[Node]) -> list[str] | None:
    """``["a", "b"]``, ``&["a"]`` or ``vec!["a"]`` arguments; None when anything is computed."""
    if len(args) != 1:
        return None
    node = args[0]
    if node.type == "reference_expression":
        node = node.child_by_field_name("value") or node
    if node.type == "array_expression":
        values = []
        for child in node.named_children:
            value = string_literal(child) or char_literal(child)
            if value is None:
                return None
            values.append(value)
        return values
    if node.type == "macro_invocation" and node_text(node.child_by_field_name("macro")) == "vec":
        tree = next((child for child in node.named_children if child.type == "token_tree"), None)
        parsed = _literal_value([tree]) if tree is not None else None
        return parsed if isinstance(parsed, list) else None
    return None


def _table_scope(node: Node) -> Node | None:
    """The iterable of the enclosing ``for`` loop, else the enclosing statement (closures included)."""
    statement: Node | None = None
    current = node.parent
    while current is not None and current.type != "function_item":
        if current.type == "for_expression":
            return current.child_by_field_name("value")
        if statement is None and current.parent is not None and current.parent.type == "block":
            statement = current
        current = current.parent
    return statement


class _RustFile:
    def __init__(self, repo: Path, path: Path, root: Node, crates: list[Crate]) -> None:
        self.repo = repo
        self.path = path
        self.root = root
        self.prefix = scope_prefix(repo, path)
        relative = path.relative_to(repo).as_posix()
        self.executable, self.other_names = rust_executable(relative, crates)
        self.text = node_text(root)
        self.symbols: list[Symbol] = []
        self.commands: list[_Command] = []
        self._clap_types: dict[str, bool] = {}
        self._constants: dict[str, str] | None = None
        captures = QueryCursor(_query()).captures(root)
        check = root.has_error
        self.captures: dict[str, list[Node]] = {
            name: sorted((node for node in nodes if not (check and in_error(node))), key=lambda node: node.start_byte)
            for name, nodes in captures.items()
            if not name.startswith("_")
        }
        self.test_ranges = [
            (node.start_byte, node.end_byte)
            for node in [*self.captures.get("scope", []), *self.captures.get("function", [])]
            if _is_test_item(node)
        ]

    def in_test(self, node: Node) -> bool:
        return any(start <= node.start_byte < end for start, end in self.test_ranges)

    def nodes(self, *names: str) -> list[Node]:
        merged = [node for name in names for node in self.captures.get(name, [])]
        return sorted(merged, key=lambda node: node.start_byte) if len(names) > 1 else merged

    def run(self) -> list[Symbol]:
        if re.search(r"\b(?:clap|structopt|argh)\b", self.text):
            self.derive_declarations()
            self.builder_declarations()
            self.emit_commands()
        self.environment()
        return dedupe(self.symbols)

    # -- shared emitters -------------------------------------------------
    def scope(self, command: _Command | None) -> str:
        if command is None:
            return "args"
        if command.root:
            return "root"
        return command.name or "args"

    def flag(  # noqa: PLR0913, PLR0917 - one declaration.
        self,
        node: Node,
        long: str | None,
        short: str | None,
        aliases: list[str],
        default: str | None,
        command: _Command | None,
        framework: str,
    ) -> None:
        names = ([f"--{long}"] if long else []) + ([f"-{short}"] if short else [])
        if not names:
            return
        primary = names[0]
        if not all(_NAME.fullmatch(name.lstrip("-")) for name in names):
            return
        self.symbols.append(
            make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_flag,
                f"{self.prefix}.{self.scope(command)}.{primary}",
                line_of(node),
                name=primary,
                aliases=[*names[1:], *aliases],
                default=default,
                language=LANGUAGE,
                framework=framework,
            )
        )

    def env(self, node: Node, name: str, framework: str | None, *, derived: bool = False) -> None:
        if _ENV_NAME.fullmatch(name):
            self.symbols.append(
                make_symbol(
                    self.repo,
                    self.path,
                    SymbolKind.env_var,
                    f"{self.prefix}.{name}",
                    line_of(node),
                    name=name,
                    language=LANGUAGE,
                    framework=framework,
                    derived=derived,
                )
            )

    # -- derive API ------------------------------------------------------
    def derive_declarations(self) -> None:  # noqa: PLR0912, PLR0915 - type graph for derive containers.
        structs: dict[str, tuple[Node, list[_Attr], set[str]]] = {}
        enums: dict[str, tuple[Node, list[_Attr], set[str]]] = {}
        for node in self.nodes("struct", "enum"):
            if self.in_test(node):
                continue
            derives = _derive_names(node)
            if not derives & _CLAP_DERIVES:
                continue
            attrs = _attributes(node)
            if any(attr.name in {"grammar", "grammar_inline"} for attr in attrs):
                continue  # pest's Parser derive, not clap.
            name = node_text(node.child_by_field_name("name"))
            (structs if node.type == "struct_item" else enums)[name] = (node, attrs, derives)
        struct_command: dict[str, _Command] = {}
        enum_parent: dict[str, _Command | None] = {}
        referenced_as_variant: set[str] = set()
        for node, _, _ in enums.values():
            body = node.child_by_field_name("body")
            for variant in body.named_children if body is not None else []:
                if variant.type == "enum_variant":
                    variant_body = variant.child_by_field_name("body")
                    if variant_body is not None and variant_body.type == "ordered_field_declaration_list":
                        referenced_as_variant.add(_type_name(variant_body.child_by_field_name("type")))
        for name, (node, attrs, derives) in structs.items():
            container = _merged(attrs, _COMMAND_ATTRS)
            is_root_derive = bool(derives & {"Parser", "Clap"}) or (
                bool(derives & {"StructOpt", "FromArgs"}) and not container.has("subcommand")
            )
            called = re.search(rf"\b{re.escape(name)}::(?:{'|'.join(_ROOT_CALLS)})\b", self.text)
            explicit = container.value("name")
            if (
                is_root_derive
                and name not in referenced_as_variant
                and (called or explicit or container.has("version"))
            ):
                command_name = explicit if isinstance(explicit, str) else self.executable
                command = _Command(command_name, node, self.framework(derives), root=True)
                command.version = container.has("version") and container.value("version") != "false"
                command.help_flag = container.value("disable_help_flag") != "true"
                if command_name and command_name != self.executable and self.executable:
                    command.aliases.append(self.executable)
                command.aliases.extend(_strings(container.value("alias")) + _strings(container.value("aliases")))
                struct_command[name] = command
                self.commands.append(command)
            elif container.has("subcommand") and isinstance(explicit, str):  # argh named subcommand struct
                command = _Command(explicit, node, self.framework(derives))
                struct_command[name] = command
                self.commands.append(command)
        # Subcommand fields link enums (and flattened structs) to their owning command.
        changed = True
        passes = 0
        while (changed or passes < 2) and passes < 6:  # noqa: PLR2004 - bounded fixed point over nested commands.
            changed = False
            passes += 1
            for name, (node, _, _) in structs.items():
                owner = struct_command.get(name)
                body = node.child_by_field_name("body")
                for field_node in body.named_children if body is not None else []:
                    if field_node.type != "field_declaration":
                        continue
                    field_attrs = _merged(_attributes(field_node), _COMMAND_ATTRS | _ARG_ATTRS)
                    target = _type_name(field_node.child_by_field_name("type"))
                    if field_attrs.has("subcommand") and target in enums and target not in enum_parent:
                        enum_parent[target] = owner
                        changed = True
                    elif field_attrs.has("flatten") and target in structs and owner and target not in struct_command:
                        struct_command[target] = owner
                        changed = True
            for name, (node, attrs, derives) in enums.items():
                if name not in enum_parent and not (derives & {"Subcommand", "StructOpt", "FromArgs"}):
                    continue
                key = f"enum:{name}"
                if key in struct_command:
                    continue
                parent = enum_parent.get(name)
                if name not in enum_parent and passes < 2:  # noqa: PLR2004 - give owners one pass to link.
                    continue
                struct_command[key] = _Command(None, node, self.framework(derives))
                container = _merged(attrs, _COMMAND_ATTRS)
                rename = container.value("rename_all")
                body = node.child_by_field_name("body")
                for variant in body.named_children if body is not None else []:
                    if variant.type != "enum_variant":
                        continue
                    variant_attrs = _merged(_attributes(variant), _COMMAND_ATTRS)
                    if variant_attrs.has("flatten"):
                        # ``#[command(flatten)] Project(Box<ProjectCommand>)`` splices another enum's variants here.
                        flattened = variant.child_by_field_name("body")
                        inner = (
                            _type_name(flattened.child_by_field_name("type"))
                            if flattened is not None and flattened.type == "ordered_field_declaration_list"
                            else ""
                        )
                        if inner in enums and inner not in enum_parent:
                            enum_parent[inner] = parent
                            changed = True
                        continue
                    if any(variant_attrs.has(key) for key in ("skip", "external_subcommand")) or variant_attrs.computed(
                        "name"
                    ):
                        continue
                    explicit = variant_attrs.value("name")
                    variant_name = node_text(variant.child_by_field_name("name"))
                    command_name = (
                        explicit
                        if isinstance(explicit, str)
                        else convert_case(variant_name, rename if isinstance(rename, str) else None)
                    )
                    if not command_name:
                        continue
                    aliases = [
                        alias
                        for key in ("alias", "aliases", "visible_alias", "visible_aliases")
                        for alias in _strings(variant_attrs.value(key))
                    ]
                    command = _Command(command_name, variant, self.framework(derives), aliases=aliases, parent=parent)
                    self.commands.append(command)
                    variant_body = variant.child_by_field_name("body")
                    if variant_body is None:
                        continue
                    if variant_body.type == "ordered_field_declaration_list":
                        inner = _type_name(variant_body.child_by_field_name("type"))
                        if inner in structs and inner not in struct_command:
                            struct_command[inner] = command
                        elif inner in enums and inner not in enum_parent:
                            enum_parent[inner] = command
                        changed = True
                    elif variant_body.type == "field_declaration_list":
                        self.derive_fields(variant_body, command, None, self.framework(derives))
        for name, (node, attrs, derives) in structs.items():
            container = _merged(attrs, _COMMAND_ATTRS)
            rename = container.value("rename_all")
            body = node.child_by_field_name("body")
            if body is not None and body.type == "field_declaration_list":
                self.derive_fields(
                    body, struct_command.get(name), rename if isinstance(rename, str) else None, self.framework(derives)
                )

    def framework(self, derives: set[str]) -> str:
        if "StructOpt" in derives:
            return "structopt"
        if "FromArgs" in derives:
            return "argh"
        return "clap"

    def derive_fields(  # noqa: PLR0912 - documented clap/structopt/argh field defaults.
        self, body: Node, command: _Command | None, rename: str | None, framework: str
    ) -> None:
        for field_node in body.named_children:
            if field_node.type != "field_declaration":
                continue
            attrs = _merged(_attributes(field_node), _ARG_ATTRS | _COMMAND_ATTRS)
            if not attrs.items or any(attrs.has(key) for key in _SKIP_FIELD):
                continue
            field_name = node_text(field_node.child_by_field_name("name")).removeprefix("r#")
            # An unknown rename_all style leaves derived names unknown; explicit names still count.
            converted = convert_case(field_name, rename) or ""
            if any(attrs.computed(key) for key in ("long", "short", "id", "name")):
                # ``long = CONST`` or ``id = CONST``: the rendered name is not a literal, so it is not guessed.
                self.field_env(field_node, attrs, field_name, framework)
                continue
            explicit_id = attrs.value("id") or attrs.value("name")
            long: str | None = None
            aliases: list[str] = []
            if attrs.has("long") or (framework == "argh" and (attrs.has("switch") or attrs.has("option"))):
                value = attrs.value("long")
                if isinstance(value, str):
                    long = value
                elif not converted:
                    long = None
                elif framework == "argh":
                    long = field_name.replace("_", "-")
                elif isinstance(explicit_id, str):
                    long = explicit_id
                    aliases.append(f"--{converted}")
                else:
                    long = converted
            short: str | None = None
            if attrs.has("short"):
                value = attrs.value("short")
                if isinstance(value, str) and len(value) == 1:
                    short = value
                elif value is None and framework != "argh" and converted:
                    short = converted[:1]
            if long is None and short is None:
                continue
            for key in ("alias", "aliases", "visible_alias", "visible_aliases"):
                aliases.extend(f"--{alias}" for alias in _strings(attrs.value(key)))
            for key in ("short_alias", "short_aliases", "visible_short_alias", "visible_short_aliases"):
                aliases.extend(f"-{alias}" for alias in _strings(attrs.value(key)) if len(alias) == 1)
            default = attrs.value("default_value") or attrs.value("default_value_t")
            if framework == "argh":
                default = None
            self.flag(
                field_node,
                long,
                short,
                aliases,
                default if isinstance(default, str) and default else None,
                command,
                framework,
            )
            self.field_env(field_node, attrs, field_name, framework)

    def field_env(self, field_node: Node, attrs: _Attr, field_name: str, framework: str) -> None:
        """``env = "X"``, bare ``env`` (SCREAMING_SNAKE field name) or ``env = EnvVars::X`` (self-named constant)."""
        if not attrs.has("env"):
            return
        literal = attrs.value("env")
        computed = attrs.computed("env")
        if isinstance(literal, str):
            name: str | None = literal
        elif computed is not None:
            last = computed.replace(" ", "").rsplit("::", 1)[-1]
            name = self.constant_named(last) or (last if _ENV_TABLE_NAME.fullmatch(last) else None)
        else:
            name = convert_case(field_name, "SCREAMING_SNAKE_CASE")
        if name:
            self.env(field_node, name, framework)

    def constant_named(self, name: str) -> str | None:
        if self._constants is None:
            self._constants = {}
            for item in self.nodes("const"):
                value = string_literal(item.child_by_field_name("value"))
                if value is not None:
                    self._constants[node_text(item.child_by_field_name("name"))] = value
        return self._constants.get(name)

    # -- builder API -----------------------------------------------------
    def clap_type(self, name: str) -> bool:
        """Whether ``name`` (Command, App, Arg, SubCommand) refers to clap here, not e.g. std::process::Command."""
        if name not in self._clap_types:
            word = re.compile(rf"(?<![\w:]){name}\b")
            # ``use`` declarations end at the first ``;`` even when their braces nest.
            clap_uses = [match.group(1) for match in re.finditer(r"\buse\s+clap::([^;]*);", self.text)]
            std_uses = [match.group(1) for match in re.finditer(r"\buse\s+std::process::([^;]*);", self.text)]
            imported = any(body.strip() == "*" or word.search(body) for body in clap_uses) or re.search(
                rf"\bclap::{name}\b", self.text
            )
            shadowed = re.search(rf"\bprocess::{name}\b", self.text) or any(word.search(body) for body in std_uses)
            self._clap_types[name] = bool(imported) and not (
                shadowed and not re.search(rf"\bclap::{name}::new\b", self.text)
            )
        return self._clap_types[name]

    def builder_declarations(self) -> None:  # noqa: PLR0912, PLR0915 - builder chain walk.
        constructors: dict[int, _Command] = {}
        for node in self.nodes("constructor", "macro"):
            if self.in_test(node):
                continue
            if node.type == "macro_invocation":
                macro = node_text(node.child_by_field_name("macro")).rsplit("::", 1)[-1]
                if macro == "command" and re.search(r"\b(?:clap|Command)\b", self.text):
                    constructors[node.start_byte] = _Command(self.executable, node, "clap", root=True)
                continue
            path = _call_path(node)
            if path not in _COMMAND_CONSTRUCTORS or not self.clap_type(path.split("::")[-2]):
                continue
            arguments = node.child_by_field_name("arguments")
            arg_nodes = list(arguments.named_children) if arguments is not None else []
            name = _single_literal(arg_nodes)
            crate_named = bool(arg_nodes) and (
                (
                    arg_nodes[0].type == "macro_invocation"
                    and node_text(arg_nodes[0].child_by_field_name("macro")).rsplit("::", 1)[-1] in _CRATE_NAME_MACROS
                )
                or node_text(arg_nodes[0]).replace(" ", "") == 'env!("CARGO_PKG_NAME")'
                or node_text(arg_nodes[0]).replace(" ", "") == 'env!("CARGO_BIN_NAME")'
            )
            if crate_named:
                constructors[node.start_byte] = _Command(self.executable, node, "clap", root=True)
            elif name and _NAME.fullmatch(name):
                root = name == self.executable or name in self.other_names
                constructors[node.start_byte] = _Command(name, node, "clap", root=root)
        if not constructors:
            self.builder_args(constructors)
            return
        bindings = self.let_bindings()
        functions = self.function_tails(constructors)
        for node in self.nodes("command_method"):
            receiver, method, args = _method(node)
            if receiver is None or self.in_test(node):
                continue
            command = self.chain_command(receiver, constructors, bindings, functions)
            if command is None:
                continue
            if method in {"alias", "visible_alias"}:
                command.aliases.extend(_strings(_single_literal(args)))
            elif method in {"aliases", "visible_aliases"}:
                command.aliases.extend(_literal_list(args) or [])
            elif method in {"version", "long_version"}:
                command.version = True
            elif method == "disable_help_flag" and args and node_text(args[0]) == "true":
                command.help_flag = False
            elif method == "disable_version_flag" and args and node_text(args[0]) == "true":
                command.version = False
            elif method in {"subcommand", "subcommands"}:
                children = args
                if (
                    method == "subcommands"
                    and len(args) == 1
                    and args[0].type in {"array_expression", "macro_invocation"}
                ):
                    children = list(args[0].named_children)
                    if args[0].type == "macro_invocation":
                        children = []
                for child in children:
                    sub = self.chain_command(child, constructors, bindings, functions)
                    if sub is not None and sub is not command:
                        sub.parent = command
                        sub.root = False
        for command in constructors.values():
            if command.parent is None and not command.root and command.name == self.executable:
                command.root = True
        self.commands.extend(constructors.values())
        self.builder_args(constructors, bindings, functions)

    def let_bindings(self) -> dict[str, list[Node]]:
        result: dict[str, list[Node]] = {}
        for node in self.nodes("binding"):
            if node.type == "let_declaration":
                pattern = node.child_by_field_name("pattern")
                value = node.child_by_field_name("value")
                if pattern is not None and value is not None:
                    name = node_text(pattern).removeprefix("mut ").strip()
                    result.setdefault(name, []).append(value)
            elif node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is not None and right is not None:
                    result.setdefault(node_text(left), []).append(right)
        return result

    def function_tails(self, constructors: dict[int, _Command]) -> dict[str, Node]:
        """Functions whose body ends in a command chain, so ``.subcommand(build_x())`` can link."""
        result: dict[str, Node] = {}
        for node in self.nodes("function"):
            body = node.child_by_field_name("body")
            if body is None:
                continue
            tail = [child for child in body.named_children if child.type not in {"line_comment", "block_comment"}]
            if tail and tail[-1].type not in {"let_declaration", "expression_statement"}:
                result[node_text(node.child_by_field_name("name"))] = tail[-1]
            elif tail and tail[-1].type == "expression_statement" and tail[-1].named_child_count == 1:
                inner = tail[-1].named_children[0]
                if inner.type == "return_expression" and inner.named_child_count == 1:
                    result[node_text(node.child_by_field_name("name"))] = inner.named_children[0]
        return result

    def chain_command(  # noqa: PLR0911 - bounded chain resolution.
        self,
        node: Node | None,
        constructors: dict[int, _Command],
        bindings: dict[str, list[Node]],
        functions: dict[str, Node],
        depth: int = 0,
    ) -> _Command | None:
        """Follow ``x.arg(..).about(..)`` down to its ``Command::new`` root (or a bound variable)."""
        if node is None or depth > 8:  # noqa: PLR2004 - bounded alias chains.
            return None
        while node is not None and node.type in {"call_expression", "parenthesized_expression", "try_expression"}:
            if node.start_byte in constructors and node.type == "call_expression" and _call_path(node):
                return constructors[node.start_byte]
            if node.type == "call_expression":
                function = node.child_by_field_name("function")
                if function is not None and function.type == "field_expression":
                    node = function.child_by_field_name("value")
                    continue
                if function is not None and function.type == "identifier" and node_text(function) in functions:
                    tail = functions[node_text(function)]
                    return self.chain_command(tail, constructors, bindings, functions, depth + 1)
                return None
            named = [child for child in node.named_children]
            node = named[0] if named else None
        if node is None:
            return None
        if node.start_byte in constructors:
            return constructors[node.start_byte]
        if node.type == "identifier":
            for value in bindings.get(node_text(node), []):
                found = self.chain_command(value, constructors, bindings, functions, depth + 1)
                if found is not None:
                    return found
        return None

    def builder_args(  # noqa: PLR0912 - one branch per clap Arg builder method.
        self,
        constructors: dict[int, _Command],
        bindings: dict[str, list[Node]] | None = None,
        functions: dict[str, Node] | None = None,
    ) -> None:
        bindings = bindings or {}
        functions = functions or {}
        for node in self.nodes("constructor", "macro"):
            if self.in_test(node):
                continue
            if node.type == "macro_invocation" and node_text(node.child_by_field_name("macro")) in {"arg", "clap::arg"}:
                self.arg_macro(node, self.owner_of(node, constructors, bindings, functions))
                continue
            if node.type != "call_expression" or _call_path(node) not in _ARG_CONSTRUCTORS:
                continue
            if not self.clap_type("Arg"):
                continue
            outer = node
            long: str | None = None
            short: str | None = None
            aliases: list[str] = []
            env_names: list[str] = []
            default: str | None = None
            dynamic = False
            while outer.parent is not None and outer.parent.type == "field_expression":
                call = outer.parent.parent
                if call is None or call.type != "call_expression":
                    break
                _, method, args = _method(call)
                literal = _single_literal(args)
                char = char_literal(args[0]) if len(args) == 1 else None
                if method == "long":
                    long = literal
                    dynamic = dynamic or literal is None
                elif method == "short":
                    short = char or (literal if literal and len(literal) == 1 else None)
                    dynamic = dynamic or short is None
                elif method in {"alias", "visible_alias"} and literal:
                    aliases.append(f"--{literal}")
                elif method in {"aliases", "visible_aliases"}:
                    aliases.extend(f"--{value}" for value in _literal_list(args) or [])
                elif method in {"short_alias", "visible_short_alias"} and char:
                    aliases.append(f"-{char}")
                elif method in {"short_aliases", "visible_short_aliases"}:
                    aliases.extend(f"-{value}" for value in _literal_list(args) or [] if len(value) == 1)
                elif method == "env" and literal:
                    env_names.append(literal)
                elif method == "default_value" and literal:
                    default = literal
                outer = call
            if dynamic:
                continue
            owner = self.owner_of(outer, constructors, bindings, functions)
            self.flag(node, long, short, aliases, default, owner, "clap")
            for env_name in env_names:
                self.env(node, env_name, "clap")

    def owner_of(
        self,
        node: Node,
        constructors: dict[int, _Command],
        bindings: dict[str, list[Node]],
        functions: dict[str, Node],
    ) -> _Command | None:
        """The command whose ``.arg``/``.args`` call receives this argument expression."""
        current = node.parent
        while current is not None:
            if current.type == "arguments" and current.parent is not None and current.parent.type == "call_expression":
                receiver, method, _ = _method(current.parent)
                if method in {"arg", "args"}:
                    return self.chain_command(receiver, constructors, bindings, functions)
                return None
            if current.type not in {"array_expression", "reference_expression", "token_tree", "macro_invocation"}:
                return None
            current = current.parent
        return None

    def arg_macro(self, node: Node, owner: _Command | None) -> None:
        """``arg!(-c --config <FILE> "help")``: read the flag tokens, ignoring strings and placeholders."""
        tree = next((child for child in node.named_children if child.type == "token_tree"), None)
        if tree is None:
            return
        tokens = list(tree.children)[1:-1]
        long: str | None = None
        short: str | None = None
        index = 0
        while index < len(tokens):
            token = tokens[index]
            text = node_text(token)
            if text in {"<", "["} or token.type in {"string_literal", "raw_string_literal", "token_tree"}:
                break
            if text == "-" and index + 2 < len(tokens) and node_text(tokens[index + 1]) == "-":
                target = tokens[index + 2]
                value = string_literal(target) if target.type == "string_literal" else node_text(target)
                if value and _NAME.fullmatch(value):
                    long = value
                index += 3
                continue
            if text == "-" and index + 1 < len(tokens):
                value = node_text(tokens[index + 1])
                if len(value) == 1:
                    short = value
                index += 2
                continue
            index += 1
        if long is None and short is None:
            return
        self.flag(node, long, short, [], None, owner, "clap")

    def emit_commands(self) -> None:
        ordered = sorted(
            (command for command in self.commands if command.name),
            key=lambda item: (item.parent is not None, item.node.start_byte),
        )
        for command in ordered:
            parent = command.parent.name if command.parent is not None and command.parent.name else None
            qualified = ".".join(part for part in (self.prefix, parent, command.name) if part)
            assert command.name is not None
            command.symbol = make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_command,
                qualified,
                line_of(command.node),
                name=command.name,
                aliases=[alias for alias in command.aliases if _NAME.fullmatch(alias)],
                language=LANGUAGE,
                framework=command.framework,
            )
            self.symbols.append(command.symbol)
        for command in self.commands:
            if command.symbol is not None and command.parent is not None and command.parent.symbol is not None:
                command.symbol.parent_id = command.parent.symbol.symbol_id
            elif command.symbol is not None and not command.root:
                command.symbol.parent_id = unresolved_parent(command.symbol)
            if command.name is None and not command.root:
                continue
            scope = self.scope(command)
            derived: list[tuple[str, list[str]]] = []
            if command.help_flag:
                derived.append(("--help", ["-h"]))
            if command.root and command.version:
                derived.append(("--version", ["-V"]))
            for name, aliases in derived:
                self.symbols.append(
                    make_symbol(
                        self.repo,
                        self.path,
                        SymbolKind.cli_flag,
                        f"{self.prefix}.{scope}.{name}",
                        line_of(command.node),
                        name=name,
                        aliases=aliases,
                        language=LANGUAGE,
                        framework=command.framework,
                        derived=True,
                    )
                )

    # -- environment -----------------------------------------------------
    def environment(self) -> None:  # noqa: PLR0912 - distinct literal environment forms.
        tables: list[Node] = []
        for node in self.nodes("env_call", "macro"):
            if self.in_test(node):
                continue
            if node.type == "call_expression":
                path = _call_path(node)
                args = node.child_by_field_name("arguments")
                arg_nodes = [child for child in args.named_children] if args is not None else []
                literal = _single_literal(arg_nodes[:1]) if arg_nodes else None
                if _ENV_READ.search(path):
                    if literal is None and arg_nodes:
                        literal = self.constant(arg_nodes[0])
                    if literal:
                        self.env(node, literal, None)
                    elif arg_nodes:
                        scope = _table_scope(node)
                        if scope is not None:
                            tables.append(scope)
                elif path.endswith(("envy::prefixed", "Env::prefixed")) and literal:
                    self.prefix_symbol(node, literal, "envy" if "envy" in path else "figment")
                elif path.endswith("Environment::with_prefix") and literal:
                    self.prefix_symbol(node, literal.rstrip("_") + "_", "config")
            elif node.type == "macro_invocation":
                macro = node_text(node.child_by_field_name("macro"))
                if macro in {"env", "option_env", "std::env", "core::env"}:
                    tree = next((child for child in node.named_children if child.type == "token_tree"), None)
                    first = next((child for child in tree.named_children), None) if tree is not None else None
                    literal = string_literal(first)
                    if literal and not literal.startswith("CARGO_"):
                        self.env(node, literal, "compile-time")
        for item in self.nodes("const"):
            # ``pub mod env { pub const APP_THEME: &str = "APP_THEME"; }`` and ``impl EnvVars { ... }`` registries
            # name environment variables.
            parent = item.parent.parent if item.parent is not None else None
            value = string_literal(item.child_by_field_name("value"))
            registry = parent is not None and (
                (parent.type == "mod_item" and node_text(parent.child_by_field_name("name")) in _ENV_MODULES)
                or (
                    parent.type == "impl_item"
                    and _ENV_REGISTRY.fullmatch(node_text(parent.child_by_field_name("type"))) is not None
                )
            )
            if registry and value and _ENV_TABLE_NAME.fullmatch(value) and not self.in_test(item):
                self.env(item, value, "env-const", derived=True)
        for function in {node.start_byte: node for node in tables}.values():
            # ``for (flag, key) in [("--tabs", "APP_TABS")] { env::var(key) }``: the literal table is the evidence.
            stack = [function]
            while stack:
                current = stack.pop()
                stack.extend(current.named_children)
                if current.type == "string_literal":
                    value = string_literal(current)
                    if value and _ENV_TABLE_NAME.fullmatch(value):
                        self.env(current, value, "env-table", derived=True)
                elif current.type in {"scoped_identifier", "identifier"}:
                    value = self.constant(current)
                    if value and _ENV_TABLE_NAME.fullmatch(value):
                        self.env(current, value, "env-table", derived=True)

    def constant(self, node: Node) -> str | None:
        """Resolve ``NAME`` or ``path::NAME`` through this file's ``const NAME: &str = "..."`` items."""
        if node.type not in {"identifier", "scoped_identifier"}:
            return None
        return self.constant_named(node_text(node).rsplit("::", 1)[-1])

    def prefix_symbol(self, node: Node, prefix: str, framework: str) -> None:
        self.symbols.append(
            env_prefix_symbol(self.repo, self.path, prefix, line_of(node), language=LANGUAGE, framework=framework)
        )


_CRATES: dict[str, tuple[tuple[tuple[str, int, int], ...], list[Crate]]] = {}


def _crates(repo: Path) -> list[Crate]:
    manifests = []
    for path in sorted(repo.rglob("Cargo.toml"))[:2000]:
        relative = path.relative_to(repo).as_posix()
        if not is_product_path(relative) or "/target/" in f"/{relative}":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        manifests.append((relative, stat.st_size, stat.st_mtime_ns))
    signature = tuple(manifests)
    cached = _CRATES.get(str(repo))
    if cached is not None and cached[0] == signature:
        return cached[1]
    crates = cargo_crates(repo)
    _CRATES[str(repo)] = (signature, crates)
    return crates


class RustCLIIndexer:
    name = "rust-cli"
    languages = frozenset({LANGUAGE})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        crates = _crates(repo)
        result: list[Symbol] = []
        for path in files:
            relative = path.relative_to(repo).as_posix()
            if not is_product_path(relative):
                continue
            source = read_source(path)
            if source is None:
                continue
            tree = parser_for(LANGUAGE).parse(source)
            result.extend(_RustFile(repo, path, tree.root_node, crates).run())
        return result


@register
class _RegisteredRustCLIIndexer(RustCLIIndexer):
    pass
