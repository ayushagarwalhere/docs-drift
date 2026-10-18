"""Tree-sitter-validated static route extraction for Express-style JS/TS sources."""

import re
from collections.abc import Sequence
from pathlib import Path

from docs_drift.index import register
from docs_drift.index.python_symbols import _symbol
from docs_drift.index.routes_common import normalize_route
from docs_drift.index.ts_parser import parser_for
from docs_drift.models import Symbol, SymbolKind


class JavaScriptRouteIndexer:
    name = "javascript-routes"
    languages = frozenset({"javascript", "typescript"})
    batch_files = True

    def applies_to(self, repo: Path) -> bool:
        return True

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]:
        result = []
        for path in files:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                parser = parser_for(
                    "typescript" if path.suffix.casefold() in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
                )
                tree = parser.parse(source.encode("utf-8"))
            except Exception:
                continue
            if tree.root_node.has_error:
                continue
            framework = (
                "express"
                if re.search(r"\bexpress\s*\(", source) or "express" in source
                else "fastify"
                if "fastify(" in source
                else ""
            )
            if not framework:
                continue
            for match in re.finditer(
                r"\b(app|router|server)\.(get|post|put|patch|delete|all)\s*\(\s*(['\"])(.*?)\3", source, re.S
            ):
                method, raw = match.group(2).upper(), match.group(4)
                method = "ANY" if method == "ALL" else method
                norm_method, norm_path = normalize_route(method, raw)
                line = source.count("\n", 0, match.start()) + 1
                symbol = _symbol(
                    repo,
                    path,
                    SymbolKind.route,
                    f"{norm_method.upper()} {norm_path}",
                    line,
                    name=f"{norm_method.upper()} {norm_path}",
                    framework=framework,
                    language="typescript" if path.suffix in {".ts", ".tsx"} else "javascript",
                )
                symbol.raw_path = raw
                result.append(symbol)
        return result


@register
class _RegisteredJavaScriptRouteIndexer(JavaScriptRouteIndexer):
    pass
