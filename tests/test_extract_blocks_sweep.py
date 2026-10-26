"""Regression cases from the 12-repository sweep for fenced examples (offline, nothing is executed)."""

import hashlib
import shlex
from pathlib import Path

from docs_drift.docs.extract_blocks import extract_block_claims
from docs_drift.docs.qualifiers import CONTEXT_PREFIX
from docs_drift.docs.sections import parse_document
from docs_drift.models import Claim, ClaimKind, DocFile, DocFormat


def project(tmp_path: Path, name: str = "llm") -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\n[project.scripts]\n{name}="{name}.cli:main"\n', encoding="utf-8"
    )
    (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "docs").write_text('#!/bin/sh\nuv run zensical "$@"\n', encoding="utf-8")
    return tmp_path


def claims(text: str, repo: Path, path: str = "README.md") -> list[Claim]:
    fmt = DocFormat.rst if path.endswith(".rst") else DocFormat.markdown
    doc = DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=fmt)
    return extract_block_claims(parse_document(doc, text), repo).claims


def of_kind(found: list[Claim], kind: ClaimKind) -> list[str]:
    return [item.value for item in found if item.kind == kind]


# Group 8: shell quoting and RST code-block extent.
def test_quoted_prompts_stay_single_arguments(tmp_path: Path) -> None:
    # simonw/llm docs/schemas.md:525 and psf/black the_basics.md:300
    found = claims("```bash\nllm --schema t:dog 'invent a dog'\nllm -c \"format = 'this'\"\n```\n", project(tmp_path))
    commands = of_kind(found, ClaimKind.code_command)
    assert [shlex.split(command) for command in commands] == [
        ["llm", "--schema", "t:dog", "invent a dog"],
        ["llm", "-c", "format = 'this'"],
    ]
    assert of_kind(found, ClaimKind.cli_command) == []


def test_rst_code_block_ends_before_less_indented_list_prose(tmp_path: Path) -> None:
    # pypa/twine docs/index.rst:81 ("Twine will prompt ..." swallowed as code)
    text = (
        "Usage\n=====\n\n2. Upload:\n\n   .. code-block:: bash\n\n      twine upload dist/*\n\n"
        "   Twine will prompt for your username and password.\n"
    )
    found = claims(text, project(tmp_path, "twine"), path="docs/index.rst")
    assert of_kind(found, ClaimKind.code_command) == ["twine upload 'dist/*'"]
    assert "will" not in of_kind(found, ClaimKind.cli_command)


# Group 9: stdin dashes, negative numbers and TAB markers.
def test_dash_values_and_tab_markers_are_not_flags(tmp_path: Path) -> None:
    # simonw/llm related-tools.md:54, typer number.md:61, typer callback-and-context.md:113
    found = claims(
        "```console\n$ llm embed-multi symbols - --format nl\n$ llm score --score -5\n$ llm run --[TAB][TAB]\n```\n",
        project(tmp_path),
    )
    assert of_kind(found, ClaimKind.cli_flag) == ["--format", "--score"]
    assert all("[TAB]" not in command for command in of_kind(found, ClaimKind.code_command))


# Group 13: arguments forwarded to another program.
def test_task_runner_and_script_arguments_are_forwarded(tmp_path: Path) -> None:
    # simonw/datasette contributing.rst:120, encode/uvicorn contributing.md:102
    repo = project(tmp_path)
    found = claims("```bash\njust playwright firefox --headed\n./scripts/docs serve\n```\n", repo)
    assert of_kind(found, ClaimKind.cli_flag) == []
    assert of_kind(found, ClaimKind.cli_command) == []
    script = next(item for item in found if item.kind == ClaimKind.code_command and "scripts/docs" in item.value)
    assert script.qualifier == f"{CONTEXT_PREFIX}forwarded:./scripts/docs"


def test_runner_wrappers_only_emit_project_arguments(tmp_path: Path) -> None:
    # fastapi/typer typer-command.md:52 (`python -m typer`) and `uv run pytest -k`
    repo = project(tmp_path)
    found = claims("```console\n$ uv run python -m llm --help\n$ uv run pytest -k permissions\n```\n", repo)
    assert of_kind(found, ClaimKind.cli_flag) == ["--help"]


def test_package_manager_builtins_are_not_project_commands(tmp_path: Path) -> None:
    # sindresorhus/np readme.md:239 (`yarn config set ...`)
    found = claims("```sh\n$ yarn config set version-sign-git-tag true\n```\n", project(tmp_path, "np"))
    assert found == []


