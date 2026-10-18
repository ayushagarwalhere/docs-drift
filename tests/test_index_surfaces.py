"""Static settings, schemas and parser failures retain evidence and healthy neighbors."""

import json
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.models import SymbolKind
from docs_drift.scanning import run_scan
from docs_drift.workspaces import detect_packages


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_config_examples_schemas_workflow_and_container_defaults(tmp_path: Path) -> None:
    put(tmp_path, "example.json", '{"server":{"workers":4}}')
    put(tmp_path, "example.toml", "[server]\ntimeout=30\n")
    put(tmp_path, "example.yaml", "server:\n  host: localhost\n  port: 8080\n")
    put(
        tmp_path,
        "settings.schema.json",
        json.dumps(
            {"properties": {"listen": {"default": 80}, "server": {"properties": {"debug": {"default": False}}}}}
        ),
    )
    put(tmp_path, "Dockerfile", "FROM scratch\nENV APP_MODE=production\nENV APP_HOST=0.0.0.0\n")
    put(tmp_path, ".github/workflows/test.yml", "jobs:\n  unit:\n    runs-on: ubuntu-latest\n")
    index = build_index(tmp_path, Config(), no_cache=True)
    names = {s.name for s in index.symbols}
    assert {"server.workers", "server.timeout", "server.host", "listen", "server.debug", "APP_MODE"} <= names
    workers = next(s for s in index.symbols if s.name == "server.workers")
    assert workers.default == "4" and workers.file == "example.json" and not workers.authoritative
    assert all(s.line >= 1 for s in index.symbols)


def test_malformed_schema_does_not_discard_healthy_batch_neighbors(tmp_path: Path) -> None:
    put(tmp_path, "broken.schema.json", '{"properties": ["not a mapping"]}')
    put(tmp_path, "good.schema.json", '{"properties":{"timeout":{"default":30}}}')
    index = build_index(tmp_path, Config(), no_cache=True)
    assert any(s.name == "timeout" for s in index.symbols)
    assert any("broken.schema.json" in warning for warning in index.warnings)


def test_click_decorator_env_and_argparse_choices_are_declarations(tmp_path: Path) -> None:
    put(
        tmp_path,
        "cli.py",
        """import argparse
import click
parser = argparse.ArgumentParser(prog="tool")
sub = parser.add_subparsers()
serve = sub.add_parser("serve", aliases=["run"])
serve.add_argument("path")
serve.add_argument("--mode", choices=["fast", "safe"], default="safe")
@click.command()
@click.option("--host", "-h", envvar="APP_HOST", default="localhost")
def main(host): pass
""",
    )
    index = build_index(tmp_path, Config(), no_cache=True)
    mode = next(s for s in index.symbols if s.name == "--mode")
    assert {"fast", "safe"} <= set(mode.aliases)
    assert mode.default == "safe"
    assert any(s.kind == SymbolKind.cli_argument and s.name == "path" for s in index.symbols)
    assert any(s.kind == SymbolKind.env_var and s.name == "APP_HOST" for s in index.symbols)


def test_dynamic_default_is_unknown_and_target_code_is_not_evaluated(tmp_path: Path) -> None:
    put(
        tmp_path,
        "settings.py",
        """import os
def forbidden(): raise RuntimeError("must not run")
class Settings:
    timeout: int = forbidden()
value = os.getenv("APP_PORT", os.getenv("OTHER_PORT", "80"))
""",
    )
    index = build_index(tmp_path, Config(), no_cache=True)
    assert next(s for s in index.symbols if s.name == "timeout").default is None
    assert next(s for s in index.symbols if s.name == "APP_PORT").default is None


@pytest.mark.parametrize(
    "file, text",
    [
        ("package.json", '{"name":"tool","scripts":null,"workspaces":7}'),
        ("pyproject.toml", 'project="not a table"\ntool="not a table"'),
        ("Cargo.toml", 'package="not a table"\nbin=[7,"bad"]\nworkspace=3'),
    ],
)
def test_malformed_manifest_shapes_never_crash_package_discovery(tmp_path: Path, file: str, text: str) -> None:
    put(tmp_path, file, text)
    assert detect_packages(tmp_path)


def test_long_relative_paths_are_skipped_with_warning_or_read_normally(tmp_path: Path) -> None:
    relative = "/".join(["directory" * 5] * 6) + "/module.py"
    try:
        put(tmp_path, relative, "def safe(): pass\n")
    except OSError:
        pytest.skip("Host does not permit constructing a long-path test input")
    put(tmp_path, "README.md", "Use `module.py`.\r\n")
    result = run_scan(tmp_path, Config())
    assert result.findings
    assert all(isinstance(warning, str) for warning in result.warnings)


def test_ambiguous_default_with_existing_subject_cannot_be_promoted_by_history(tmp_path: Path) -> None:
    from tests.test_history import _commit, _git  # noqa: PLC0415

    _git(tmp_path, "init", "-q")
    put(tmp_path, "README.md", "The `docs` setting defaults to `[]`.\n")
    _commit(tmp_path, "class Config:\n    docs: list = []\n", "2026-01-01T00:00:00+00:00")
    _git(tmp_path, "add", "README.md")
    _commit(tmp_path, "class Config:\n    docs: list\n    llm: bool = False\n", "2026-01-02T00:00:00+00:00")
    result = run_scan(tmp_path, Config())
    default = next(f for f in result.findings if f.claim.kind == "default_value")
    assert default.verdict == "unverifiable" and default.suggestion is None
