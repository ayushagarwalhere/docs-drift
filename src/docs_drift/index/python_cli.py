"""AST recognizers for common argparse, Click and Typer declarations."""

import ast
import configparser
import tomllib
from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path

from docs_drift.ids import make_symbol_id
from docs_drift.index import register
from docs_drift.index.python_source import read_python
from docs_drift.index.python_symbols import _literal, _module_name, _symbol
from docs_drift.models import Symbol, SymbolKind

MAX_SOURCE_BYTES = 1_000_000
# Libraries whose ``option``/``Option`` factories declare command-line flags.
_CLICK_MODULES = frozenset({"click", "typer", "cloup", "rich_click", "asyncclick"})
# Click decorator factories with built-in flag names when no declarations are given.
_BUILTIN_OPTION_FACTORIES = {
    "version_option": ["--version"],
    "help_option": ["--help"],
    "password_option": ["--password"],
    "confirmation_option": ["--yes"],
}
_OPTION_FACTORIES = frozenset({"option", "Option", *_BUILTIN_OPTION_FACTORIES})
# Click strips these function-name suffixes when deriving a command name.
_COMMAND_SUFFIXES = frozenset({"command", "cmd", "group", "grp"})
_ARGPARSE_GROUPS = ("add_argument_group", "add_mutually_exclusive_group")


