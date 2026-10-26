"""Static Go CLI declarations: Cobra, spf13/pflag, the standard ``flag`` package and urfave/cli.

Only literal declarations become symbols. A flag whose name is a variable, a
command whose ``Use`` is computed, or a default that is an expression is left
out (or recorded without a default) so documentation is never reported against
a guess. Environment variables come from ``os.Getenv``/``os.LookupEnv``
literals, urfave ``EnvVars``, viper ``BindEnv`` and ``env:"NAME"`` struct tags;
dynamic prefixes (viper ``SetEnvPrefix``/``AutomaticEnv``, ``envPrefix`` tags,
``os.Environ`` prefix scans) are recorded as partial-prefix symbols so names
under them are unverifiable instead of missing.

Repository functions that register a flag named by one of their parameters
(for example ``StringEnumFlag(cmd, &v, "state", "s", ...)``) are recognised as
helpers, and their call sites are indexed with the literal names they pass.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

from tree_sitter import Node, Query, QueryCursor

from docs_drift.index import register
from docs_drift.index.native_common import (
    dedupe,
    env_prefix_symbol,
    go_executables,
    go_module_name,
    in_error,
    is_product_path,
    language_object,
    line_of,
    make_symbol,
    node_text,
    parser_for,
    read_source,
    repo_files,
    scope_prefix,
    string_literal,
    unresolved_parent,
    walk,
)
from docs_drift.models import Symbol, SymbolKind

LANGUAGE = "go"
COBRA = "github.com/spf13/cobra"
PFLAG = "github.com/spf13/pflag"
VIPER = "github.com/spf13/viper"
SIMPLECOBRA = "github.com/bep/simplecobra"
_FLAG_TYPES = frozenset(
    {
        "",
        "Bool",
        "String",
        "Int",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Uint",
        "Uint8",
        "Uint16",
        "Uint32",
        "Uint64",
        "Float32",
        "Float64",
        "Duration",
        "Count",
        "IP",
        "IPMask",
        "IPNet",
        "IPSlice",
        "BytesHex",
        "BytesBase64",
        "StringSlice",
        "StringArray",
        "IntSlice",
        "Int32Slice",
        "Int64Slice",
        "UintSlice",
        "Float32Slice",
        "Float64Slice",
        "BoolSlice",
        "DurationSlice",
        "StringToString",
        "StringToInt",
        "StringToInt64",
        "Func",
        "BoolFunc",
        "Text",
    }
)
# Defaults are only taken from these scalar kinds; slices, durations and maps are expressions.
_SCALAR_DEFAULT = frozenset(
    {"Bool", "String", "Int", "Int8", "Int16", "Int32", "Int64", "Uint", "Uint8", "Uint16", "Uint32", "Uint64"}
    | {"Float32", "Float64"}
)
_NO_DEFAULT = frozenset({"", "Count", "Func", "BoolFunc", "Text"})
_FLAGSET_METHODS = frozenset({"Flags", "PersistentFlags"})
_EXECUTE = frozenset({"Execute", "ExecuteC", "ExecuteContext", "ExecuteContextC"})
_ENV_READERS = frozenset({"Getenv", "LookupEnv"})
_PREFIX_TESTS = frozenset({"HasPrefix", "CutPrefix", "TrimPrefix"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_TABLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_UPPER_PREFIX = re.compile(r"[A-Z][A-Z0-9]*_?")
_FLAG_NAME = re.compile(r"[A-Za-z0-9][\w.-]*")
_SHORT = re.compile(r"[A-Za-z0-9?]")
_ENVIRON_SCAN = re.compile(rb"\bos\.Environ\(|\benviron\b")
_MAX_CHAIN = 4


@cache
def _query() -> Query:
    source = resources.files("docs_drift.index").joinpath("queries/go.scm").read_text(encoding="utf-8")
    return Query(language_object(LANGUAGE), source)


@cache
def _helper_query(names: tuple[str, ...]) -> Query:
    quoted = " ".join(f'"{name}"' for name in names)
    return Query(
        language_object(LANGUAGE),
        "((call_expression function: [(identifier) @_n (selector_expression field: (field_identifier) @_n)]) @helper"
        f" (#any-of? @_n {quoted}))",
    )


@dataclass(frozen=True)
class _Registration:
    kind: str
    var: bool
    short: bool

    @property
    def name_index(self) -> int:
        return 1 if self.var else 0

    @property
    def short_index(self) -> int | None:
        return self.name_index + 1 if self.short else None

    @property
    def default_index(self) -> int | None:
        if self.kind in _NO_DEFAULT:
            return None
        return self.name_index + (2 if self.short else 1)


def registration(method: str) -> _Registration | None:
    """Parse pflag/flag registration method names such as ``StringVarP`` or ``BoolFunc``."""
    for suffix, var, short in (
        ("VarPF", True, True),
        ("VarP", True, True),
        ("Var", True, False),
        ("PF", False, True),
        ("P", False, True),
        ("", False, False),
    ):
        if not method.endswith(suffix):
            continue
        kind = method[: len(method) - len(suffix)] if suffix else method
        if kind in _FLAG_TYPES and (kind or var):
            return _Registration(kind, var, short)
    return None


@dataclass(eq=False)
class _Command:
    node: Node
    name: str
    framework: str
    aliases: list[str] = field(default_factory=list)
    version: bool = False
    parent: _Command | None = None
    root: bool = False
    symbol: Symbol | None = None


@dataclass(frozen=True)
class _Helper:
    """A repository function that registers a flag named by one of its parameters."""

    params: int
    name_index: int
    short_index: int | None
    short_literal: str | None
    default_index: int | None
    kind: str
    style: str
    owner_index: int | None


def _default_package(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    while len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]):
        parts.pop()
    last = parts[-1] if parts else path
    return re.sub(r"\.v\d+$", "", last).removeprefix("go-").replace("-", "_")


def _call_parts(node: Node) -> tuple[Node | None, str, list[Node]]:
    """Return (receiver, method, arguments) for ``recv.Method(args)`` or ``Func(args)`` calls."""
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    args = [child for child in arguments.named_children if child.type != "comment"] if arguments is not None else []
    if function is None:
        return None, "", args
    if function.type == "selector_expression":
        return function.child_by_field_name("operand"), node_text(function.child_by_field_name("field")), args
    if function.type == "identifier":
        return None, node_text(function), args
    return None, "", args


def _unwrap(node: Node | None) -> Node | None:
    """Strip ``&``, parentheses and literal_element wrappers around an expression."""
    while node is not None and node.type in {"unary_expression", "parenthesized_expression", "literal_element"}:
        if node.type == "unary_expression" and node_text(node.child_by_field_name("operator")) != "&":
            return node
        named = [child for child in node.named_children if child.type != "comment"]
        if len(named) != 1:
            return node
        node = named[0]
    return node


def _composite_type(node: Node) -> tuple[str, str] | None:
    type_node = node.child_by_field_name("type")
    if type_node is not None and type_node.type == "generic_type":
        type_node = type_node.child_by_field_name("type")
    if type_node is None or type_node.type != "qualified_type":
        return None
    return node_text(type_node.child_by_field_name("package")), node_text(type_node.child_by_field_name("name"))


def _fields(node: Node) -> dict[str, Node]:
    """Keyed elements of a composite literal (``Name: value``)."""
    body = node if node.type == "literal_value" else node.child_by_field_name("body")
    result: dict[str, Node] = {}
    for element in body.named_children if body is not None else []:
        if element.type != "keyed_element":
            continue
        named = [child for child in element.named_children if child.type != "comment"]
        if len(named) == 2:  # noqa: PLR2004 - key and value.
            result[node_text(named[0]).strip()] = named[1]
    return result


def _assignments(statement: Node) -> list[tuple[Node, Node]]:
    """(target, value) pairs of a := / = / var statement with matching arity."""
    if statement.type in {"var_spec", "const_spec"}:
        targets = list(statement.children_by_field_name("name"))
        value_list = statement.child_by_field_name("value")
    else:
        left = statement.child_by_field_name("left")
        targets = [child for child in left.named_children if child.type != "comment"] if left is not None else []
        value_list = statement.child_by_field_name("right")
    values = [child for child in value_list.named_children if child.type != "comment"] if value_list else []
    return list(zip(targets, values, strict=True)) if len(targets) == len(values) else []


def _params(function: Node) -> list[tuple[str, str]]:
    """(name, type text) for each parameter, expanding ``a, b string`` groups."""
    parameters = function.child_by_field_name("parameters")
    result: list[tuple[str, str]] = []
    for declaration in parameters.named_children if parameters is not None else []:
        if declaration.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
            continue
        type_text = node_text(declaration.child_by_field_name("type"))
        if declaration.type == "variadic_parameter_declaration":
            type_text = "..." + type_text
        names = list(declaration.children_by_field_name("name"))
        if not names:
            result.append(("", type_text))
        result.extend((node_text(name), type_text) for name in names)
    return result


def _table_scope(node: Node) -> Node | None:
    """The range expression of the enclosing ``for`` loop, else the enclosing statement."""
    statement: Node | None = None
    current = node.parent
    while current is not None and current.type not in {"function_declaration", "method_declaration", "func_literal"}:
        if current.type == "for_statement":
            clause = next((child for child in current.named_children if child.type == "range_clause"), None)
            return clause.child_by_field_name("right") if clause is not None else None
        if statement is None and current.parent is not None and current.parent.type in {"block", "statement_list"}:
            statement = current
        current = current.parent
    return statement


def _receiver_type(method: Node) -> str:
    match = re.search(
        r"\*?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\)$", node_text(method.child_by_field_name("receiver"))
    )
    return match.group(1) if match else ""


class _GoFile:
    """Per-file extraction state; all lookups stay inside this one parsed file."""

    def __init__(  # noqa: PLR0913, PLR0917 - repository context for one parsed file.
        self,
        repo: Path,
        path: Path,
        source: bytes,
        root: Node,
        executables: set[str],
        helpers: dict[str, _Helper],
    ) -> None:
        self.repo = repo
        self.path = path
        self.source = source
        self.root = root
        self.executables = executables
        self.helpers = helpers
        self.is_main = bool(re.search(rb"(?m)^package\s+main\b", source))
        self.prefix = scope_prefix(repo, path)
        captures = QueryCursor(_query()).captures(root)
        check = root.has_error
        self.captures: dict[str, list[Node]] = {
            name: sorted((node for node in nodes if not (check and in_error(node))), key=lambda node: node.start_byte)
            for name, nodes in captures.items()
            if not name.startswith("_")
        }
        self.imports = self._imports()
        aliases = self.imports
        self.cobra = {alias for alias, value in aliases.items() if value == COBRA}
        self.pflag = {alias for alias, value in aliases.items() if value == PFLAG}
        self.stdflag = {alias for alias, value in aliases.items() if value == "flag"}
        self.viper = {alias for alias, value in aliases.items() if value == VIPER}
        self.simplecobra = {alias for alias, value in aliases.items() if value == SIMPLECOBRA}
        self.urfave = {
            alias
            for alias, value in aliases.items()
            if re.fullmatch(
                r"github\.com/urfave/cli(?:/v\d+)?|gopkg\.in/urfave/cli\.v\d+|github\.com/codegangsta/cli", value
            )
        }
        self.os = {alias for alias, value in aliases.items() if value in {"os", "syscall"}}
        self.strings = {alias for alias, value in aliases.items() if value == "strings"}
        self.functions = self.captures.get("function", [])
        self.function_starts = [node.start_byte for node in self.functions]
        self.consts = self._consts()
        self.bindings: dict[int, dict[str, list[Node]]] = {}
        for statement in [*self.captures.get("bind", []), *self.captures.get("spec", [])]:
            scope = self.scope_key(statement)
            for target, value in _assignments(statement):
                self.bindings.setdefault(scope, {}).setdefault(node_text(target), []).append(value)
        self.commands: dict[int, _Command] = {}
        self.returns: dict[str, _Command] = {}
        self.symbols: list[Symbol] = []

    # -- lookups -------------------------------------------------------
    def _imports(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in self.captures.get("import", []):
            path = string_literal(node.child_by_field_name("path"))
            if not path:
                continue
            alias_node = node.child_by_field_name("name")
            alias = node_text(alias_node) if alias_node is not None else _default_package(path)
            if alias and alias not in {"_", "."}:
                aliases[alias] = path
        return aliases

    def _consts(self) -> dict[str, str]:
        """``const``/``var`` names bound to one string literal (names with conflicting values are dropped)."""
        seen: dict[str, str | None] = {}
        for node in self.captures.get("spec", []):
            for target, value in _assignments(node):
                name = node_text(target)
                literal = string_literal(value)
                seen[name] = literal if name not in seen or seen[name] == literal else None
        return {name: value for name, value in seen.items() if value is not None}

    def enclosing_function(self, node: Node) -> Node | None:
        index = bisect.bisect_right(self.function_starts, node.start_byte) - 1
        if index >= 0 and node.end_byte <= self.functions[index].end_byte:
            return self.functions[index]
        return None

    def scope_key(self, node: Node) -> int:
        function = self.enclosing_function(node)
        return function.start_byte if function is not None else -1

    def lookup(self, node: Node) -> list[Node]:
        """Values bound to this expression's text in its function, then at package level."""
        text = node_text(node)
        local = self.bindings.get(self.scope_key(node), {}).get(text, [])
        return [*local, *self.bindings.get(-1, {}).get(text, [])]

    def literal(self, node: Node | None) -> str | None:
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "identifier":
            return self.consts.get(node_text(node))
        return string_literal(node)

    def string_list(self, node: Node | None) -> list[str] | None:
        """Literal ``[]string{"a", "b"}`` contents; None if any element is not a literal."""
        node = _unwrap(node)
        if node is None or node.type != "composite_literal":
            return None
        body = node.child_by_field_name("body")
        values = []
        for element in body.named_children if body is not None else []:
            if element.type == "comment":
                continue
            value = self.literal(element)
            if value is None:
                return None
            values.append(value)
        return values

    def scalar(self, node: Node | None, kind: str) -> str | None:
        """A default only when it is an unambiguous, non-zero literal of the flag's own type."""
        node = _unwrap(node)
        if node is None or kind not in _SCALAR_DEFAULT:
            return None
        if kind == "String":
            # Empty strings conventionally mean "computed elsewhere", so they are not a documented default.
            return self.literal(node) or None
        if kind == "Bool":
            return node_text(node) if node.type in {"true", "false"} else None
        compact = node_text(node).replace("_", "").replace(" ", "")
        numeric = node.type in {"int_literal", "float_literal"} or (
            node.type == "unary_expression" and re.fullmatch(r"-\d+(?:\.\d+)?", compact) is not None
        )
        if numeric and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", compact) and float(compact) != 0:
            return compact
        return None

    def resolve_command(self, expression: Node | None, depth: int = 0) -> _Command | None:
        expression = _unwrap(expression)
        if expression is None or depth > _MAX_CHAIN:
            return None
        if expression.start_byte in self.commands and expression.type in {"composite_literal", "call_expression"}:
            found = self.commands[expression.start_byte]
            if found.node.type == expression.type:
                return found
        if expression.type == "call_expression":
            receiver, method, _ = _call_parts(expression)
            return self.returns.get(method) if receiver is None else None
        for value in self.lookup(expression):
            resolved = self.resolve_command(value, depth + 1)
            if resolved is not None:
                return resolved
        return None

    # -- extraction ----------------------------------------------------
    def run(self) -> list[Symbol]:
        if self.cobra:
            self.cobra_commands()
        if self.simplecobra:
            self.simplecobra_commands()
        if self.urfave:
            self.urfave_commands()
        if self.stdflag or self.pflag:
            self.flagset_commands()
        self.link_commands()
        self.emit_commands()
        self.registrations()
        if self.urfave:
            for node in self.captures.get("composite", []):
                shape = _composite_type(node)
                if shape is not None and shape[0] in self.urfave and shape[1].endswith("Flag"):
                    self.urfave_flag(node, shape[1])
        self.helper_calls()
        self.environment()
        return dedupe(self.symbols)

    def bind_returns(self, literal: Node, command: _Command) -> None:
        """Remember functions that return this command, directly or through a variable."""
        function = self.enclosing_function(literal)
        if function is None or function.type != "function_declaration":
            return
        holder = literal.parent
        while holder is not None and holder.type in {"unary_expression", "parenthesized_expression", "expression_list"}:
            holder = holder.parent
        names: set[str] = set()
        if holder is not None and holder.type in {"short_var_declaration", "assignment_statement", "var_spec"}:
            names = {node_text(target) for target, value in _assignments(holder) if _unwrap(value) == literal}
        for node in walk(function):
            if node.type != "return_statement":
                continue
            returned = node_text(node).removeprefix("return").strip()
            if node == holder or returned in names:
                self.returns.setdefault(node_text(function.child_by_field_name("name")), command)

    def cobra_commands(self) -> None:
        for node in self.captures.get("composite", []):
            shape = _composite_type(node)
            if shape is None or shape[0] not in self.cobra or shape[1] != "Command":
                continue
            fields = _fields(node)
            use = self.literal(fields.get("Use"))
            name = use.split()[0] if use and use.split() else ""
            if not _FLAG_NAME.fullmatch(name):
                continue
            command = _Command(
                node, name, "cobra", aliases=self.string_list(fields.get("Aliases")) or [], version="Version" in fields
            )
            self.commands[node.start_byte] = command
            self.bind_returns(node, command)
        # Field assignments such as ``cmd.Aliases = []string{...}`` or ``cmd.Version = v``.
        for statement in self.captures.get("bind", []):
            if statement.type != "assignment_statement":
                continue
            for target, value in _assignments(statement):
                if target.type != "selector_expression":
                    continue
                owner = self.resolve_command(target.child_by_field_name("operand"))
                field_name = node_text(target.child_by_field_name("field"))
                if owner is not None and field_name == "Aliases":
                    owner.aliases.extend(self.string_list(value) or [])
                elif owner is not None and field_name == "Version":
                    owner.version = True

    def simplecobra_commands(self) -> None:  # noqa: PLR0912 - Name/Init/alias pairing per type.
        """bep/simplecobra: ``Name() string { return "x" }`` on types with ``Init(*simplecobra.Commandeer)``."""
        init_methods: dict[str, Node] = {}
        names: dict[str, tuple[Node, str]] = {}
        for node in self.functions:
            if node.type != "method_declaration":
                continue
            method = node_text(node.child_by_field_name("name"))
            receiver = _receiver_type(node)
            if method == "Init" and any(
                f"{alias}.Commandeer" in type_text for alias in self.simplecobra for _, type_text in _params(node)
            ):
                init_methods[receiver] = node
            elif method == "Name" and not _params(node):
                body = node.child_by_field_name("body")
                returns = [child for child in walk(body) if child.type == "return_statement"] if body else []
                if len(returns) == 1:
                    values = [child for child in returns[0].named_children if child.type == "expression_list"]
                    value = values[0].named_children[0] if values and values[0].named_child_count == 1 else None
                    literal = self.literal(value)
                    if literal and _FLAG_NAME.fullmatch(literal):
                        names[receiver] = (node, literal)
        for receiver, (node, literal) in names.items():
            init = init_methods.get(receiver)
            if init is None:
                continue
            command = _Command(node, literal, "simplecobra")
            self.commands[node.start_byte] = command
            # Inside Init, ``cmd := cd.CobraCommand`` and ``cd.CobraCommand`` both denote this command.
            scope = self.bindings.setdefault(init.start_byte, {})
            for param, type_text in _params(init):
                if any(f"{alias}.Commandeer" in type_text for alias in self.simplecobra):
                    scope.setdefault(f"{param}.CobraCommand", []).append(node)
            for name, values in list(scope.items()):
                if any(node_text(value).endswith(".CobraCommand") for value in values):
                    scope.setdefault(name, []).append(node)
        for statement in self.captures.get("bind", []):
            if statement.type != "assignment_statement":
                continue
            for target, value in _assignments(statement):
                if target.type == "selector_expression" and node_text(target.child_by_field_name("field")) == "Aliases":
                    owner = self.resolve_simple(target.child_by_field_name("operand"))
                    if owner is not None:
                        owner.aliases.extend(self.string_list(value) or [])

    def resolve_simple(self, node: Node | None) -> _Command | None:
        if node is None:
            return None
        for value in self.lookup(node):
            if value.start_byte in self.commands and value.type == "method_declaration":
                return self.commands[value.start_byte]
        return None

    def urfave_literals(self) -> list[tuple[Node, str]]:
        """Typed ``cli.Command{}``/``cli.App{}`` literals plus elided ``{...}`` elements of ``[]*cli.Command{}``."""
        found: list[tuple[Node, str]] = []
        for node in self.captures.get("composite", []):
            shape = _composite_type(node)
            if shape is not None and shape[0] in self.urfave and shape[1] in {"Command", "App"}:
                found.append((node, shape[1]))
        element_type = re.compile(r"\[\]\*?(?:" + "|".join(re.escape(alias) for alias in self.urfave) + r")\.Command")
        for node in self.captures.get("slice", []):
            if not element_type.fullmatch(node_text(node.child_by_field_name("type")).replace(" ", "")):
                continue
            body = node.child_by_field_name("body")
            for element in body.named_children if body is not None else []:
                inner = (
                    element.named_children[0] if element.type == "literal_element" and element.named_children else None
                )
                if inner is not None and inner.type == "literal_value":
                    found.append((inner, "Command"))
        return sorted(found, key=lambda item: item[0].start_byte)

    def urfave_commands(self) -> None:
        for node, kind in self.urfave_literals():
            shape = ("", kind)
            fields = _fields(node)
            raw = self.literal(fields.get("Name"))
            names = [part.strip() for part in raw.split(",") if part.strip()] if raw else []
            if not names and shape[1] == "App" and self.is_main and "Name" not in fields:
                # urfave/cli names an anonymous App after the running executable.
                names = [self.directory_executable()]
            if not names or not all(_FLAG_NAME.fullmatch(name) for name in names):
                continue
            aliases = [*names[1:], *(self.string_list(fields.get("Aliases")) or [])]
            short_name = self.literal(fields.get("ShortName"))
            if short_name:
                aliases.append(short_name)
            command = _Command(node, names[0], "urfave-cli", aliases=aliases, version="Version" in fields)
            command.root = shape[1] == "App"
            self.commands[node.start_byte] = command
            self.bind_returns(node, command)
        for command in self.commands.values():
            if command.framework != "urfave-cli":
                continue
            ancestor = command.node.parent
            while ancestor is not None:
                if ancestor.type in {"composite_literal", "literal_value"} and ancestor.start_byte in self.commands:
                    command.parent = self.commands[ancestor.start_byte]
                    break
                ancestor = ancestor.parent

    def directory_executable(self) -> str:
        directory = self.path.parent
        if (directory / "go.mod").is_file():
            return go_module_name(self.repo, directory) or ""
        return directory.name

    def flagset_commands(self) -> None:
        """``flag.NewFlagSet("serve", ...)`` is the standard library's subcommand idiom."""
        for node in self.captures.get("call", []):
            receiver, method, args = _call_parts(node)
            if method != "NewFlagSet" or node_text(receiver) not in self.stdflag | self.pflag or not args:
                continue
            name = self.literal(args[0])
            if name and _FLAG_NAME.fullmatch(name):
                framework = "go-flag" if node_text(receiver) in self.stdflag else "pflag"
                self.commands[node.start_byte] = _Command(node, name, framework)

    def link_commands(self) -> None:
        executed: set[int] = set()
        for node in self.captures.get("call", []):
            receiver, method, args = _call_parts(node)
            if receiver is None:
                continue
            if method == "AddCommand":
                parent = self.resolve_command(receiver)
                for argument in args:
                    child = self.resolve_command(argument)
                    if child is not None and parent is not None and child is not parent:
                        child.parent = parent
            elif method in _EXECUTE or (method in {"Run", "RunContext"} and self.urfave):
                command = self.resolve_command(receiver)
                if command is not None:
                    executed.add(id(command))
        for command in self.commands.values():
            if command.parent is not None:
                command.root = False
            elif command.name in self.executables or id(command) in executed:
                command.root = True

    def scope_of(self, command: _Command | None) -> str:
        if command is None:
            return "flags"
        return "root" if command.root else command.name

    def emit_commands(self) -> None:
        ordered = sorted(self.commands.values(), key=lambda item: (item.parent is not None, item.node.start_byte))
        for command in ordered:
            parent_name = command.parent.name if command.parent is not None else None
            command.symbol = make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_command,
                ".".join(part for part in (self.prefix, parent_name, command.name) if part),
                line_of(command.node),
                name=command.name,
                aliases=[alias for alias in command.aliases if _FLAG_NAME.fullmatch(alias)],
                language=LANGUAGE,
                framework=command.framework,
            )
            self.symbols.append(command.symbol)
        for command in ordered:
            if command.symbol is not None and command.parent is not None and command.parent.symbol is not None:
                command.symbol.parent_id = command.parent.symbol.symbol_id
            elif command.symbol is not None and not command.root:
                command.symbol.parent_id = unresolved_parent(command.symbol)
            self.derived_for(command)

    def derived_for(self, command: _Command) -> None:
        """Help/version flags and help/completion commands the frameworks add implicitly."""
        if command.framework == "go-flag":
            return  # the standard library's -h/-help is added with the FlagSet's first flag
        scope = self.scope_of(command)
        flags: list[tuple[str, list[str]]] = [("--help", ["-h"])]
        if command.root and command.version:
            flags.append(("--version", ["-v"] if command.framework == "urfave-cli" else []))
        for name, aliases in flags:
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
        if command.root and command.symbol is not None:
            implicit = ["help", "completion"] if command.framework in {"cobra", "simplecobra"} else ["help"]
            for name in implicit:
                symbol = make_symbol(
                    self.repo,
                    self.path,
                    SymbolKind.cli_command,
                    f"{self.prefix}.{command.name}.{name}",
                    line_of(command.node),
                    name=name,
                    aliases=["h"] if command.framework == "urfave-cli" and name == "help" else [],
                    language=LANGUAGE,
                    framework=command.framework,
                    derived=True,
                )
                symbol.parent_id = command.symbol.symbol_id
                self.symbols.append(symbol)

    # -- pflag / flag registrations ------------------------------------
    def flagset_style(  # noqa: PLR0911, PLR0912 - each FlagSet origin is a distinct literal shape.
        self, receiver: Node | None, depth: int = 0
    ) -> tuple[str, _Command | None] | None:
        """Classify a registration receiver as ('pflag'|'flag', owning command) or None if unknown."""
        receiver = _unwrap(receiver)
        if receiver is None or depth > _MAX_CHAIN:
            return None
        text = node_text(receiver)
        if receiver.type == "identifier" and text in self.stdflag:
            return "flag", None
        if receiver.type == "identifier" and text in self.pflag:
            return "pflag", None
        if receiver.type == "call_expression":
            inner, method, _ = _call_parts(receiver)
            if method in _FLAGSET_METHODS and inner is not None and (self.cobra or self.pflag or self.simplecobra):
                owner = self.resolve_command(inner) or self.resolve_simple(inner)
                return "pflag", owner
            if method == "NewFlagSet" and inner is not None:
                command = self.commands.get(receiver.start_byte)
                if node_text(inner) in self.stdflag:
                    return "flag", command
                if node_text(inner) in self.pflag:
                    return "pflag", command
            return None
        for value in self.lookup(receiver):
            found = self.flagset_style(value, depth + 1)
            if found is not None:
                return found
        function = self.enclosing_function(receiver)
        for param, type_text in _params(function) if function is not None else []:
            if param != text:
                continue
            if any(type_text.endswith(f"{alias}.FlagSet") for alias in self.stdflag):
                return "flag", None
            if any(type_text.endswith(f"{alias}.FlagSet") for alias in self.pflag):
                return "pflag", None
        return None

    def registrations(self) -> None:
        for node in self.captures.get("registration", []):
            receiver, method, args = _call_parts(node)
            spec = registration(method)
            if spec is None or receiver is None or len(args) <= spec.name_index:
                continue
            classified = self.flagset_style(receiver)
            if classified is None:
                continue
            style, owner = classified
            name = self.literal(args[spec.name_index])
            short = (
                self.literal(args[spec.short_index])
                if spec.short_index is not None and len(args) > spec.short_index
                else None
            )
            default = (
                self.scalar(args[spec.default_index], spec.kind)
                if spec.default_index is not None and len(args) > spec.default_index
                else None
            )
            self.add_flag(node, name, short, default, style, owner)

    def helper_calls(self) -> None:
        names = tuple(sorted(name for name in self.helpers if name.encode() in self.source))
        if not names:
            return
        captures = QueryCursor(_helper_query(names)).captures(self.root)
        for node in sorted(captures.get("helper", []), key=lambda item: item.start_byte):
            if self.root.has_error and in_error(node):
                continue
            _, method, args = _call_parts(node)
            helper = self.helpers.get(method)
            if helper is None or len(args) != helper.params:
                continue
            name = self.literal(args[helper.name_index])
            short = self.literal(args[helper.short_index]) if helper.short_index is not None else helper.short_literal
            default = self.scalar(args[helper.default_index], helper.kind) if helper.default_index is not None else None
            owner = self.resolve_command(args[helper.owner_index]) if helper.owner_index is not None else None
            self.add_flag(node, name, short, default, helper.style, owner)

    def add_flag(  # noqa: PLR0913, PLR0917 - one declaration.
        self, node: Node, name: str | None, short: str | None, default: str | None, style: str, owner: _Command | None
    ) -> None:
        if not name or not _FLAG_NAME.fullmatch(name):
            return
        aliases: list[str] = []
        if style == "flag":
            # The standard library accepts one or two dashes; documentation usually shows one.
            primary, aliases = f"-{name}", [f"--{name}"]
            framework = "go-flag"
            self.add_help(node, owner, "-h", ["-help", "--help", "--h"], framework)
        else:
            primary = f"--{name}"
            if short and _SHORT.fullmatch(short):
                aliases.append(f"-{short}")
            framework = "cobra" if (self.cobra or self.simplecobra) else "pflag"
        self.symbols.append(
            make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_flag,
                f"{self.prefix}.{self.scope_of(owner)}.{primary}",
                line_of(node),
                name=primary,
                aliases=aliases,
                default=default,
                language=LANGUAGE,
                framework=framework,
            )
        )

    def add_help(self, node: Node, owner: _Command | None, name: str, aliases: list[str], framework: str) -> None:
        self.symbols.append(
            make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_flag,
                f"{self.prefix}.{self.scope_of(owner)}.{name}",
                line_of(node),
                name=name,
                aliases=aliases,
                language=LANGUAGE,
                framework=framework,
                derived=True,
            )
        )

    def urfave_flag(self, node: Node, type_name: str) -> None:
        fields = _fields(node)
        raw = self.literal(fields.get("Name"))
        names = [part.strip() for part in raw.split(",") if part.strip()] if raw else []
        names.extend(self.string_list(fields.get("Aliases")) or [])
        if not names or not all(_FLAG_NAME.fullmatch(name) for name in names):
            return
        owner: _Command | None = None
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.type in {"composite_literal", "literal_value"} and ancestor.start_byte in self.commands:
                owner = self.commands[ancestor.start_byte]
                break
            ancestor = ancestor.parent
        rendered = [f"--{name}" if len(name) > 1 else f"-{name}" for name in names]
        # urfave/cli parses with the standard library, so ``-name`` is accepted for long names too.
        aliases = [*rendered[1:], *(f"-{name}" for name in names if len(name) > 1)]
        kind = type_name.removesuffix("Flag")
        self.symbols.append(
            make_symbol(
                self.repo,
                self.path,
                SymbolKind.cli_flag,
                f"{self.prefix}.{self.scope_of(owner)}.{rendered[0]}",
                line_of(node),
                name=rendered[0],
                aliases=aliases,
                default=self.scalar(fields.get("Value"), kind),
                language=LANGUAGE,
                framework="urfave-cli",
            )
        )
        envs = list(self.string_list(fields.get("EnvVars")) or [])
        single = self.literal(fields.get("EnvVar"))
        if single:
            envs.extend(part.strip() for part in single.split(","))
        sources = _unwrap(fields.get("Sources"))
        if sources is not None and sources.type == "call_expression":
            _, method, args = _call_parts(sources)
            if method == "EnvVars":
                envs.extend(value for arg in args if (value := self.literal(arg)))
        for env in envs:
            self.add_env(node, env, "urfave-cli")

    # -- environment ---------------------------------------------------
    def add_env(self, node: Node, name: str, framework: str | None, *, derived: bool = False) -> None:
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

    def add_prefix(self, node: Node, prefix: str, framework: str) -> None:
        self.symbols.append(
            env_prefix_symbol(self.repo, self.path, prefix, line_of(node), language=LANGUAGE, framework=framework)
        )

    def environment(self) -> None:  # noqa: PLR0912 - distinct literal environment forms.
        environ_scan = bool(_ENVIRON_SCAN.search(self.source))
        viper_prefix: list[tuple[Node, str]] = []
        automatic: list[Node] = []
        tables: dict[int, Node] = {}
        for node in self.captures.get("call", []):
            receiver, method, args = _call_parts(node)
            receiver_text = node_text(receiver)
            if receiver_text in self.os and method in _ENV_READERS and args:
                value = self.literal(args[0])
                if value:
                    self.add_env(node, value, None)
                else:
                    scope = _table_scope(node)
                    if scope is not None:
                        tables[scope.start_byte] = scope
            elif self.viper and method == "BindEnv" and len(args) > 1:
                for arg in args[1:]:
                    value = self.literal(arg)
                    if value:
                        self.add_env(node, value, "viper")
            elif self.viper and method == "SetEnvPrefix" and args:
                value = self.literal(args[0])
                if value:
                    viper_prefix.append((node, value.upper() + "_"))
            elif self.viper and method == "AutomaticEnv":
                automatic.append(node)
            elif receiver_text in self.strings and method in _PREFIX_TESTS and len(args) == 2 and environ_scan:  # noqa: PLR2004
                value = self.literal(args[1])
                if value and _UPPER_PREFIX.fullmatch(value):
                    self.add_prefix(node, value, "environ-prefix")
        for node in self.captures.get("tagged", []):
            tag = string_literal(node.child_by_field_name("tag"))
            env = re.search(r'(?:^|\s)env:"([^"]*)"', tag or "")
            if env and env.group(1).split(",", 1)[0] not in {"", "-"}:
                self.add_env(node, env.group(1).split(",", 1)[0], "env-tag")
            prefix = re.search(r'(?:^|\s)envPrefix:"([^"]*)"', tag or "")
            if prefix and prefix.group(1):
                self.add_prefix(node, prefix.group(1), "env-tag")
        for node, name in viper_prefix:
            self.add_prefix(node, name, "viper")
        if automatic and not viper_prefix:
            # AutomaticEnv without a prefix maps every configuration key to an environment name.
            self.add_prefix(automatic[0], "", "viper")
        for scope in tables.values():
            # ``for _, key := range []string{"APP_A", "APP_B"} { os.Getenv(key) }``: the literal table is the evidence.
            for node in walk(scope):
                if node.type in {"interpreted_string_literal", "raw_string_literal"}:
                    value = string_literal(node)
                    if value and _ENV_TABLE_NAME.fullmatch(value):
                        self.add_env(node, value, "env-table", derived=True)


