"""Parse documentation locations and historical context without executing embedded content."""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from markdown_it import MarkdownIt
from markdown_it.token import Token

from docs_drift.ids import make_section_id
from docs_drift.models import DocFile, DocFormat, Section, SectionContext

MIN_ADORNMENT_LENGTH = 3


@dataclass(frozen=True)
class ParsedDoc:
    doc: DocFile
    sections: list[Section]
    tokens: list[Token]
    line_to_section: dict[int, Section]
    text: str


def _historical_heading(heading: str) -> bool:
    words = (
        r"\b(?:change[- ]?log|release notes|history|migrat\w*|upgrad\w*|deprecat\w*|legacy|"
        r"breaking changes|what['\u2019]s new)\b"
    )
    versions = r"\bv\d+(?:\.\d+)*(?:\.x)?\b|\b\d+(?:\.\d+)*\.x\b|\b\d+\.\d+\.\d+\b"
    return re.search(f"{words}|{versions}", heading, re.IGNORECASE) is not None


def _historical_file(path: str) -> bool:
    return (
        re.match(
            r"^(?:change[-_ ]?log|history|releases?|upgrading|migrations?)(?:[._ -]|$)",
            PurePosixPath(path).stem,
            re.IGNORECASE,
        )
        is not None
    )


def _historical_paragraph(text: str) -> bool:
    return (
        re.search(
            r"\b(?:previously|formerly|was\s+renamed\s+to|no\s+longer|deprecated\s+in|removed\s+in|used\s+to\s+be)\b",
            text,
            re.IGNORECASE,
        )
        is not None
    )


def _skip_front_matter(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip().removeprefix("\ufeff") == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.rstrip() == "---":
                return "\n" * (index + 1) + "".join(lines[index + 1 :]), index + 1
    return text, 0


def _inline_text(token: Token, *, include_code: bool = True) -> str:
    if token.children is None:
        return token.content
    return "".join(
        child.content
        if child.type in {"text", "image"} or (include_code and child.type == "code_inline")
        else (" " if child.type in {"softbreak", "hardbreak", "code_inline"} else "")
        for child in token.children
    )


@dataclass(frozen=True)
class _Heading:
    text: str
    level: int
    line: int


def _headings(tokens: list[Token]) -> list[_Heading]:
    return [
        _Heading(_inline_text(tokens[index + 1]), int(token.tag[1:]), token.map[0] + 1)
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.map is not None
    ]


def _sections(doc: DocFile, text: str, tokens: list[Token], skipped: int) -> list[Section]:
    headings = _headings(tokens)
    lines = text.splitlines()
    first_heading = headings[0].line - 1 if headings else len(lines)
    if any(line.strip() for line in lines[skipped:first_heading]):
        headings.insert(0, _Heading("(top)", 0, skipped + 1))
    stack: list[_Heading] = []
    occurrences: Counter[tuple[str, ...]] = Counter()
    sections = []
    for index, heading in enumerate(headings):
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        trail = [parent.text for parent in stack if parent.level] + ([heading.text] if heading.level else [])
        occurrence = occurrences[tuple(trail)]
        occurrences[tuple(trail)] += 1
        context = (
            SectionContext.historical
            if _historical_file(doc.path) or any(map(_historical_heading, trail))
            else SectionContext.current
        )
        sections.append(
            Section(
                section_id=make_section_id(doc.path, trail, occurrence),
                doc_path=doc.path,
                heading=heading.text,
                heading_level=heading.level,
                heading_path=trail,
                start_line=heading.line,
                end_line=headings[index + 1].line - 1 if index + 1 < len(headings) else len(lines),
                context=context,
            )
        )
        stack.append(heading)
    return sections


def _adornment(line: str) -> str | None:
    text = line.strip()
    return text[0] if len(text) >= MIN_ADORNMENT_LENGTH and text[0] in '=-~^"+#*' and len(set(text)) == 1 else None


def _rst_heading(lines: list[str], index: int) -> tuple[str, str, int] | None:
    if index + 2 < len(lines) and _adornment(lines[index]) and _adornment(lines[index]) == _adornment(lines[index + 2]):
        return lines[index + 1].strip(), "over:" + lines[index].strip()[0], 3
    if (
        index + 1 < len(lines)
        and lines[index].strip()
        and _adornment(lines[index + 1])
        and len(lines[index + 1].strip()) >= len(lines[index].strip())
    ):
        return lines[index].strip(), "under:" + lines[index + 1].strip()[0], 2
    return None


def _simple_tokens(text: str, *, rst: bool) -> list[Token]:
    lines = text.splitlines()
    tokens: list[Token] = []
    levels: dict[str, int] = {}
    index = 0
    while index < len(lines):
        heading = _rst_heading(lines, index) if rst else None
        if heading is not None:
            title, adornment, count = heading
            level = levels.setdefault(adornment, len(levels) + 1)
            tokens.extend(
                [
                    Token("heading_open", f"h{level}", 1, map=[index, index + count]),
                    Token("inline", "", 0, content=title, map=[index, index + count]),
                    Token("heading_close", f"h{level}", -1),
                ]
            )
            index += count
        elif lines[index].strip():
            start = index
            index += 1
            while index < len(lines) and lines[index].strip() and not (rst and _rst_heading(lines, index)):
                index += 1
            tokens.extend(
                [
                    Token("paragraph_open", "p", 1, map=[start, index]),
                    Token("inline", "", 0, content="\n".join(lines[start:index]), map=[start, index]),
                    Token("paragraph_close", "p", -1),
                ]
            )
        else:
            index += 1
    return tokens


def _mark_paragraphs(tokens: list[Token], mapping: dict[int, Section]) -> None:
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open" or token.map is None:
            continue
        if _historical_paragraph(_inline_text(tokens[index + 1], include_code=False)):
            first, end = token.map[0] + 1, token.map[1]
            section = mapping.get(first)
            if section is not None:
                section.historical_spans.append((first, end))


def is_historical_span(section: Section, line: int) -> bool:
    """Paragraph markers suppress their full paragraph, never the rest of a current section."""
    return section.start_line <= line <= section.end_line and (
        section.context == SectionContext.historical
        or any(start <= line <= end for start, end in section.historical_spans)
    )


def parse_document(doc: DocFile, text: str) -> ParsedDoc:
    """Partition physical lines at every heading; map lines to their most specific section."""
    if doc.format in {DocFormat.markdown, DocFormat.mdx}:
        source, skipped = _skip_front_matter(text)
        tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(source)
    else:
        source, skipped = text, 0
        tokens = _simple_tokens(source, rst=doc.format == DocFormat.rst)
    sections = _sections(doc, source, tokens, skipped)
    mapping = {line: section for section in sections for line in range(section.start_line, section.end_line + 1)}
    _mark_paragraphs(tokens, mapping)
    return ParsedDoc(doc, sections, tokens, mapping, text)