def _call_name(node: ast.AST) -> str:
    return ast.unparse(node.func) if isinstance(node, ast.Call) else ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _strings(nodes: Iterable[ast.expr | None]) -> list[str]:
    """String constants given directly or inside list/tuple literals, in source order."""
    values: list[str] = []
    for node in nodes:
        items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
        values.extend(item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return values


def _flag_names(values: Iterable[str], *, split_pairs: bool) -> list[str]:
    """Option strings, with Click/Typer ``--on/--off`` pairs split into both flags."""
    names: list[str] = []
    for value in values:
        for part in value.split("/") if split_pairs else [value]:
            flag = part.strip()
            if len(flag) > 1 and flag.startswith("-") and not any(char.isspace() for char in flag):
                names.append(flag)
    return list(dict.fromkeys(names))


def _primary(flags: list[str]) -> tuple[str, list[str]]:
    name = next((flag for flag in flags if flag.startswith("--")), flags[0])
    return name, [flag for flag in flags if flag != name]


def _typer_help_flags(call: ast.Call) -> list[str]:
    enabled = _keyword(call, "add_help_option")
    if enabled is not None and not (isinstance(enabled, ast.Constant) and enabled.value is True):
        return []
    settings = _keyword(call, "context_settings")
    if settings is None:
        return ["--help"]
    try:
        context = ast.literal_eval(settings)
    except (ValueError, TypeError):
        return []
    if context is None:
        context = {}
    if not isinstance(context, dict):
        return []
    names = context.get("help_option_names")
    if names is None:
        return ["--help"]
    return [name for name in names if isinstance(name, str) and name.startswith("-")] if isinstance(names, list) else []


def _imports_click_family(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] in _CLICK_MODULES for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _CLICK_MODULES:
            return True
    return False


class PythonCLIIndexer:
    name = "python-cli"
    languages = frozenset({"python"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        result: list[Symbol] = []
        for path in files:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            try:
                source, tree = read_python(path)
            except (OSError, SyntaxError, ValueError):
                continue
            result.extend(_argparse(repo, path, source, tree))
            result.extend(_decorator_cli(repo, path, source, tree))
        return result


def _argparse(repo: Path, path: Path, source: str, tree: ast.Module) -> list[Symbol]:  # noqa: PLR0912, PLR0915
    module = _module_name(repo, path)
    parser_vars: dict[str, str | None] = {}
    command_ids: dict[str, str] = {}
    result: list[Symbol] = []
    parsers: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            continue
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        target_names = [target.id for target in targets if isinstance(target, ast.Name)]
        call_value = node.value
        if not isinstance(call_value, ast.Call):
            continue
        call = call_value
        call_name = ast.unparse(call.func)
        if call_name.endswith("ArgumentParser"):
            parsers.append(call)
        if not target_names and not call_name.endswith("add_parser"):
            continue
        owner_var = (
            call.func.value.id if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) else ""
        )
        if call_name.endswith("ArgumentParser"):
            prog = _literal(_keyword(call, "prog"), source)
            for target in target_names:
                parser_vars[target] = None
                if prog:
                    # The executable is metadata for future console-script resolution, not a command itself.
                    command_ids[target] = prog
        elif call_name.endswith("add_parser") and call.args:
            parent_command = parser_vars.get(owner_var)
            command = _literal(call.args[0], source)
            if not command:
                continue
            aliases_node = _keyword(call, "aliases")
            aliases: list[str] = []
            if aliases_node is not None:
                with suppress(ValueError, TypeError):
                    aliases = [item for item in ast.literal_eval(aliases_node) if isinstance(item, str)]
            qualified = f"{module}.{command}".strip(".")
            symbol = _symbol(
                repo,
                path,
                SymbolKind.cli_command,
                qualified,
                node.lineno,
                name=command,
                aliases=aliases,
                framework="argparse",
            )
            if parent_command and parent_command in command_ids:
                symbol.parent_id = command_ids[parent_command]
            if target_names:
                command_ids[target_names[0]] = qualified
            for target in target_names:
                parser_vars[target] = command
            result.append(symbol)
        elif call_name.endswith(("add_subparsers", *_ARGPARSE_GROUPS)):
            # Argument groups add flags to the parser (or subcommand) that created them.
            for target in target_names:
                parser_vars[target] = parser_vars.get(owner_var)
    for call in parsers:
        result.extend(_argparse_help(repo, path, module, call))
    local_vars = _function_parser_vars(tree, source, parser_vars)
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "add_argument"
        ):
            continue
        owner = node.func.value
        parser_name = owner.id if isinstance(owner, ast.Name) else ""
        # Builder functions reuse one local name (``parser = subparsers.add_parser("run")``) for many
        # commands; the assignment in the call's own function wins over the file-wide last assignment.
        scope_vars = local_vars.get(id(node), parser_vars)
        known_owner = parser_name in scope_vars
        options = [value for arg in node.args if (value := _literal(arg, source)) is not None]
        flags = _flag_names(options, split_pairs=False)
        if not flags:
            if not known_owner:
                # A positional of an unknown owner gives no evidence of which command accepts it.
                continue
            position = options[0] if options else "argument"
            symbol = _symbol(
                repo,
                path,
                SymbolKind.cli_argument,
                f"{module}.{scope_vars[parser_name]}.{position}".strip("."),
                node.lineno,
                name=position,
                framework="argparse",
            )
            symbol.nargs = _argparse_nargs(_keyword(node, "nargs"), source)
            result.append(symbol)
            continue
        # Parsers passed into helpers (``register_arguments(parser)``), ``self.parser`` and custom group
        # wrappers are still argparse declarations; without a known command they apply to the root.
        scope = scope_vars.get(parser_name) or "root"
        long_name, aliases = _primary(flags)
        dest = _literal(_keyword(node, "dest"), source)
        default = _literal(_keyword(node, "default"), source)
        action_node = _keyword(node, "action")
        action = _literal(action_node, source)
        if action_node is not None and ast.unparse(action_node).endswith("BooleanOptionalAction"):
            aliases.extend(f"--no-{flag[2:]}" for flag in flags if flag.startswith("--"))
        choices_node = _keyword(node, "choices")
        if choices_node is not None:
            with suppress(ValueError, TypeError):
                aliases.extend(str(item) for item in ast.literal_eval(choices_node))
        qualified = f"{module}.{scope}.{long_name}".strip(".")
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.cli_flag,
                qualified,
                node.lineno,
                name=long_name,
                aliases=aliases + ([dest] if dest else []),
                default="true" if action == "store_true" else default,
                framework="argparse",
            )
        )
    result.extend(_flag_specs(repo, path, module, tree))
    return result


def _argparse_nargs(node: ast.expr | None, source: str) -> str | None:
    """``argparse.REMAINDER`` (and its literal ``"..."``) hands every later token to another program."""
    if node is None:
        return None
    if ast.unparse(node).endswith("REMAINDER"):
        return "..."
    return _literal(node, source)


def _local_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes in a function body, excluding nested functions and classes (their own scopes)."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        found.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
    return found


