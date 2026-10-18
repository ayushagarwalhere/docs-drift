"""Shared tree-sitter parsers with a stable cache location for Windows DLL loading."""

import os
from pathlib import Path

from tree_sitter import Parser
from tree_sitter_language_pack import PackConfig, configure, get_parser

_PARSERS: dict[str, Parser] = {}


def parser_for(language: str) -> Parser:
    """Reuse parsers and keep native grammar DLLs outside disposable fixture trees."""
    selected = "typescript" if language == "typescript" else "javascript"
    if selected not in _PARSERS:
        cache = Path(os.environ.get("DOCS_DRIFT_TS_CACHE", Path.cwd() / ".docs-drift" / "cache" / "tree-sitter"))
        configure(PackConfig(cache_dir=str(cache)))
        _PARSERS[selected] = get_parser(selected)
    return _PARSERS[selected]
