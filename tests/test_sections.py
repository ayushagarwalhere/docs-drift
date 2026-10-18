"""Inline examples exercise syntax and physical source locations without running doc commands."""

import hashlib

import pytest

from docs_drift.docs.sections import ParsedDoc, is_historical_span, parse_document
from docs_drift.models import DocFile, DocFormat, Section, SectionContext


def parse(text: str, path: str = "README.md", format_: DocFormat = DocFormat.markdown) -> ParsedDoc:
    return parse_document(
        DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=format_), text
    )


def test_nested_headings_and_partitioned_ranges() -> None:
    parsed = parse("# Usage\nIntro\n\n## Server options\nFlags\n### Advanced\nMore\n## Client\nEnd\n")
    assert [(s.heading_path, s.start_line, s.end_line) for s in parsed.sections] == [
        (["Usage"], 1, 3),
        (["Usage", "Server options"], 4, 5),
        (["Usage", "Server options", "Advanced"], 6, 7),
        (["Usage", "Client"], 8, 9),
    ]
    assert parsed.line_to_section[7].heading == "Advanced"
    assert parsed.line_to_section[8].heading == "Client"
    assert parsed.text.startswith("# Usage")


def test_setext_headings() -> None:
    parsed = parse("Usage\n=====\nBody\n\nServer\n------\nOptions\n")
    assert [(s.heading, s.heading_level, s.start_line, s.end_line) for s in parsed.sections] == [
        ("Usage", 1, 1, 4),
        ("Server", 2, 5, 7),
    ]
    assert parsed.sections[1].heading_path == ["Usage", "Server"]


def test_front_matter_preserves_line_numbers() -> None:
    text = '---\ntitle: "# Legacy"\nversion: v1\n---\n# Current\nBody\n'
    parsed = parse(text)
    assert [(s.heading, s.start_line, s.end_line) for s in parsed.sections] == [("Current", 5, 6)]
    assert parsed.sections[0].context == SectionContext.current
    assert 1 not in parsed.line_to_section
    assert parsed.tokens[0].map == [4, 5]
    assert parsed.text == text


def test_crlf_and_inline_formatting_preserve_heading_identity_and_history() -> None:
    parsed = parse("---\r\ntitle: Guide\r\n---\r\n# **Usage**\r\n`--port` was **renamed** to `--listen`.\r\n")
    section = parsed.sections[0]
    assert (section.heading, section.start_line, section.end_line) == ("Usage", 4, 5)
    assert section.section_id == parse("# Usage\nBody\n").sections[0].section_id
    assert section.historical_spans == [(5, 5)]


def test_indented_yaml_delimiter_is_not_a_front_matter_boundary() -> None:
    parsed = parse("---\nnotes: |\n  ---\n  # Legacy\n---\n# Current\n")
    assert [(section.heading, section.start_line) for section in parsed.sections] == [("Current", 6)]
    assert parsed.sections[0].context == SectionContext.current


def test_preamble_and_no_heading_document() -> None:
    parsed = parse("Intro text\n\n# Heading\nBody\n")
    assert [(s.heading, s.heading_path, s.start_line, s.end_line) for s in parsed.sections] == [
        ("(top)", [], 1, 2),
        ("Heading", ["Heading"], 3, 4),
    ]
    plain = parse("Just a paragraph.\n")
    assert plain.sections[0].heading_path == []
    assert plain.sections[0].heading_level == 0


def test_historical_context_inherits_and_stops_at_sibling() -> None:
    parsed = parse("# Guide\n## Migrating from v1\nOld\n### Flags\nOlder\n## Current\nNew\n")
    assert [s.context for s in parsed.sections] == ["current", "historical", "historical", "current"]
    assert is_historical_span(parsed.sections[2], 5)
    assert not is_historical_span(parsed.sections[2], 7)


