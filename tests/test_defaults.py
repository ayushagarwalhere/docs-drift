"""Default normalization and subject-linking behavior."""

import hashlib

import pytest

from docs_drift.docs.defaults import NormalizedValue, extract_defaults, normalize_value
from docs_drift.docs.extract_blocks import extract_block_claims
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.sections import ParsedDoc, parse_document
from docs_drift.models import DocFile, DocFormat


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("30", "30.0", True),
        ('"30"', "30", True),
        ("-2", "-2.0", True),
        ("0", "0.0", True),
        ("30s", "30 seconds", True),
        ("30 sec", "30s", True),
        ("0.5m", "30s", True),
        ("1h", "3600 seconds", True),
        ("1MB", "1000000B", True),
        ("1 MiB", "1048576B", True),
        ("1MB", "1MiB", False),
        ("true", "yes", True),
        ("on", "enabled", True),
        ("1", "true", True),
        ("false", "no", True),
        ("off", "disabled", True),
        ("0", "false", True),
        ("'value'", '"value"', True),
        ("INFO", "info", True),
        ("INFO", "info", True),
        ("Info", "info", True),
        ("none", "null", True),
        ("unset", "empty", True),
        ('""', "none", True),
        ("~/app/", "~/app", True),
        ("/etc/app///", "/etc/app", True),
        ("./config/", "./config", True),
        ("30", "30s", None),
        ("1048576", "1MiB", None),
        ("1MB", "1MiB", False),
        ("alpha", "Alpha", False),
        ("1.25", "1.250", True),
        ("2m", "120 sec", True),
        ("1KiB", "1024B", True),
        ("1KB", "1KiB", False),
    ],
)
def test_normalized_values_compare(left: str, right: str, equal: bool | None) -> None:
    first = normalize_value(left)
    second = normalize_value(right)
    assert first.equals(second) is equal


def test_normalized_value_retains_kind_unit_and_raw_form() -> None:
    value = normalize_value("0.5m")
    assert value == NormalizedValue("duration", "30", "s", "0.5m")


def parse(text: str, path: str = "README.md") -> ParsedDoc:
    doc = DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    return parse_document(doc, text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The `--timeout` flag defaults to 30.", ("--timeout", "30")),
        ("The `--level` default is `info`.", ("--level", "info")),
        ("The `--workers` option (default: 4).", ("--workers", "4")),
        ("Pass `--workers` (default 4).", ("--workers", "4")),
        ("By default, the server listens on port 8000 using `--port`.", ("--port", "8000")),
        ("If unset, uses `~/.config/app` for `APP_CONFIG_PATH`.", ("APP_CONFIG_PATH", "~/.config/app")),
        ("The `request_timeout` field defaults to 30 seconds.", ("request_timeout", "30s")),
        ("Timeout (seconds, default 30) uses config key `timeout_seconds`.", ("timeout_seconds", "30")),
        ("Set `LOG_LEVEL` default is `INFO`.", ("LOG_LEVEL", "info")),
        ("`--cache` defaults to none.", ("--cache", "none")),
        ("Use `--debug` (default: off).", ("--debug", "false")),
        ("The `APP_PORT` default is 8080.", ("APP_PORT", "8080")),
        ("The config key `server_timeout` defaults to 30.", ("server_timeout", "30")),
        ("The `--size` default is 1MiB.", ("--size", "1048576B")),
        ("The `--root` default is `./config/`.", ("--root", "./config")),
    ],
)
def test_prose_default_extraction_cases(text: str, expected: tuple[str, str]) -> None:
    parsed = parse(text)
    source_claims = extract_claims(parsed).claims
    result = extract_defaults(parsed, source_claims)
    assert [(claim.value, claim.expected_default) for claim in result.claims] == [expected]


def test_two_equally_close_subjects_are_skipped_with_debug_note() -> None:
    parsed = parse("`--port` and `APP_PORT` default is 8000.")
    claims = extract_claims(parsed).claims
    subjects = [claim for claim in claims if claim.value in {"--port", "APP_PORT"}]
    claims = [claim.model_copy(update={"column": 10}) if claim in subjects else claim for claim in claims]
    result = extract_defaults(parsed, claims)
    assert result.claims == []
    assert "equally near" in result.debug_notes[0]


def test_table_default_cell_is_attached_to_subject() -> None:
    parsed = parse("| Option | Default |\n| --- | --- |\n| `--workers` | `4` |\n")
    result = extract_defaults(parsed, extract_claims(parsed).claims)
    assert [(claim.value, claim.qualifier, claim.expected_default) for claim in result.claims] == [
        ("--workers", "cli_flag", "4")
    ]


def test_help_text_default_is_found_in_fence() -> None:
    parsed = parse("```console\n--workers INT Number of workers [default: 4]\n```\n")
    claims = [*extract_claims(parsed).claims, *extract_block_claims(parsed, __import__("pathlib").Path.cwd()).claims]
    result = extract_defaults(parsed, claims)
    assert [(claim.value, claim.expected_default) for claim in result.claims] == [("--workers", "4")]


def test_port_default_requires_exactly_one_port_subject() -> None:
    parsed = parse("`--port` listens on port 8000.")
    assert [claim.expected_default for claim in extract_defaults(parsed, extract_claims(parsed).claims).claims] == [
        "8000"
    ]
    ambiguous = parse("`--port` and `APP_PORT` listen on port 8000.")
    result = extract_defaults(ambiguous, extract_claims(ambiguous).claims)
    assert result.claims == []
    assert result.debug_notes


def test_none_equals_returns_boolean_not_unverifiable() -> None:
    assert normalize_value("none").equals(normalize_value("null")) is True