def test_commands_from_an_installed_plugin_are_unverifiable(tmp_path: Path) -> None:
    # simonw/llm docs/setup.md:78
    found = claims("```bash\nllm install llm-python\nllm python\n```\n", project(tmp_path))
    plugin = [item for item in found if item.kind == ClaimKind.code_command and item.value == "llm python"]
    assert plugin and plugin[0].qualifier == f"{CONTEXT_PREFIX}plugin:llm-python"


# Group 1: configuration blocks.
def test_only_the_projects_own_tool_table_is_claimed_and_unprefixed(tmp_path: Path) -> None:
    # psf/black the_basics.md:559, pallets/click standalone-apps.md:79, black using_black_with_other_tools.md
    repo = project(tmp_path, "black")
    found = claims(
        '## Configuration\n\n```toml\n[tool.black]\nline-length = 88\n[tool.isort]\nprofile = "black"\n'
        '[tool.briefcase]\nversion = "1"\n```\n',
        repo,
    )
    assert of_kind(found, ClaimKind.config_key) == ["line-length"]


def test_logging_dictconfig_examples_are_not_project_configuration(tmp_path: Path) -> None:
    # encode/uvicorn docs/concepts/logging.md:76-130
    yaml = (
        "## Configuration\n\n```yaml\nversion: 1\ndisable_existing_loggers: false\nformatters:\n"
        "  default:\n    fmt: '%(message)s'\nloggers:\n  uvicorn:\n    level: INFO\n```\n"
    )
    json_block = (
        '## Configuration\n\n```json\n{"version": 1, "loggers": {"uvicorn.error": {"level": "INFO"}}, '
        '"formatters": {"access": {"()": "uvicorn.logging.AccessFormatter"}}}\n```\n'
    )
    assert of_kind(claims(yaml, project(tmp_path)), ClaimKind.config_key) == []
    assert of_kind(claims(json_block, project(tmp_path)), ClaimKind.config_key) == []


def test_package_json_example_keeps_only_the_projects_field(tmp_path: Path) -> None:
    # sindresorhus/np readme.md:139 ("name" in the user's package.json)
    text = '## Config\n\n```json\n{\n  "name": "superb-package",\n  "np": {\n    "yarn": false\n  }\n}\n```\n'
    assert of_kind(claims(text, project(tmp_path, "np")), ClaimKind.config_key) == ["yarn"]


def test_dotted_keys_stop_flattening(tmp_path: Path) -> None:
    text = '## Settings\n\n```json\n{"server": {"port": 1, "a.b": {"c": 2}}}\n```\n'
    assert of_kind(claims(text, project(tmp_path)), ClaimKind.config_key) == ["server.port"]


# Group 11: requests to other people's servers.
def test_requests_to_external_hosts_are_not_project_routes(tmp_path: Path) -> None:
    repo = project(tmp_path)
    # psf/black editors.md:300 (curl from raw.githubusercontent.com)
    assert (
        claims("```sh\ncurl -o b.vim https://raw.githubusercontent.com/psf/black/stable/plugin/black.vim\n```\n", repo)
        == []
    )
    # httpie/cli README.md:2519 (```http with `Host: pie.dev`)
    assert claims("```http\nPOST /post HTTP/1.1\nHost: pie.dev\n```\n", repo) == []
    # httpie/cli README.md:379 (request line echoing `http DELETE pie.dev/delete`)
    assert (
        claims("```bash\n$ http DELETE pie.dev/delete\n```\n\nSent:\n\n```http\nDELETE /delete HTTP/1.1\n```\n", repo)
        == []
    )
    # httpie/cli README.md:540-550 (filesystem-looking and dot-segment paths)
    assert claims("```http\nGET /../../etc/password HTTP/1.1\nHost: localhost\n```\n", repo) == []


def test_local_requests_remain_endpoint_claims(tmp_path: Path) -> None:
    repo = project(tmp_path)
    assert of_kind(claims("```sh\ncurl -X POST http://localhost:8000/login\n```\n", repo), ClaimKind.endpoint) == [
        "POST /login"
    ]
    assert of_kind(claims("```http\nGET /users HTTP/1.1\nHost: localhost:8000\n```\n", repo), ClaimKind.endpoint) == [
        "GET /users"
    ]