def _helper_signatures(repo: Path, sources: dict[Path, bytes]) -> dict[str, _Helper]:
    """Find functions that register a flag whose name is one of their own parameters."""
    found: dict[str, _Helper | None] = {}
    for path, source in sources.items():
        if b"Flags()" not in source and b"FlagSet" not in source:
            continue
        file = _GoFile(repo, path, source, parser_for(LANGUAGE).parse(source).root_node, set(), {})
        if not (file.cobra or file.pflag or file.stdflag or file.simplecobra):
            continue
        for node in file.captures.get("registration", []):
            receiver, method, args = _call_parts(node)
            spec = registration(method)
            function = file.enclosing_function(node)
            if spec is None or function is None or len(args) <= spec.name_index:
                continue
            params = _params(function)
            if not params or any(type_text.startswith("...") for _, type_text in params):
                continue
            names = [name for name, _ in params]
            name_arg = _unwrap(args[spec.name_index])
            if name_arg is None or name_arg.type != "identifier" or node_text(name_arg) not in names:
                continue
            classified = file.flagset_style(receiver)
            if classified is None:
                continue

            def param_index(argument: Node | None, names: list[str] = names) -> int | None:
                text = node_text(_unwrap(argument))
                return names.index(text) if text in names else None

            short_arg = (
                args[spec.short_index] if spec.short_index is not None and len(args) > spec.short_index else None
            )
            default_arg = (
                args[spec.default_index] if spec.default_index is not None and len(args) > spec.default_index else None
            )
            inner = _unwrap(receiver)
            owner_index = None
            if inner is not None and inner.type == "call_expression":
                owner, owner_method, _ = _call_parts(inner)
                if owner_method in _FLAGSET_METHODS:
                    owner_index = param_index(owner)
            helper = _Helper(
                params=len(params),
                name_index=names.index(node_text(name_arg)),
                short_index=param_index(short_arg),
                short_literal=file.literal(short_arg) if param_index(short_arg) is None else None,
                default_index=param_index(default_arg),
                kind=spec.kind,
                style=classified[0],
                owner_index=owner_index,
            )
            key = node_text(function.child_by_field_name("name"))
            found[key] = helper if key not in found or found[key] == helper else None
    return {name: helper for name, helper in found.items() if helper is not None and registration(name) is None}


