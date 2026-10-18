"""Precision cases for inline, table, and explicit prose claim extraction."""

import hashlib

import pytest

from docs_drift.docs.extract_inline import ExtractionResult, extract_claims
from docs_drift.docs.sections import parse_document
from docs_drift.models import ClaimKind, DocFile, DocFormat


def extract(text: str, *, path: str = "README.md") -> ExtractionResult:
    doc = DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    return extract_claims(parse_document(doc, text))


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("--port", (ClaimKind.cli_flag, "--port")),
        ("--port=8080", (ClaimKind.cli_flag, "--port")),
        ("-p", (ClaimKind.cli_flag, "-p")),
        ("-x", (ClaimKind.cli_flag, "-x")),
        ("--dry-run", (ClaimKind.cli_flag, "--dry-run")),
        ("--workers", (ClaimKind.cli_flag, "--workers")),
        ("APP_DEBUG", (ClaimKind.env_var, "APP_DEBUG")),
        ("API_URL", (ClaimKind.env_var, "API_URL")),
        ("TOKEN", (ClaimKind.env_var, "TOKEN")),
        ("HTTP", None),
        ("JSON", None),
        ("API", None),
        ("README", None),
        ("TODO", None),
        ("GET", None),
        ("POST", None),
        ("NOTE", None),
        ("MIT", None),
        ("process.env.API_URL", (ClaimKind.env_var, "API_URL")),
        ('os.environ["APP_DEBUG"]', (ClaimKind.env_var, "APP_DEBUG")),
        ('os.getenv("APP_PORT")', (ClaimKind.env_var, "APP_PORT")),
        ("$APP_DEBUG", (ClaimKind.env_var, "APP_DEBUG")),
        ("${APP_DEBUG}", (ClaimKind.env_var, "APP_DEBUG")),
        ("GET /users/{id}", (ClaimKind.endpoint, "GET /users/{param}")),
        ("POST /login", (ClaimKind.endpoint, "POST /login")),
        ("/api/health", (ClaimKind.endpoint, "/api/health")),
        ("GET /users/:id", (ClaimKind.endpoint, "GET /users/{param}")),
        ("GET /users/<id>", (ClaimKind.endpoint, "GET /users/{param}")),
        ("create_app()", (ClaimKind.symbol, "create_app")),
        ("client.users.list", (ClaimKind.symbol, "client.users.list")),
        ("FastAPIApp", None),
        ("request_timeout", None),
        ("src/config.py", (ClaimKind.file_path, "src/config.py")),
        ("docs/setup.md", (ClaimKind.file_path, "docs/setup.md")),
        (".env.example", (ClaimKind.file_path, ".env.example")),
        ("https://example.com/docs/setup.md", None),
        ("npm run build:prod", (ClaimKind.package_script, "build:prod")),
        ("pnpm test", (ClaimKind.package_script, "test")),
        ("yarn lint", (ClaimKind.package_script, "lint")),
        ("make test", (ClaimKind.make_target, "test")),
        ("just check", (ClaimKind.make_target, "check")),
        ("uv run pytest", (ClaimKind.package_script, "pytest")),
        ("poetry run docs-drift", (ClaimKind.package_script, "docs-drift")),
        ("npx node-tool deploy --dry-run", (ClaimKind.code_command, "npx node-tool deploy --dry-run")),
        ("git log --oneline", None),
        ("pip install -e .", None),
        ("--name alice", None),
    ],
)
def test_inline_shape_cases(span: str, expected: tuple[ClaimKind, str] | None) -> None:
    result = extract(f"Use `{span}` here.")
    if expected is None:
        assert result.claims == []
    else:
        assert [(claim.kind, claim.value) for claim in result.claims] == [expected]


def test_table_header_selects_kind_and_attaches_default() -> None:
    result = extract("| Option | Default | Description |\n| --- | --- | --- |\n| `--port` | `8080` | Listen port |\n")
    flag = next(claim for claim in result.claims if claim.kind == ClaimKind.cli_flag)
    assert flag.value == "--port"
    assert flag.expected_default == "8080"
    assert flag.extraction.value == "table"


@pytest.mark.parametrize(
    ("header", "cell", "kind", "value"),
    [
        ("Flag", "--port", ClaimKind.cli_flag, "--port"),
        ("Option", "--host", ClaimKind.cli_flag, "--host"),
        ("Argument", "--workers", ClaimKind.cli_flag, "--workers"),
        ("Environment variable", "APP_DEBUG", ClaimKind.env_var, "APP_DEBUG"),
        ("Env", "API_URL", ClaimKind.env_var, "API_URL"),
        ("Endpoint", "GET /users/{id}", ClaimKind.endpoint, "GET /users/{param}"),
        ("Setting", "request_timeout", ClaimKind.config_key, "request_timeout"),
        ("Key", "server.port", ClaimKind.config_key, "server.port"),
    ],
)
def test_table_kind_cases(header: str, cell: str, kind: ClaimKind, value: str) -> None:
    result = extract(f"| {header} | Description |\n| --- | --- |\n| `{cell}` | current |\n")
    assert [(claim.kind, claim.value) for claim in result.claims] == [(kind, value)]


