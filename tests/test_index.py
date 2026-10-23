"""Static index tests: repository files are parsed as data and never imported or run."""

import json
import subprocess
from pathlib import Path

import pytest

import docs_drift.index as index_module
from docs_drift.config import Config
from docs_drift.ids import make_symbol_id
from docs_drift.index import build_index, clear_cache
from docs_drift.index.base import CodeIndex, language_for, normalize_name, source_files
from docs_drift.index.routes_common import normalize_route
from docs_drift.models import Symbol, SymbolKind


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ("", {"--help"}),
        ("add_help_option=False", set()),
        ("context_settings={'help_option_names': ['-h', '--assist']}", {"-h", "--assist"}),
        ("context_settings={'help_option_names': []}", set()),
        ("context_settings=dynamic_settings", set()),
        ("add_help_option=dynamic_flag", set()),
    ],
)
def test_typer_help_defaults_respect_disabled_custom_and_dynamic_settings(
    tmp_path: Path, settings: str, expected: set[str]
) -> None:
    repo = _repo(tmp_path, {"cli.py": f"import typer\napp = typer.Typer({settings})\n"})
    symbols = build_index(repo, Config(), no_cache=True).symbols
    actual = {s.name for s in symbols if s.kind == SymbolKind.cli_flag} & {"--help", "-h", "--assist"}
    assert actual == expected


def test_git_listing_failure_never_falls_back_to_untracked_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()

    def denied(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(128, "git")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("A failed Git listing must not traverse ignored filesystem trees.")

    monkeypatch.setattr("docs_drift.index.base.subprocess.run", denied)
    monkeypatch.setattr(Path, "rglob", unexpected)
    with pytest.raises(OSError, match="Git could not list tracked source files"):
        source_files(tmp_path, [])


def test_lookup_is_exact_and_route_normalization_is_stable(tmp_path: Path) -> None:
    symbol = Symbol(
        symbol_id=make_symbol_id(SymbolKind.route, "GET /users/{param}", "app.py"),
        kind=SymbolKind.route,
        name="GET /users/{param}",
        qualified_name="GET /users/{param}",
        aliases=[],
        default=None,
        file="app.py",
        line=1,
        language="python",
    )
    index = CodeIndex(symbols=[symbol])
    assert index.candidates("route", "GET /users/{id}") == []
    assert normalize_route("GET", "/users/<int:id>/")[1] == "/users/{param}"
    assert normalize_name("API_URL") == "api_url"


def test_python_index_is_ast_only_and_cache_hits(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/app.py": "def build_app():\n    return None\n",
            "src/pkg/settings.py": "class Settings:\n    timeout_seconds: int = 60\n",
        },
    )
    first = build_index(repo, Config(), no_cache=False)
    assert any(item.name == "build_app" and item.kind == SymbolKind.function for item in first.symbols)
    setting = next(item for item in first.symbols if item.name == "timeout_seconds")
    assert setting.default == "60" and setting.authoritative
    second = build_index(repo, Config(), no_cache=False)
    assert second.cache_hits == 3
    assert second.cache_misses == 0
    assert clear_cache(repo)
    assert not clear_cache(repo)


def test_javascript_index_and_task_index_are_static(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "name": "sample-tool",
                    "bin": {"sample-tool": "src/cli.ts"},
                    "scripts": {"build": "tsc", "test": "vitest"},
                }
            ),
            "src/cli.ts": 'import { Command } from "commander";\nconst p = new Command();\n'
            'p.command("deploy").option("--dry-run", "dry run");\n',
            "src/app.ts": 'const api = process.env.API_BASE_URL;\nconst app = require("express")();\n'
            'app.get("/healthz", (_req, res) => res.send("ok"));\n',
        },
    )
    result = build_index(repo, Config(), no_cache=True)
    assert any(item.kind == SymbolKind.package_script and item.name == "build" for item in result.symbols)
    assert any(item.kind == SymbolKind.cli_command and item.name == "deploy" for item in result.symbols)
    assert any(item.kind == SymbolKind.cli_flag and item.name == "--dry-run" for item in result.symbols)
    assert any(item.kind == SymbolKind.env_var and item.name == "API_BASE_URL" for item in result.symbols)
    assert any(item.kind == SymbolKind.route and item.name == "GET /healthz" for item in result.symbols)


def test_language_detection_includes_task_files() -> None:
    assert language_for(Path("Makefile")) == "make"
    assert language_for(Path("justfile")) == "just"
    assert language_for(Path(".env.example")) == "dotenv"


def test_only_changed_file_and_version_bump_miss_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, {"one.py": "answer = 1\n", "two.py": "def run(): pass\n"})
    build_index(repo, Config())
    (repo / "one.py").write_text("answer = 2\n", encoding="utf-8")
    changed = build_index(repo, Config())
    assert changed.cache_hits == 1 and changed.cache_misses == 1
    monkeypatch.setattr(index_module, "INDEXER_VERSION", "test-bump")
    bumped = build_index(repo, Config())
    assert bumped.cache_hits == 0 and bumped.cache_misses == 2
