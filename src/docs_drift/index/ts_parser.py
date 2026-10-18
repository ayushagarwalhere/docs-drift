"""Load installed JS/TS grammar wheels without runtime downloads or temporary DLLs."""

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

_PARSERS: dict[str, Parser] = {}


def parser_for(language: str) -> Parser:
    """Reuse native parsers whose grammars ship with the installed dependencies."""
    selected = "typescript" if language == "typescript" else "javascript"
    if selected not in _PARSERS:
        capsule = (
            tree_sitter_typescript.language_typescript()
            if selected == "typescript"
            else tree_sitter_javascript.language()
        )
        _PARSERS[selected] = Parser(Language(capsule))
    return _PARSERS[selected]
