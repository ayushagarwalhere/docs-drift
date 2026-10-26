"""Names that a document's own example code defines, so prose about them is not a project claim.

Tutorials declare a throwaway option (``@click.option('--yes')``), command group
(``cli_group='other'``) or route (``/users/page/<int:page>``) and then talk about it.
Those names are expected to be absent from the project's source tree. The example
code is only scanned with regular expressions; nothing is imported or executed.
"""

import re
import shlex
import textwrap
from dataclasses import dataclass

from docs_drift.docs.qualifiers import CONTEXT_PREFIX, context_qualifier
from docs_drift.docs.sections import ParsedDoc
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, DocFormat

_DECLARATION = re.compile(
    r"\b(?:option|Option|add_argument|argument|Argument)\(\s*"
    r"((?:[rbuf]?(?:'[^'\n]*'|\"[^\"\n]*\")\s*,\s*)*[rbuf]?(?:'[^'\n]*'|\"[^\"\n]*\"))"
)
_STRING = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)
_UPPER_ASSIGN = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=(?!=)", re.MULTILINE)
_NAMED = re.compile(
    r"\b(?:cli_group|name|create_function|create_aggregate|create_collation|command|group)\s*[=(]\s*"
    r"['\"]([A-Za-z_][\w-]*)['\"]"
)
_ROUTE = re.compile(r"\.(?:route|get|post|put|patch|delete|add_route|add_url_rule)\(\s*[rbuf]?['\"](/[^'\"]*)['\"]")
_ROUTE_PARAM = re.compile(r"<(?:[\w]+:)?\w+>|\{[^}/]+\}|:[A-Za-z_]\w*")
_PREFIX_MENTION = re.compile(r"auto_envvar_prefix")
# Sphinx/MyST directives that document a named configuration value or data attribute.
_DATA_DIRECTIVE = re.compile(
    r"^\s*(?:\.\.\s+|`{3,}\{|:{3,}\{)(?:py:)?(?:data|attribute|confval|setting)(?:::|\})\s+([A-Za-z_][\w.]*)",
    re.MULTILINE,
)
_GENERATED_ENV_WINDOW = 20


@dataclass(frozen=True)
class LocalDefinitions:
    flags: frozenset[str]
    names: frozenset[str]
    routes: tuple[re.Pattern[str], ...]
    prefix_lines: tuple[int, ...]


_PYTHON_LANGS = frozenset({"python", "py", "python3", "pycon", "ipython", "{eval-rst}", "eval-rst", "rst"})
_DATA_LANGS = frozenset({"env", "dotenv", "yaml", "yml", "toml", "json", "ini", "sh", "bash", "shell", "console"})
_PYTHON_SHAPE = re.compile(r"^\s*(?:def|class|import|from|@)\b", re.MULTILINE)


def _rst_literal_blocks(parsed: ParsedDoc) -> list[str]:
    """RST `paragraph::` literal blocks: the indented lines after a line ending in `::`."""
    blocks = []
    lines = parsed.lines
    for index, line in enumerate(lines):
        if not line.rstrip().endswith("::") or line.lstrip().startswith(".."):
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        blocks.append(textwrap.dedent("\n".join(body)))
    return blocks


def _code_blocks(parsed: ParsedDoc) -> list[str]:
    """Python example code only; env, YAML and shell blocks document the project's interface instead."""
    blocks = []
    candidates = [(token.info, token.content) for token in parsed.tokens if token.type == "fence" and token.content]
    if parsed.doc.format == DocFormat.rst:
        candidates.extend(("", block) for block in _rst_literal_blocks(parsed))
    for info, content in candidates:
        language = info.strip().casefold().split(maxsplit=1)[0] if info.strip() else ""
        if language in _PYTHON_LANGS or (language not in _DATA_LANGS and _PYTHON_SHAPE.search(content)):
            blocks.append(content)
    return blocks


