"""Regression cases from the 12-repository sweep for historical content and default extraction."""

import hashlib
from pathlib import Path

from docs_drift.config import Config
from docs_drift.docs.defaults import extract_defaults
from docs_drift.docs.extract_blocks import extract_block_claims
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.pipeline import scan_claims
from docs_drift.docs.sections import ParsedDoc, is_historical_span, parse_document
from docs_drift.models import DocFile, DocFormat, SectionContext


def parsed(text: str, path: str = "README.md") -> ParsedDoc:
    fmt = DocFormat.rst if path.endswith(".rst") else DocFormat.markdown
    return parse_document(DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=fmt), text)


# Group 21: historical content.
def test_changes_and_news_files_are_historical() -> None:
    # pallets/click CHANGES.md:1343
    for name in ("CHANGES.md", "NEWS.md", "docs/changes.rst"):
        doc = parsed("# Version 7\n\n- Fixed `get_winter_size()`.\n", name)
        assert all(section.context == SectionContext.historical for section in doc.sections)


def test_rst_version_directives_are_historical_spans() -> None:
    # pallets/flask config.rst:419-423
    text = (
        "Config\n======\n\nThe ``SECRET_KEY`` value is current.\n\n"
        ".. versionadded:: 0.10\n   ``JSON_SORT_KEYS``, ``JSONIFY_PRETTYPRINT_REGULAR``\n\n"
        ".. versionchanged:: 1.0\n    ``LOGGER_HANDLER_POLICY`` was removed.\n\n"
        "After the notes, ``SESSION_COOKIE_NAME`` is current.\n"
    )
    doc = parsed(text, "docs/config.rst")
    section = doc.sections[0]
    lines = {line.strip(): number for number, line in enumerate(text.splitlines(), 1)}
    historical = [number for number in range(1, len(text.splitlines()) + 1) if is_historical_span(section, number)]
    assert lines["``JSON_SORT_KEYS``, ``JSONIFY_PRETTYPRINT_REGULAR``"] in historical
    assert lines["``LOGGER_HANDLER_POLICY`` was removed."] in historical
    assert lines["The ``SECRET_KEY`` value is current."] not in historical
    assert lines["After the notes, ``SESSION_COOKIE_NAME`` is current."] not in historical


def test_myst_version_fences_are_historical(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Options\n\n```{versionchanged} 8.0\nThe `--old-flag` option was replaced.\n```\n\nUse `--new-flag`.\n",
        encoding="utf-8",
    )
    result = scan_claims(tmp_path, Config())
    assert [claim.value for claim in result.claims] == ["--new-flag"]


# Group 22: default values.
def test_bullet_default_belongs_to_the_items_leading_option() -> None:
    # encode/uvicorn settings.md:149 ("**Default:** *2048*" after a cross-reference to --limit-concurrency)
    doc = parsed(
        "## Limits\n\n* `--backlog <int>` - Connections to hold, independent of `--limit-concurrency`. "
        "**Default:** *2048*.\n"
    )
    defaults = extract_defaults(doc, extract_claims(doc).claims).claims
    assert [(claim.value, claim.expected_default) for claim in defaults] == [("--backlog", "2048")]


def test_a_span_inside_the_stated_value_is_not_its_subject() -> None:
    # pallets/click documentation.md:329 ("The default is `--help` but can be overridden")
    doc = parsed("## Help\n\nThe default is `--help` but can be overridden.\n")
    assert extract_defaults(doc, extract_claims(doc).claims).claims == []


def test_explicit_port_in_an_example_command_is_not_a_default() -> None:
    # psf/black black_as_a_server.md:34 (`blackd --bind-port 9090 &` and `curl localhost:9090`)
    doc = parsed(
        '## Usage\n\n```sh\nblackd --bind-port 9090 &\ncurl -s -XPOST "localhost:9090" -d "print(\'valid\')"\n```\n'
    )
    claims = [*extract_claims(doc).claims, *extract_block_claims(doc, Path.cwd()).claims]
    assert extract_defaults(doc, claims).claims == []


def test_prose_port_default_still_links_to_its_only_port_option() -> None:
    doc = parsed("## Server\n\nThe `--port` option listens on port 8000 by default.\n")
    defaults = extract_defaults(doc, extract_claims(doc).claims).claims
    assert [(claim.value, claim.expected_default) for claim in defaults] == [("--port", "8000")]


def test_stated_values_are_read_whole_and_descriptions_are_skipped() -> None:
    # encode/uvicorn settings.md:41-138 (bullet defaults in emphasis, dotted numbers, prose descriptions)
    doc = parsed(
        "## Settings\n\n"
        "* `--host <str>` - Bind socket to this host. **Default:** *'127.0.0.1'*.\n"
        "* `--reload-delay <float>` - Delay between checks. **Default:** *0.25*.\n"
        "* `--ws-max-size <int>` - Max message size. **Default:** *16777216* (16 MB).\n"
        "* `--app-dir <path>` - Look in this directory. **Default:** *Current working directory*.\n"
        "* `--workers <int>` - Worker count. Defaults to the `$WEB_CONCURRENCY` environment variable if available.\n"
        "* `--ssl-ciphers <str>` - The ciphers to use. **Default:** OpenSSL's safe defaults.\n"
    )
    defaults = extract_defaults(doc, extract_claims(doc).claims).claims
    assert sorted((claim.value, claim.expected_default) for claim in defaults) == [
        ("--host", "127.0.0.1"),
        ("--reload-delay", "0.25"),
        ("--ws-max-size", "16777216"),
    ]