@pytest.mark.parametrize(
    "path", ["CHANGELOG.md", "docs/HISTORY.markdown", "RELEASES.mdx", "UPGRADING.md", "MIGRATION.md"]
)
def test_historical_file_includes_preamble(path: str) -> None:
    parsed = parse("Intro\n\n# Latest\n## Fixed\nBody\n", path)
    assert all(section.context == SectionContext.historical for section in parsed.sections)
    assert all(is_historical_span(section, section.start_line) for section in parsed.sections)


@pytest.mark.parametrize(
    "heading",
    [
        "Changelog",
        "Release notes",
        "History",
        "Migration",
        "Upgrading from",
        "Deprecated",
        "Legacy",
        "Breaking changes",
        "What's new",
        "What\u2019s new",
        "v1",
        "1.x",
        "0.9.0",
    ],
)
def test_historical_heading_patterns(heading: str) -> None:
    assert parse(f"# {heading}\nOld\n").sections[0].context == SectionContext.historical


def test_inline_history_marks_only_its_paragraph() -> None:
    parsed = parse("# Options\n`--port` was renamed to `--listen` in 2.0.\n\nUse `--listen` now.\n")
    section = parsed.sections[0]
    assert section.context == SectionContext.current
    assert section.historical_spans == [(2, 2)]
    assert is_historical_span(section, 2)
    assert not is_historical_span(section, 4)
    assert Section.model_validate_json(section.model_dump_json()).historical_spans == [(2, 2)]


@pytest.mark.parametrize("marker", ["previously", "formerly", "no longer", "deprecated in", "removed in", "used to be"])
def test_wrapped_paragraph_history(marker: str) -> None:
    parsed = parse(f"# Options\nThis {marker} applied\nto old flags.\n\nCurrent paragraph.\n")
    assert parsed.sections[0].historical_spans == [(2, 3)]
    assert is_historical_span(parsed.sections[0], 3)
    assert not is_historical_span(parsed.sections[0], 5)


def test_code_fences_and_inline_code_do_not_create_history_or_headings() -> None:
    parsed = parse("# Current\n```sh\n# Legacy v1\necho previously\n```\n\nThe value is `previously`.\n")
    assert len(parsed.sections) == 1
    assert parsed.sections[0].historical_spans == []
    assert not is_historical_span(parsed.sections[0], 4)


def test_empty_and_front_matter_only_docs() -> None:
    assert parse("").sections == []
    assert parse("\n\n").line_to_section == {}
    assert parse("---\ntitle: Guide\n---\n").sections == []


def test_duplicate_headings_have_distinct_stable_ids() -> None:
    first = parse("# Usage\nA\n# Usage\nB\n")
    moved = parse("Intro\n\n# Usage\nA\n# Usage\nB\n")
    assert first.sections[0].section_id != first.sections[1].section_id
    assert [s.section_id for s in first.sections] == [s.section_id for s in moved.sections[1:]]


def test_mdx_is_parsed_without_executing_jsx() -> None:
    parsed = parse("# Usage\n<Component value={dangerous()} />\n", format_=DocFormat.mdx)
    assert parsed.sections[0].heading == "Usage"
    assert parsed.sections[0].end_line == 2


def test_basic_rst_and_plain_text() -> None:
    parsed = parse("Intro\n\nGuide\n=====\nBody\n\nLegacy\n------\nOld\n", "guide.rst", DocFormat.rst)
    assert [(s.heading, s.start_line, s.end_line) for s in parsed.sections] == [
        ("(top)", 1, 2),
        ("Guide", 3, 6),
        ("Legacy", 7, 9),
    ]
    assert parsed.sections[-1].context == SectionContext.historical
    overline = parse("=====\nGuide\n=====\nBody\n", "guide.rst", DocFormat.rst)
    assert overline.sections[0].start_line == 1
    plain = parse("# Not a heading\n\nFormerly a test.\n", "notes.txt", DocFormat.text)
    assert plain.sections[0].heading == "(top)"
    assert is_historical_span(plain.sections[0], 3)