def local_definitions(parsed: ParsedDoc) -> LocalDefinitions:
    """Collect option spellings, definitions, command names and routes from the doc's examples."""
    flags: set[str] = set()
    for match in _DECLARATION.finditer(parsed.text):
        for single, double in _STRING.findall(match.group(1)):
            for part in (single or double).split("/"):
                if part.strip().startswith("-"):
                    flags.add(part.strip())
    names: set[str] = set()
    routes: list[re.Pattern[str]] = []
    for code in _code_blocks(parsed):
        for pattern in (_DEF, _UPPER_ASSIGN, _NAMED):
            names.update(pattern.findall(code))
        for template in _ROUTE.findall(code):
            pieces = _ROUTE_PARAM.split(template)
            routes.append(re.compile("[^/]+".join(re.escape(piece) for piece in pieces) + "/?"))
    # Click derives command names from function names by replacing underscores with dashes.
    names.update(name.replace("_", "-") for name in list(names))
    prefix_lines = tuple(number for number, line in enumerate(parsed.lines, 1) if _PREFIX_MENTION.search(line))
    return LocalDefinitions(frozenset(flags), frozenset(names), tuple(routes), prefix_lines)


def _command_uses_local_name(value: str, definitions: LocalDefinitions) -> bool:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return False
    positional = [token for token in tokens[1:] if not token.startswith("-")]
    flags = [token.split("=", 1)[0] for token in tokens[1:] if token.startswith("-")]
    return bool(positional and positional[0] in definitions.names) or any(f in definitions.flags for f in flags)


def _is_local(claim: Claim, definitions: LocalDefinitions) -> str | None:  # noqa: PLR0911
    value = claim.value
    if claim.kind == ClaimKind.cli_flag:
        return "doc-example" if value in definitions.flags else None
    if claim.kind in {ClaimKind.cli_command, ClaimKind.config_key}:
        return "doc-example" if value in definitions.names else None
    if claim.kind == ClaimKind.symbol:
        name = value.removesuffix("()")
        return "doc-example" if name in definitions.names or name.rsplit(".", 1)[-1] in definitions.names else None
    if claim.kind == ClaimKind.code_command:
        return "doc-example" if _command_uses_local_name(value, definitions) else None
    if claim.kind == ClaimKind.env_var:
        if value in definitions.names:
            return "doc-example"
        if any(abs(claim.line - line) <= _GENERATED_ENV_WINDOW for line in definitions.prefix_lines):
            return "generated-env"
        return None
    if claim.kind == ClaimKind.endpoint:
        path = value.split(maxsplit=1)[-1].split("?", 1)[0]
        return "doc-example" if any(route.fullmatch(path) for route in definitions.routes) else None
    return None


def _as_config_key(parsed: ParsedDoc, claim: Claim) -> Claim:
    """`.. py:data:: TRAP_HTTP_EXCEPTIONS` documents a configuration value, not an environment variable."""
    section = parsed.line_to_section[claim.line]
    return claim.model_copy(
        update={
            "kind": ClaimKind.config_key,
            "claim_id": make_claim_id(parsed.doc.path, section.heading_path, ClaimKind.config_key, claim.value, 0),
        }
    )


def apply_local_definitions(parsed: ParsedDoc, claims: list[Claim]) -> list[Claim]:
    """Mark claims about names the document's own examples define as unverifiable, not missing."""
    definitions = local_definitions(parsed)
    data_names = frozenset(_DATA_DIRECTIVE.findall(parsed.text))
    if not (definitions.flags or definitions.names or definitions.routes or definitions.prefix_lines or data_names):
        return claims
    result = []
    for claim in claims:
        if claim.kind == ClaimKind.env_var and claim.value in data_names and claim.line in parsed.line_to_section:
            claim = _as_config_key(parsed, claim)  # noqa: PLW2901 - reclassified copy of the same mention.
        if claim.qualifier and claim.qualifier.startswith(CONTEXT_PREFIX):
            result.append(claim)
            continue
        reason = _is_local(claim, definitions)
        detail = "auto_envvar_prefix" if reason == "generated-env" else ""
        result.append(claim.model_copy(update={"qualifier": context_qualifier(reason, detail)}) if reason else claim)
    return result