_CONTEXT: dict[str, tuple[str, set[str], dict[str, _Helper]]] = {}


def _repository_context(repo: Path) -> tuple[set[str], dict[str, _Helper]]:
    """Executable names and flag helpers for the whole module tree, cached by file stat signature."""
    paths = [path for path in repo_files(repo, (".go",)) if is_product_path(path.relative_to(repo).as_posix())]
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(repo).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    signature = digest.hexdigest()
    cached = _CONTEXT.get(str(repo))
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    sources = {path: source for path in paths if (source := read_source(path)) is not None}
    main_dirs = {path.parent for path, source in sources.items() if re.search(rb"(?m)^package\s+main\b", source)}
    executables = go_executables(repo, main_dirs)
    helpers = _helper_signatures(repo, sources)
    _CONTEXT[str(repo)] = (signature, executables, helpers)
    return executables, helpers


class GoCLIIndexer:
    name = "go-cli"
    languages = frozenset({LANGUAGE})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        executables, helpers = _repository_context(repo)
        result: list[Symbol] = []
        for path in files:
            if not is_product_path(path.relative_to(repo).as_posix()):
                continue
            source = read_source(path)
            if source is None:
                continue
            root = parser_for(LANGUAGE).parse(source).root_node
            result.extend(_GoFile(repo, path, source, root, executables, helpers).run())
        return result


@register
class _RegisteredGoCLIIndexer(GoCLIIndexer):
    pass
