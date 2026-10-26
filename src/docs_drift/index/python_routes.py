"""Static route decorators for common Python web frameworks."""

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from docs_drift.index import register
from docs_drift.index.python_source import read_python
from docs_drift.index.python_symbols import _literal, _module_name, _symbol
from docs_drift.index.routes_common import normalize_route
from docs_drift.models import Symbol, SymbolKind


class PythonRouteIndexer:
    name = "python-routes"
    languages = frozenset({"python"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:  # noqa: PLR0912
        result = []
        for path in files:
            try:
                source, tree = read_python(path)
            except (OSError, SyntaxError, ValueError):
                continue
            module = _module_name(repo, path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                        continue
                    framework = (
                        "fastapi"
                        if decorator.func.attr
                        in {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
                        else "flask"
                        if decorator.func.attr in {"route"}
                        else None
                    )
                    if framework is None or not decorator.args:
                        continue
                    raw = _literal(decorator.args[0], source)
                    if not raw:
                        continue
                    methods_node = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
                    methods = [decorator.func.attr.upper()] if decorator.func.attr != "route" else ["GET"]
                    if methods_node is not None:
                        try:
                            methods = [str(item).upper() for item in ast.literal_eval(methods_node)]
                        except (ValueError, TypeError):
                            methods = ["ANY"]
                    prefix = ""
                    for statement in tree.body:
                        router_var = decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else ""
                        if (
                            isinstance(statement, ast.Assign)
                            and isinstance(statement.value, ast.Call)
                            and isinstance(statement.value.func, ast.Name)
                            and ast.unparse(statement.value.func).endswith(("APIRouter", "Blueprint"))
                            and any(
                                isinstance(target, ast.Name) and target.id == router_var for target in statement.targets
                            )
                        ):
                            prefix = (
                                _literal(
                                    next(
                                        (
                                            kw.value
                                            for kw in statement.value.keywords
                                            if kw.arg in {"prefix", "url_prefix"}
                                        ),
                                        None,
                                    ),
                                    source,
                                )
                                or ""
                            )
                    path_value = prefix.rstrip("/") + "/" + raw.lstrip("/")
                    for method in methods:
                        normalized_method, normalized_path = normalize_route(method, path_value)
                        symbol = _symbol(
                            repo,
                            path,
                            SymbolKind.route,
                            f"{normalized_method.upper()} {normalized_path}",
                            node.lineno,
                            name=f"{normalized_method.upper()} {normalized_path}",
                            framework=framework,
                        )
                        symbol.raw_path = path_value
                        symbol.handler = f"{module}.{node.name}".strip(".")
                        result.append(symbol)
            result.extend(_call_routes(repo, path, source, tree, module))
        return result


# Calls that register a route without a decorator: ``add_route(view, r"^/(?P<db>[^/]+)$")`` (datasette, aiohttp,
# Starlette), ``Route("/x", endpoint)`` (Starlette), ``add_url_rule`` (Flask) and ``re_path`` (Django).
_ROUTE_CALLS = frozenset({"add_route", "add_api_route", "add_url_rule", "add_websocket_route", "Route", "re_path"})
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY", "*"})
_AIOHTTP_DEFINITIONS = frozenset(
    f"web.{name}" for name in ("get", "post", "put", "patch", "delete", "head", "options", "route", "view")
)
# ``(\.(?P<format>\w+))?`` and similar optional extension suffixes: the path is served with or without them.
_OPTIONAL_SUFFIX = re.compile(r"\((?:\\\.|\[\.\])\(\?P<\w+>[^()]*\)\)\?")
_NAMED_GROUP = re.compile(r"\(\?P<(\w+)>(?:[^()\\]|\\.|\([^()]*\))*\)")


def regex_template(raw: str) -> str:
    """Turn a route regex into a ``{param}`` template; unparsed regex syntax is left in place."""
    value = raw.removeprefix("^").removesuffix("$").removesuffix(r"\Z")
    value = _OPTIONAL_SUFFIX.sub("", value)
    value = _NAMED_GROUP.sub(lambda match: "{" + match.group(1) + "}", value)
    value = re.sub(r"\\([./-])", r"\1", value)
    return value if value.startswith("/") else "/" + value


def _route_path(call: ast.Call, source: str) -> tuple[str | None, str | None]:
    strings = [
        (index, value)
        for index, item in enumerate(call.args)
        if (value := _literal(item, source)) is not None and isinstance(item, ast.Constant)
    ]
    path_node = next((kw.value for kw in call.keywords if kw.arg in {"path", "rule", "route"}), None)
    method = next((value for _, value in strings if value.upper() in _HTTP_METHODS), None)
    path_value = _literal(path_node, source) if path_node is not None else None
    if path_value is None:
        path_value = next((value for _, value in strings if value.startswith(("/", "^"))), None)
    return method, path_value


def _call_routes(repo: Path, path: Path, source: str, tree: ast.Module, module: str) -> list[Symbol]:
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func).rsplit(".", 1)[-1]
        # aiohttp route definitions: ``app.add_routes([web.post("/", handler)])``.
        aiohttp = ast.unparse(node.func) in _AIOHTTP_DEFINITIONS
        if name not in _ROUTE_CALLS and not aiohttp:
            continue
        method, raw = _route_path(node, source)
        if aiohttp and name.upper() in _HTTP_METHODS:
            method = name
        if not raw:
            continue
        template = regex_template(raw) if name in {"add_route", "re_path"} or raw.startswith("^") else raw
        methods_node = next((kw.value for kw in node.keywords if kw.arg == "methods"), None)
        methods = [method.upper().replace("*", "ANY")] if method else ["ANY"]
        if methods_node is not None:
            try:
                methods = [str(item).upper() for item in ast.literal_eval(methods_node)]
            except (ValueError, TypeError, SyntaxError):
                methods = ["ANY"]
        for http_method in methods:
            normalized_method, normalized_path = normalize_route(http_method, template)
            symbol = _symbol(
                repo,
                path,
                SymbolKind.route,
                f"{normalized_method.upper()} {normalized_path}",
                node.lineno,
                name=f"{normalized_method.upper()} {normalized_path}",
                framework=name,
            )
            # The original regex lets a concrete documented URL be checked by a full match.
            symbol.raw_path = raw
            result.append(symbol)
    return result


@register
class _RegisteredPythonRouteIndexer(PythonRouteIndexer):
    pass