def _function_parser_vars(
    tree: ast.Module, source: str, file_vars: dict[str, str | None]
) -> dict[int, dict[str, str | None]]:
    """Map each node inside a function to that function's parser variables.

    Names assigned in the function shadow the file-wide map; other names (parameters such as
    ``def add_options(parser)``) keep the file-wide meaning.
    """
    by_node: dict[int, dict[str, str | None]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = _local_nodes(function)
        local: dict[str, str | None] = {}
        assignments = sorted(
            (node for node in nodes if isinstance(node, (ast.Assign, ast.AnnAssign))),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for node in assignments:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            call = node.value
            if not names or not isinstance(call, ast.Call):
                continue
            call_name = ast.unparse(call.func)
            owner = (
                call.func.value.id
                if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                else ""
            )
            value: str | None
            if call_name.endswith("ArgumentParser"):
                value = None
            elif call_name.endswith("add_parser") and call.args:
                value = _literal(call.args[0], source)
                if not value:
                    continue
            elif call_name.endswith(("add_subparsers", *_ARGPARSE_GROUPS)):
                value = local[owner] if owner in local else file_vars.get(owner)
            else:
                continue
            for name in names:
                local[name] = value
        if not local:
            continue
        scoped = {**file_vars, **local}
        for item in nodes:
            by_node[id(item)] = scoped
    return by_node


def _argparse_help(repo: Path, path: Path, module: str, call: ast.Call) -> list[Symbol]:
    """``ArgumentParser`` adds ``-h/--help`` unless ``add_help=False``."""
    enabled = _keyword(call, "add_help")
    if enabled is not None and not (isinstance(enabled, ast.Constant) and enabled.value is True):
        return []
    return [
        _symbol(
            repo,
            path,
            SymbolKind.cli_flag,
            f"{module}.root.--help".strip("."),
            call.lineno,
            name="--help",
            aliases=["-h"],
            framework="argparse",
            derived=True,
        )
    ]


def _flag_specs(repo: Path, path: Path, module: str, tree: ast.Module) -> list[Symbol]:
    """Data-driven parsers: dict literals such as ``{"flags": ["-f", "--format"], ...}`` fed to add_argument."""
    result: list[Symbol] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value in {"flags", "option_strings"}):
                continue
            if not isinstance(value, (ast.List, ast.Tuple)):
                continue
            flags = _flag_names(_strings([value]), split_pairs=False)
            if not flags:
                continue
            name, aliases = _primary(flags)
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.cli_flag,
                    f"{module}.root.{name}".strip("."),
                    node.lineno,
                    name=name,
                    aliases=aliases,
                    framework="argparse",
                )
            )
    return result


def _click_command_name(function: str) -> tuple[str, list[str]]:
    """Click's default name: lower-case, dashes, and a trailing ``-command``/``-group`` style suffix removed."""
    dashed = function.lower().replace("_", "-")
    left, separator, suffix = dashed.rpartition("-")
    if separator and suffix in _COMMAND_SUFFIXES:
        return left, [dashed]
    return dashed, []


def _command_decorator_name(decorator: ast.expr, source: str) -> str | None:
    """An explicit name from ``@group.command("name")`` or ``@cli.group(name="name")``."""
    if not isinstance(decorator, ast.Call):
        return None
    if decorator.args:
        name = _literal(decorator.args[0], source)
        if name:
            return name
    return _literal(_keyword(decorator, "name"), source)


def _registered_functions(tree: ast.Module, typer_apps: set[str], source: str) -> dict[str, tuple[str, str | None]]:
    """Functions turned into commands by calls: ``typer.run(main)`` and ``app.command("x")(handler)``."""
    registered: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.Name):
            continue
        function = node.args[0].id
        if ast.unparse(node.func) == "typer.run":
            registered[function] = ("typer-root", None)
            continue
        factory = node.func
        if isinstance(factory, ast.Call) and isinstance(factory.func, ast.Attribute):
            owner = ast.unparse(factory.func.value).split(".")[0]
            if factory.func.attr in {"command", "group"}:
                framework = "typer" if owner in typer_apps else "click"
                registered[function] = (framework, _command_decorator_name(factory, source))
    return registered


