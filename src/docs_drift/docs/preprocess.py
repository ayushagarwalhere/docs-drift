"""Line-preserving MDX stripping and explicit reStructuredText claims."""

import re

from docs_drift.docs.sections import ParsedDoc
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction


def strip_mdx(text: str) -> str:
    lines, fence, importing = [], None, False
    for line in text.splitlines(keepends=True):
        marker = re.match(r"\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence else marker[1][0]
        if fence or marker:
            lines.append(line)
            continue
        if re.match(r"^\s*(import|export)\b", line) or importing:
            importing = not bool(re.search(r"(?:from\s+['\"].*['\"]|[;}])\s*;?\s*$", line))
            lines.append("\n" if line.endswith("\n") else "")
        else:
            # Strip wrappers, retaining child code fences and inline Markdown.
            lines.append(re.sub(r"</?[A-Z][\w.]*(?:\s[^>]*?)?/?>", lambda m: " " * len(m[0]), line))
    return "".join(lines)


def _rst_flag(value: str) -> str | None:
    """Return the option name of an RST flag literal, dropping `=value`, ` value` and TAB markers."""
    value = re.sub(r"\s*\[TAB\]", "", value).strip()
    if not re.match(r"^--?[A-Za-z]", value):
        return None  # A lone '-' (stdin, "dashes") or a negative number is not an option.
    return re.split(r"[\s=]", value, maxsplit=1)[0]


def rst_claims(parsed: ParsedDoc, *, project_names: frozenset[str] = frozenset()) -> list[Claim]:
    from docs_drift.docs.extract_inline import (  # noqa: PLC0415 - shared lexical rules.
        _classify,
        _context_qualifier,
        _explicit_api_reference,
        _flag_pair,
        _list_lead_in,
        _paragraph_sentence,
        path_context,
        preceding_text,
    )

    claims = []
    counts: dict[tuple[str, str, str], int] = {}
    lines = parsed.text.splitlines()
    # Paragraph extents let a wrapped RST sentence ("Uvicorn provides the ``--ssl-keyfile`` ...") keep its context.
    paragraphs = {
        number: (token.map[0] + 1, token.map[1])
        for token in parsed.tokens
        if token.type == "paragraph_open" and token.map is not None
        for number in range(token.map[0] + 1, token.map[1] + 1)
    }
    for line_no, line in enumerate(lines, 1):
        for match in re.finditer(r":(option|envvar):`([^`]+)`|``([^`]+)``", line):
            value = match[2] or match[3]
            kind: ClaimKind | None = None
            if match[1] == "envvar":
                kind = ClaimKind.env_var
            elif match[1] == "option" or value.lstrip().startswith("-"):
                # ``-c/--config`` names one option; the long spelling is the one the index declares.
                flag = _flag_pair(value.strip()) or _rst_flag(value)
                kind, value = (ClaimKind.cli_flag, flag) if flag else (None, value)
            elif re.search(r"\bdefault\s*:\s*$", line[: match.start()], re.IGNORECASE):
                continue  # "Default: ``timedelta(days=31)``" states a value, not an API name.
            else:
                # Share explicit API disambiguation without broadening unrelated
                # RST literals into Markdown-only configuration/prose heuristics.
                classified = _classify(value)
                if _explicit_api_reference(value, line):
                    kind = ClaimKind.symbol
                elif classified:
                    kind = classified[0]
                    value = classified[1]
            section = parsed.line_to_section.get(line_no)
            if kind is None or section is None:
                continue
            start, end = paragraphs.get(line_no, (line_no, line_no))
            sentence = _paragraph_sentence(lines, start, end, line_no, match.start() + 1)
            qualifier = (
                (path_context(sentence, value, project_names) or path_context(line, value, project_names))
                if kind == ClaimKind.file_path
                else _context_qualifier(
                    kind,
                    value,
                    sentence=sentence,
                    paragraph=preceding_text(lines, start, line_no, match.start() + 1),
                    lead_in=_list_lead_in(lines, line_no),
                    link_host=None,
                    project_names=project_names,
                    heading=" / ".join(section.heading_path),
                )
            )
            key = (section.section_id, kind.value, value)
            occurrence = counts.get(key, 0)
            counts[key] = occurrence + 1
            claims.append(
                Claim(
                    claim_id=make_claim_id(parsed.doc.path, section.heading_path, kind.value, value, occurrence),
                    kind=kind,
                    raw_text=line,
                    value=value,
                    qualifier=qualifier,
                    doc_path=parsed.doc.path,
                    line=line_no,
                    column=match.start(2 if match[2] else 3) + 1,
                    section_id=section.section_id,
                    extraction=Extraction.inline_code,
                    extraction_confidence=0.99 if match[1] else 0.9,
                )
            )
    return claims
