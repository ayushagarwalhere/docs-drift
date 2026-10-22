"""Public CLI behavior for the CLI and extraction-only scan."""

from importlib.metadata import version
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from docs_drift import __version__
from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims


def test_help_renders() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for text in (
        "scan",
        "check",
        "baseline",
        "ignore",
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
        assert text in Text.from_ansi(result.output).plain


def test_init_and_graph_are_implemented(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["init", "--repo", str(tmp_path), "--yes"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".docs-drift.toml").is_file()
    result = CliRunner().invoke(app, ["graph", "--repo", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output


def test_explain_requires_a_finding_id(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--repo", str(tmp_path), "explain"])
    assert result.exit_code == 2
    assert "FINDING_ID" in result.output


def test_baseline_update_then_check_only_fails_for_new_drift(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Run with `--port`.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--listen")\n', encoding="utf-8"
    )
    runner = CliRunner()
    update = runner.invoke(app, ["--repo", str(tmp_path), "baseline", "update"])
    assert update.exit_code == 0, update.output
    baseline = tmp_path / ".docs-drift-baseline.json"
    assert baseline.is_file()
    clean = runner.invoke(app, ["--repo", str(tmp_path), "check", "--baseline", str(baseline), "--quiet"])
    assert clean.exit_code == 0, clean.output


def test_explain_and_ignore_use_stable_claim_id(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Run with `--port`.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--listen")\n', encoding="utf-8"
    )
    claim = scan_claims(tmp_path, Config()).claims[0]
    runner = CliRunner()
    explained = runner.invoke(app, ["--repo", str(tmp_path), "explain", claim.claim_id[:8]])
    assert explained.exit_code == 0, explained.output
    assert "Finding evidence" in explained.output
    ignored = runner.invoke(app, ["--repo", str(tmp_path), "ignore", claim.claim_id[:8]])
    assert ignored.exit_code == 0, ignored.output
    assert claim.claim_id in (tmp_path / ".docs-drift.toml").read_text(encoding="utf-8")


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
    assert result.exit_code == 0, result.output
    if output_format == "json":
        assert '"schema_version": 1' in result.output
    elif output_format == "terminal":
        assert "docs scanned" in result.output
    elif output_format == "markdown":
        assert "Documentation health" in result.output
    elif output_format == "sarif":
        assert '"version": "2.1.0"' in result.output
    else:
        assert "<!doctype html>" in result.output


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


def test_full_scan_matches_exact_symbols_and_hides_ok_by_default(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Usage\n\nUse `--port` and `--missing`.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--port')\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--repo", str(tmp_path), "scan"])
    assert result.exit_code == 0, result.output
    assert "--port" not in result.output
    weak = CliRunner().invoke(app, ["--repo", str(tmp_path), "scan", "--show-low-confidence"])
    assert "--missing" in weak.output
    all_result = CliRunner().invoke(app, ["--repo", str(tmp_path), "scan", "--all"])
    assert all_result.exit_code == 0, all_result.output
    assert "--port" in all_result.output


def test_check_confidence_threshold_and_fail_on(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Usage\n\nUse `--missing`.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import argparse\nparser = argparse.ArgumentParser()\n", encoding="utf-8")
    default = CliRunner().invoke(app, ["--repo", str(tmp_path), "check", "--quiet"])
    assert default.exit_code == 0 and default.output == ""
    strict = CliRunner().invoke(
        app,
        ["--repo", str(tmp_path), "check", "--fail-on", "drift", "--min-confidence", "0.4", "--quiet"],
    )
    assert strict.exit_code == 1
    assert "--missing" in Text.from_ansi(strict.output).plain
    invalid = CliRunner().invoke(app, ["--repo", str(tmp_path), "check", "--fail-on", "nothing"])
    assert invalid.exit_code == 2


def test_invalid_format_rejected() -> None:
    result = CliRunner().invoke(app, ["--format", "pdf", "scan"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert "not built yet" not in result.output


def test_configuration_error_is_actionable(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "missing.toml"), "scan"])
    assert result.exit_code == 2
    assert "Fix the file or path" in " ".join(Text.from_ansi(result.output).plain.replace("│", "").split())
    assert "Traceback" not in result.output


def test_default_repo_is_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".docs-drift.toml").write_text("min_confidence = 5\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["scan"])
    assert result.exit_code == 2
    assert "Correct the listed fields" in " ".join(Text.from_ansi(result.output).plain.replace("│", "").split())