def _decorator_cli(  # noqa: PLR0912, PLR0915 - distinct static Click and Typer declaration forms.
    repo: Path, path: Path, source: str, tree: ast.Module
) -> list[Symbol]:
    module = _module_name(repo, path)
    result: list[Symbol] = []
    consumed: set[int] = set()
    typer_apps = {
        target.id
        for item in ast.walk(tree)
        if isinstance(item, ast.Assign)
        and isinstance(item.value, ast.Call)
        and ast.unparse(item.value.func).endswith("Typer")
        for target in item.targets
        if isinstance(target, ast.Name)
    }
    for item in ast.walk(tree):
        if (
            isinstance(item, ast.Assign)
            and isinstance(item.value, ast.Call)
            and ast.unparse(item.value.func).endswith("Typer")
        ):
            for flag in _typer_help_flags(item.value):
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_flag,
                        f"{module}.root.{flag}",
                        item.lineno,
                        name=flag,
                        framework="typer",
                        derived=True,
                    )
                )
            completion = _keyword(item.value, "add_completion")
            if completion is None or (isinstance(completion, ast.Constant) and completion.value is True):
                for flag in ("--install-completion", "--show-completion"):
                    result.append(
                        _symbol(
                            repo,
                            path,
                            SymbolKind.cli_flag,
                            f"{module}.root.{flag}",
                            item.lineno,
                            name=flag,
                            framework="typer",
                            derived=True,
                        )
                    )
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "add_typer":
            name = _literal(_keyword(item, "name"), source)
            if isinstance(item.func.value, ast.Name) and item.func.value.id in typer_apps and name:
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_command,
                        f"{module}.{name}",
                        item.lineno,
                        name=name,
                        framework="typer",
                    )
                )
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "add_command":
            # ``cli.add_command(cmd, "name")`` / ``name="name"`` registers under an explicit name.
            explicit = item.args[1] if len(item.args) > 1 else _keyword(item, "name")
            name = _literal(explicit, source)
            if name:
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_command,
                        f"{module}.{name}",
                        item.lineno,
                        name=name,
                        framework="click",
                    )
                )
    registered = _registered_functions(tree, typer_apps, source)
    click_commands = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        command = None
        framework = None
        callback = False
        for decorator in node.decorator_list:
            name = ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator)
            if name.split(".")[0] in typer_apps and name.endswith((".command", ".callback")):
                framework = "typer"
                callback = name.endswith(".callback")
            elif name.startswith("click.") or name.endswith(".command") or name.endswith(".group"):
                framework = "click"
            elif name.startswith("typer.") or name.endswith(".command"):
                framework = "typer" if "typer" in name else framework
            if framework and name.endswith((".command", ".group")):
                command = _command_decorator_name(decorator, source) or command
        if framework is None and node.name in registered:
            mode, command = registered[node.name]
            framework = "typer" if mode.startswith("typer") else mode
            callback = mode == "typer-root"
        command_aliases: list[str] = []
        if command:
            command_name = command
        elif framework == "click":
            command_name, command_aliases = _click_command_name(node.name)
        else:
            command_name = node.name.lower().replace("_", "-")
        if framework and not callback:
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.cli_command,
                    f"{module}.{command_name}".strip("."),
                    node.lineno,
                    name=command_name,
                    aliases=command_aliases,
                    framework=framework,
                )
            )
        if framework == "click":
            click_commands = True
            result.extend(_click_options(repo, path, source, node, module, command_name, consumed))
        if framework != "typer":
            continue
        result.extend(
            _typer_parameters(repo, path, source, node, module, "root" if callback else command_name, consumed)
        )
    result.extend(_loose_click_options(repo, path, source, tree, module, consumed, click_commands=click_commands))
    return result


def _is_bool(annotation: ast.expr | None, default: ast.expr | None) -> bool:
    if annotation is not None and ast.unparse(annotation).replace(" ", "") in {"bool", "bool|None", "Optional[bool]"}:
        return True
    return isinstance(default, ast.Constant) and isinstance(default.value, bool)


