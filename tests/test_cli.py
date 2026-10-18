"""Public CLI behavior for the CLI and extraction-only scan."""

from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift import __version__
from docs_drift.cli import app


def test_help_renders() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for text in (
        "scan",
        "check",
        "explain",
        "fix",
        "graph",
        "init",
        "serve",
        "--repo",
        "--config",
        "--format",
        "--verbose",
        "--no-color",
        "--version",
        "terminal",
        "json",
        "markdown",
        "sarif",
        "html",
    ):
        assert text in result.output


@pytest.mark.parametrize("command", ["scan", "check", "explain", "fix", "graph", "init", "serve"])
def test_stub_exits_two(command: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--repo", str(tmp_path), command])
    assert result.exit_code == 2
    assert result.output.strip() == "not built yet"


def test_version_uses_metadata_without_loading_config(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "missing"), "--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__ == version("docs-drift")


@pytest.mark.parametrize("output_format", ["terminal", "json", "markdown", "sarif", "html"])
def test_global_options(output_format: str, tmp_path: Path) -> None:
    config = tmp_path / "custom.toml"
    config.write_text("min_confidence = 0.8\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "--repo",
            str(tmp_path),
            "--config",
            str(config),
            "--format",
            output_format,
            "--verbose",
            "--no-color",
            "scan",
        ],
    )
    assert result.exit_code == 2
    assert result.output.strip() == "not built yet"


def test_claims_only_scan_renders_rows_and_verbose_timings(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Usage\n\nRun `--port` to listen.\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["--repo", str(tmp_path), "--verbose", "scan", "--claims-only"],
    )
    assert result.exit_code == 0, result.output
    assert "cli_flag" in result.output
    assert "--port" in result.output
    assert "Timings:" in result.output


def test_scan_suppression_can_be_shown(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Usage\n\n<!-- docs-drift-ignore-next-line -->\n`--secret`\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["--repo", str(tmp_path), "scan", "--claims-only", "--show-suppressed"])
    assert result.exit_code == 0, result.output
    assert "Suppressed claims" in result.output
    assert "--secret" in result.output


def test_index_tree_displays_static_cli_commands_and_flags(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "sub = parser.add_subparsers()\n"
        "serve = sub.add_parser('serve')\n"
        "serve.add_argument('--port', default=8000)\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--repo", str(tmp_path), "index", "--no-cache", "--tree"])
    assert result.exit_code == 0, result.output
    assert "serve" in result.output
    assert "--port" in result.output


def test_invalid_format_rejected() -> None:
    result = CliRunner().invoke(app, ["--format", "pdf", "scan"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert "not built yet" not in result.output


def test_configuration_error_is_actionable(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "missing.toml"), "scan"])
    assert result.exit_code == 2
    assert "Fix the file or path" in " ".join(result.output.replace("│", "").split())
    assert "Traceback" not in result.output


def test_default_repo_is_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".docs-drift.toml").write_text("min_confidence = 5\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["scan"])
    assert result.exit_code == 2
    assert "Correct the listed fields" in " ".join(result.output.replace("│", "").split())