def test_explicit_default_prose_links_to_nearest_preceding_claim() -> None:
    result = extract("The `--workers` option defaults to `4`.")
    defaults = [claim for claim in result.claims if claim.kind == ClaimKind.default_value]
    assert len(defaults) == 1
    assert defaults[0].value == "4"
    assert defaults[0].qualifier == "--workers"


@pytest.mark.parametrize("text", ["Pass `--port` (default 8000).", "Pass `--port`; default is `8000`."])
def test_parenthetical_and_is_defaults_are_extracted(text: str) -> None:
    defaults = [claim for claim in extract(text).claims if claim.kind == ClaimKind.default_value]
    assert [claim.value for claim in defaults] == ["8000"]


def test_defaults_link_to_the_nearest_claim_in_their_sentence() -> None:
    result = extract("`--port` default is `8000`. `--workers` defaults to `4`.")
    defaults = [claim for claim in result.claims if claim.kind == ClaimKind.default_value]
    assert [(claim.value, claim.qualifier) for claim in defaults] == [("8000", "--port"), ("4", "--workers")]


def test_set_value_keeps_name_and_example_separate() -> None:
    result = extract("Set `APP_ENV=production` to choose the deployment mode.")
    assert [(claim.kind, claim.value) for claim in result.claims] == [
        (ClaimKind.env_var, "APP_ENV"),
        (ClaimKind.default_value, "production"),
    ]


def test_set_setting_to_value_marks_value_as_example() -> None:
    result = extract("Set `APP_ENV` to `production` before startup.")
    default = next(claim for claim in result.claims if claim.kind == ClaimKind.default_value)
    assert (default.value, default.qualifier) == ("production", "APP_ENV")


def test_historical_inline_and_prose_spans_are_counted_and_skipped() -> None:
    result = extract("## Current\nThe `--port` flag was renamed to `--listen` in 2.0.\n\nUse `--host` today.\n")
    assert [(claim.kind, claim.value) for claim in result.claims] == [(ClaimKind.cli_flag, "--host")]
    assert result.historical_skipped == 2


def test_historical_heading_skips_spans() -> None:
    result = extract("## Migrating from v1\nThe old option was `--port`.\n")
    assert result.claims == []
    assert result.historical_skipped == 1


def test_qualifier_comes_from_subcommand_sentence_or_heading() -> None:
    sentence = extract("The `serve` command accepts `--port`.")
    heading = extract("### serve\nPass `--port` to start the server.\n")
    assert sentence.claims[-1].qualifier == "serve"
    assert heading.claims[0].qualifier == "serve"


def test_api_reference_heading_accepts_identifier_symbol() -> None:
    result = extract("## API reference\nUse `create_app` to create the service.\n")
    assert [(claim.kind, claim.value) for claim in result.claims] == [(ClaimKind.symbol, "create_app")]


def test_identifier_is_symbol_when_sentence_names_its_role() -> None:
    result = extract("The class `FastAPIApp` owns the service routes.")
    assert [(claim.kind, claim.value) for claim in result.claims] == [(ClaimKind.symbol, "FastAPIApp")]


def test_application_factory_name_is_a_symbol() -> None:
    result = extract("Create the application with `create_app`.")
    assert [(claim.kind, claim.value) for claim in result.claims] == [(ClaimKind.symbol, "create_app")]


def test_code_command_flags_are_not_misclassified_as_this_projects_flags() -> None:
    result = extract("Run `npx node-tool deploy --dry-run` before release.")
    assert all(claim.value != "--dry-run" for claim in result.claims)
    assert [claim.kind for claim in result.claims] == [ClaimKind.code_command]


def test_shell_fence_extracts_known_command_without_interpreting_it() -> None:
    result = extract("```sh\nnpx node-tool deploy --dry-run\n```\n")
    assert [(claim.kind, claim.value, claim.line) for claim in result.claims] == [
        (ClaimKind.code_command, "npx node-tool deploy --dry-run", 2)
    ]


def test_duplicate_values_get_distinct_stable_ids() -> None:
    result = extract("Use `--port` first. Then pass `--port` again.\n")
    claims = [claim for claim in result.claims if claim.kind == ClaimKind.cli_flag]
    assert len(claims) == 2
    assert claims[0].claim_id != claims[1].claim_id