def _typer_parameters(  # noqa: PLR0913, PLR0917 - static declaration context.
    repo: Path,
    path: Path,
    source: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    scope: str,
    consumed: set[int],
) -> list[Symbol]:
    """Typer turns every parameter into a CLI parameter: explicit Option/Argument metadata or the bare name."""
    result: list[Symbol] = []
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    positional_defaults: list[ast.expr | None] = [None] * (
        len(node.args.posonlyargs) + len(node.args.args) - len(node.args.defaults)
    )
    defaults = [*positional_defaults, *node.args.defaults, *node.args.kw_defaults]
    for argument, default in zip(arguments, defaults, strict=True):
        annotation = argument.annotation
        value_type = annotation
        call = annotation if isinstance(annotation, ast.Call) else None
        if isinstance(annotation, ast.Subscript) and ast.unparse(annotation.value).endswith("Annotated"):
            metadata = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else []
            value_type = metadata[0] if metadata else None
            call = next(
                (
                    item
                    for item in metadata
                    if isinstance(item, ast.Call) and ast.unparse(item.func).endswith(("Option", "Argument"))
                ),
                None,
            )
        if (
            call is None
            and isinstance(default, ast.Call)
            and ast.unparse(default.func).endswith(("Option", "Argument"))
        ):
            call = default
        if value_type is not None and ast.unparse(value_type).endswith("Context"):
            continue
        dashed = "--" + argument.arg.replace("_", "-")
        if call is None:
            # Typer: parameters without a default are CLI arguments; with a default, options named after them.
            if default is None:
                result.append(
                    _symbol(
                        repo,
                        path,
                        SymbolKind.cli_argument,
                        f"{module}.{scope}.{argument.arg}".strip("."),
                        node.lineno,
                        name=argument.arg,
                        framework="typer",
                        derived=True,
                    )
                )
                continue
            aliases = [f"--no-{dashed[2:]}"] if _is_bool(value_type, default) else []
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.cli_flag,
                    f"{module}.{scope}.{dashed}".strip("."),
                    node.lineno,
                    name=dashed,
                    aliases=aliases,
                    default=_literal(default, source),
                    framework="typer",
                    derived=True,
                )
            )
            continue
        consumed.add(id(call))
        option = ast.unparse(call.func).endswith("Option")
        declared = _flag_names(_strings(call.args), split_pairs=True)
        names = declared or [dashed]
        # Old-style ``param: bool = typer.Option(False)`` keeps the default as the first argument.
        option_default = (call.args[0] if call.args else None) if call is default else default
        if option and not declared and _is_bool(value_type, option_default):
            names.append(f"--no-{dashed[2:]}")
        flag, aliases = _primary(names)
        kind = SymbolKind.cli_flag if option else SymbolKind.cli_argument
        result.append(
            _symbol(
                repo,
                path,
                kind,
                f"{module}.{scope}.{flag}".strip("."),
                node.lineno,
                name=flag,
                aliases=aliases,
                default=_literal(default, source),
                framework="typer",
            )
        )
    return result


def _option_symbol(  # noqa: PLR0913, PLR0917 - static declaration context.
    repo: Path, path: Path, source: str, call: ast.Call, module: str, scope: str, *, framework: str
) -> Symbol | None:
    factory = _call_name(call).rsplit(".", 1)[-1]
    nodes: list[ast.expr | None] = [*call.args, _keyword(call, "param_decls")]
    flags = _flag_names(_strings(nodes), split_pairs=True)
    if not flags:
        flags = list(_BUILTIN_OPTION_FACTORIES.get(factory, []))
    if not flags:
        return None
    name, aliases = _primary(flags)
    return _symbol(
        repo,
        path,
        SymbolKind.cli_flag,
        f"{module}.{scope}.{name}".strip("."),
        call.lineno,
        name=name,
        aliases=aliases,
        default=_literal(_keyword(call, "default"), source),
        framework=framework,
        derived=factory in _BUILTIN_OPTION_FACTORIES,
    )


def _click_options(  # noqa: PLR0913, PLR0917 - static declaration context.
    repo: Path,
    path: Path,
    source: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    command: str,
    consumed: set[int],
) -> list[Symbol]:
    result = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        factory = _call_name(decorator).rsplit(".", 1)[-1]
        if factory not in _OPTION_FACTORIES:
            continue
        symbol = _option_symbol(repo, path, source, decorator, module, command, framework="click")
        consumed.add(id(decorator))
        if symbol is not None:
            result.append(symbol)
    return result


