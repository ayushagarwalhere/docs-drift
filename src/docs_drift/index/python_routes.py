"""Static route decorators for common Python web frameworks."""

import ast
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
        return result


@register
class _RegisteredPythonRouteIndexer(PythonRouteIndexer):
    pass
