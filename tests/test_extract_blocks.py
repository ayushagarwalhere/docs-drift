"""Fenced code extraction tests use text fixtures and never execute their snippets."""

import hashlib
from pathlib import Path

import pytest

from docs_drift.docs.extract_blocks import extract_block_claims, project_command_names
from docs_drift.docs.sections import ParsedDoc, parse_document
from docs_drift.models import Claim, ClaimKind, DocFile, DocFormat


def parsed(text: str) -> ParsedDoc:
    doc = DocFile(path="README.md", content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    return parse_document(doc, text)


def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-app"\n[project.scripts]\nmyapp="example.cli:app"\n', encoding="utf-8"
    )
    (tmp_path / "example").mkdir(exist_ok=True)
    (tmp_path / "example" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("argument", ["/tmp/example", "./output", "data.json", "8080", "https://example.com"])
def test_positional_values_are_not_invented_subcommands(argument: str, tmp_path: Path) -> None:
    found = claims(f"```sh\nmyapp {argument} second-value --verbose\n```\n", project(tmp_path))
    assert not any(item.kind == ClaimKind.cli_command for item in found)
    assert any(item.kind == ClaimKind.code_command and argument in item.value for item in found)


def test_end_of_options_keeps_option_shaped_positional_data_out_of_flag_claims(tmp_path: Path) -> None:
    found = claims("```sh\nmyapp -- --literal-file\n```\n", project(tmp_path))
    assert not any(item.kind == ClaimKind.cli_flag for item in found)
    assert any(item.kind == ClaimKind.code_command for item in found)


def claims(text: str, root: Path) -> list[Claim]:
    return extract_block_claims(parsed(text), root).claims


@pytest.mark.parametrize(
    ("language", "command"),
    [
        ("sh", "myapp serve --port 8000"),
        ("bash", "myapp serve --port 8000"),
        ("shell", "myapp serve --port 8000"),
        ("zsh", "myapp serve --port 8000"),
        ("powershell", "myapp serve --port 8000"),
        ("ps1", "myapp serve --port 8000"),
        ("cmd", "myapp serve --port 8000"),
        ("", "$ myapp serve --port 8000"),
        ("sh", "myapp serve --port 8000 # start app"),
    ],
)
def test_shell_dialects_and_sniffing(language: str, command: str, tmp_path: Path) -> None:
    root = project(tmp_path)
    info = f"{language} " if language else ""
    found = claims(f"```{info}\n{command}\n```\n", root)
    assert any(item.kind == ClaimKind.code_command for item in found)
    assert any(item.kind == ClaimKind.cli_command and item.value == "serve" for item in found)
    assert any(item.kind == ClaimKind.cli_flag and item.value == "--port" for item in found)


@pytest.mark.parametrize(
    ("language", "continuation"),
    [("sh", "\\"), ("powershell", "`"), ("cmd", "^")],
)
def test_shell_continuations(language: str, continuation: str, tmp_path: Path) -> None:
    root = project(tmp_path)
    found = claims(f"```{language}\nmyapp serve {continuation}\n --port 8080\n```\n", root)
    assert any(item.kind == ClaimKind.cli_flag and item.value == "--port" for item in found)


def test_prompts_skip_output_lines(tmp_path: Path) -> None:
    found = claims("```console\n$ myapp serve --port 8000\nServer started\n$ myapp check\nOK\n```\n", project(tmp_path))
    commands = [item.value for item in found if item.kind == ClaimKind.code_command]
    assert commands == ["myapp serve --port 8000", "myapp check"]


def test_env_prefix_and_boolean_flag_values(tmp_path: Path) -> None:
    found = claims("```sh\nAPP_DEBUG=1 myapp serve --port 8000 --verbose\n```\n", project(tmp_path))
    assert any(item.kind == ClaimKind.env_var and item.value == "APP_DEBUG" for item in found)
    flags = [item for item in found if item.kind == ClaimKind.cli_flag]
    assert [(item.value, item.qualifier) for item in flags] == [("--port", "serve"), ("--verbose", "serve")]


def test_command_separators_split_into_independent_commands(tmp_path: Path) -> None:
    found = claims("```sh\nmyapp serve && myapp check; myapp graph | myapp scan\n```\n", project(tmp_path))
    assert [item.value for item in found if item.kind == ClaimKind.cli_command] == ["serve", "check", "graph", "scan"]


@pytest.mark.parametrize("command", ["git log --oneline", "docker run image", "pip install pkg", "npm install pkg"])
def test_third_party_commands_are_ignored(command: str, tmp_path: Path) -> None:
    assert claims(f"```sh\n{command}\n```\n", project(tmp_path)) == []


@pytest.mark.parametrize(
    "command",
    [
        "python -m example.cli serve --port 8000",
        "npx example-app deploy --dry-run",
        "npm run build",
        "pnpm test",
        "yarn lint",
        "uv run my-script --check",
        "poetry run task --fast",
        "make test",
        "just check",
    ],
)
def test_supported_project_wrappers(command: str, tmp_path: Path) -> None:
    found = claims(f"```sh\n{command}\n```\n", project(tmp_path))
    assert any(item.kind == ClaimKind.code_command for item in found)


def test_make_target_and_package_script_kinds(tmp_path: Path) -> None:
    found = claims("```sh\nnpm run build\nmake test\n```\n", project(tmp_path))
    assert [
        (item.kind, item.value) for item in found if item.kind in {ClaimKind.package_script, ClaimKind.make_target}
    ] == [
        (ClaimKind.package_script, "build"),
        (ClaimKind.make_target, "test"),
    ]


def test_dotenv_claims_names_and_does_not_claim_values(tmp_path: Path) -> None:
    found = claims("```dotenv\nAPP_DEBUG=1\nAPP_URL=https://example.test\n```\n", project(tmp_path))
    assert [(item.kind, item.value) for item in found] == [
        (ClaimKind.env_var, "APP_DEBUG"),
        (ClaimKind.env_var, "APP_URL"),
    ]


def test_yaml_and_json_emit_dotted_config_keys(tmp_path: Path) -> None:
    yaml = claims("## Configuration\n```yaml\nserver:\n  timeout: 30\n  host: 0.0.0.0\n```\n", project(tmp_path))
    assert [item.value for item in yaml] == ["server.timeout", "server.host"]
    json_claims = claims('## Settings\n```json\n{"server": {"port": 8080}}\n```\n', project(tmp_path))
    assert [item.value for item in json_claims] == ["server.port"]


def test_toml_filename_hint_enables_config_claims(tmp_path: Path) -> None:
    found = claims('```toml title="pyproject.toml"\n[tool.demo]\ntimeout = 30\n```\n', project(tmp_path))
    assert [item.value for item in found] == ["tool.demo.timeout"]


def test_http_and_curl_blocks_emit_endpoint_claims(tmp_path: Path) -> None:
    http = claims("```http\nPOST http://localhost:8000/login\n```\n", project(tmp_path))
    curl = claims("```sh\ncurl -X POST http://localhost:8000/login\n```\n", project(tmp_path))
    assert [(item.kind, item.value) for item in http] == [(ClaimKind.endpoint, "POST /login")]
    assert [(item.kind, item.value) for item in curl] == [(ClaimKind.endpoint, "POST /login")]


def test_python_imports_are_parsed_without_execution(tmp_path: Path) -> None:
    root = project(tmp_path)
    found = claims("```python\nfrom example.client import Client\nClient.create()\n```\n", root)
    assert {item.value for item in found if item.kind == ClaimKind.symbol} == {"Client", "Client.create"}


def test_javascript_project_imports_and_calls(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "package.json").write_text('{"name":"example-app","bin":"example-app"}', encoding="utf-8")
    found = claims("```js\nimport { Client } from 'example-app';\nClient.create();\n```\n", root)
    assert any(item.kind == ClaimKind.symbol and item.value == "Client" for item in found)


def test_packaging_metadata_names_are_read_as_text(tmp_path: Path) -> None:
    root = project(tmp_path)
    assert project_command_names(root)[0] == {"myapp", "example-app"}


def test_historical_fences_are_suppressed(tmp_path: Path) -> None:
    found = claims("## Migrating from v1\n```sh\nmyapp serve --port 80\n```\n", project(tmp_path))
    assert found == []