def _loose_click_options(  # noqa: PLR0913, PLR0917 - static declaration context.
    repo: Path,
    path: Path,
    source: str,
    tree: ast.Module,
    module: str,
    consumed: set[int],
    *,
    click_commands: bool,
) -> list[Symbol]:
    """Options declared outside a command's own decorator list.

    ``click.Option([...])`` objects appended to ``params``, option lists in shared decorator factories,
    ``click.option(...)(fn)`` calls and ``@click.version_option()`` all add flags whose command is not
    statically known, so they are scoped to the root and inherited by every subcommand claim.
    """
    if not _imports_click_family(tree):
        return []
    result: list[Symbol] = []
    help_names: list[str] = ["--help"] if click_commands else []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "help_option_names":
            help_names.extend(_flag_names(_strings([node.value]), split_pairs=False))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "help_option_names" and value is not None:
                    help_names.extend(_flag_names(_strings([value]), split_pairs=False))
        if not isinstance(node, ast.Call) or id(node) in consumed:
            continue
        factory = _call_name(node).rsplit(".", 1)[-1]
        if factory not in _OPTION_FACTORIES:
            continue
        framework = "typer" if _call_name(node).startswith("typer.") else "click"
        symbol = _option_symbol(repo, path, source, node, module, "root", framework=framework)
        if symbol is not None:
            result.append(symbol)
    names = list(dict.fromkeys(help_names))
    if names:
        name, aliases = _primary(names)
        result.append(
            _symbol(
                repo,
                path,
                SymbolKind.cli_flag,
                f"{module}.root.{name}".strip("."),
                1,
                name=name,
                aliases=aliases,
                framework="click",
                derived=True,
            )
        )
    return result


def _pyproject_entry_points(text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}, []
    project = data.get("project")
    if not isinstance(project, dict):
        return {}, []
    table = project.get("entry-points")
    groups = (
        {
            str(group): {str(key): str(value) for key, value in entries.items()}
            for group, entries in table.items()
            if isinstance(entries, dict)
        }
        if isinstance(table, dict)
        else {}
    )
    scripts = project.get("scripts")
    return groups, [str(name) for name in scripts] if isinstance(scripts, dict) else []


def _setup_cfg_entry_points(text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}, []
    if not parser.has_section("options.entry_points"):
        return {}, []
    groups: dict[str, dict[str, str]] = {}
    scripts: list[str] = []
    for group, body in parser.items("options.entry_points"):
        entries = dict(
            (left.strip(), right.strip()) for left, _, right in (line.partition("=") for line in body.splitlines()) if _
        )
        if group in {"console_scripts", "gui_scripts"}:
            scripts.extend(entries)
        else:
            groups[group] = entries
    return groups, scripts


class PythonEntryPointIndexer:
    """Subcommands registered through entry-point groups such as ``twine.registered_commands``.

    Only groups whose name says they hold commands are read; other groups name plugins, not commands.
    """

    name = "python-entry-points"
    languages = frozenset({"toml", "ini"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        result: list[Symbol] = []
        for path in files:
            if path.name not in {"pyproject.toml", "setup.cfg"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            groups, scripts = (
                _pyproject_entry_points(text) if path.name == "pyproject.toml" else _setup_cfg_entry_points(text)
            )
            relative = path.relative_to(repo).as_posix()
            for group, entries in groups.items():
                if "command" not in group.casefold():
                    continue
                prefix = group.split(".", 1)[0]
                root = prefix if prefix in scripts else scripts[0] if len(scripts) == 1 else None
                parent_id = make_symbol_id(SymbolKind.cli_command, root, relative) if root else None
                for name, target in entries.items():
                    if not name:
                        continue
                    symbol = _symbol(
                        repo,
                        path,
                        SymbolKind.cli_command,
                        f"{group}.{name}",
                        1,
                        name=name,
                        aliases=[target],
                        framework="entry-points",
                    )
                    symbol.parent_id = parent_id
                    result.append(symbol)
        return result


@register
class _RegisteredPythonCLIIndexer(PythonCLIIndexer):
    pass


@register
class _RegisteredPythonEntryPointIndexer(PythonEntryPointIndexer):
    pass
