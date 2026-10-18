"""AST recognizers for common argparse, Click and Typer declarations."""

import ast
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from docs_drift.index import register
from docs_drift.index.python_symbols import _literal, _module_name, _symbol
from docs_drift.models import Symbol, SymbolKind

MAX_SOURCE_BYTES = 1_000_000


def _call_name(node: ast.AST) -> str:
    return ast.unparse(node.func) if isinstance(node, ast.Call) else ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


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
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(path))
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
        if not target_names and not call_name.endswith("add_parser"):
            continue
        if call_name.endswith("ArgumentParser"):
            prog = _literal(_keyword(call, "prog"), source)
            for target in target_names:
                parser_vars[target] = None
                if prog:
                    # The executable is metadata for future console-script resolution, not a command itself.
                    command_ids[target] = prog
        elif call_name.endswith("add_parser") and call.args:
            parent_var = (
                call.func.value.id
                if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                else ""
            )
            parent_command = parser_vars.get(parent_var)
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
        elif call_name.endswith("add_subparsers"):
            for target in target_names:
                parser_vars[target] = (
                    parser_vars.get(call.func.value.id)
                    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    else None
                )
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "add_argument"
        ):
            continue
        owner = node.func.value
        parser_name = owner.id if isinstance(owner, ast.Name) else ""
        if parser_name not in parser_vars:
            continue
        options = [value for arg in node.args if (value := _literal(arg, source)) is not None]
        flags = [value for value in options if value.startswith("-")]
        if not flags:
            position = options[0] if options else "argument"
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.cli_argument,
                    f"{module}.{parser_vars[parser_name]}.{position}".strip("."),
                    node.lineno,
                    name=position,
                    framework="argparse",
                )
            )
            continue
        long_name = next((flag for flag in flags if flag.startswith("--")), flags[0])
        aliases = [flag for flag in flags if flag != long_name]
        dest = _literal(_keyword(node, "dest"), source)
        default = _literal(_keyword(node, "default"), source)
        action = _literal(_keyword(node, "action"), source)
        choices_node = _keyword(node, "choices")
        if choices_node is not None:
            with suppress(ValueError, TypeError):
                aliases.extend(str(item) for item in ast.literal_eval(choices_node))
        qualified = f"{module}.{parser_vars[parser_name] or 'root'}.{long_name}".strip(".")
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
    return result


def _decorator_cli(repo: Path, path: Path, source: str, tree: ast.Module) -> list[Symbol]:
    module = _module_name(repo, path)
    result: list[Symbol] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        command = None
        framework = None
        for decorator in node.decorator_list:
            name = ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator)
            if name.startswith("click.") or name.endswith(".command") or name.endswith(".group"):
                framework = "click"
            elif name.startswith("typer.") or name.endswith(".command"):
                framework = "typer" if "typer" in name else framework
            if framework and isinstance(decorator, ast.Call) and decorator.args:
                command = _literal(decorator.args[0], source)
        if framework:
            command_name = command or node.name.replace("_", "-")
            result.append(
                _symbol(
                    repo,
                    path,
                    SymbolKind.cli_command,
                    f"{module}.{command_name}".strip("."),
                    node.lineno,
                    name=command_name,
                    framework=framework,
                )
            )
        if framework != "typer":
            continue
        for argument, default in zip(node.args.args[::-1], (node.args.defaults or [])[::-1], strict=False):
            annotation = argument.annotation
            call = annotation if isinstance(annotation, ast.Call) else None
            if call is None or not ast.unparse(call.func).endswith(("Option", "Argument")):
                continue
            names = [
                value for item in call.args if (value := _literal(item, source)) is not None and value.startswith("-")
            ]
            flag = names[0] if names else "--" + argument.arg.replace("_", "-")
            kind = SymbolKind.cli_flag if ast.unparse(call.func).endswith("Option") else SymbolKind.cli_argument
            result.append(
                _symbol(
                    repo,
                    path,
                    kind,
                    f"{module}.{command or node.name}.{flag}".strip("."),
                    node.lineno,
                    name=flag,
                    aliases=names[1:],
                    default=_literal(default, source),
                    framework="typer",
                )
            )
    return result


@register
class _RegisteredPythonCLIIndexer(PythonCLIIndexer):
    pass
