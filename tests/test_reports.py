"""Format contracts, untrusted text, terminal snapshots and CLI destinations."""

import json
import re
from pathlib import Path
from xml.etree import ElementTree

import jsonschema
import pytest
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config, load_config
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions, render
from docs_drift.report.html import github_remote
from docs_drift.scanning import run_scan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def scans(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ScanResult]:
    from tests.fixtures.build_fixtures import build_fixtures  # noqa: PLC0415

    root = tmp_path_factory.mktemp("reports")
    build_fixtures(root)
    return {path.name: run_scan(path, Config()) for path in root.iterdir() if path.is_dir()}


@pytest.mark.parametrize("fixture", ["py-cli", "node-tool", "fastapi-svc", "clean-repo"])
def test_contracts_and_snapshots(scans: dict[str, ScanResult], fixture: str) -> None:
    result = scans[fixture]
    output = json.loads(render(result, "json"))
    jsonschema.validate(output, json.loads((ROOT / "docs/schema/scan-result.v1.json").read_text()))
    assert ScanResult.model_validate(output).head_sha == result.head_sha
    schema = json.loads((ROOT / "docs/schema/sarif-schema-2.1.0.json").read_text())
    jsonschema.Draft4Validator(schema).validate(json.loads(render(result, "sarif")))
    terminal = render(result, "terminal", RenderOptions(show_low_confidence=True))
    assert "\x1b" not in terminal
    normalized = re.sub(r"\d+(?:\.\d+)?s\b", "<time>", terminal)
    normalized = re.sub(r"\d+ days ago", "<age>", normalized)
    normalized = re.sub(r"\b[0-9a-f]{8}\b", "<sha>", normalized)
    assert normalized == (ROOT / f"tests/snapshots/{fixture}.txt").read_text(encoding="utf-8")
    document = render(result, "html")
    assert len(document.encode()) < 1_000_000
    payload = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', document, re.S)
    assert payload and json.loads(payload[1])["nodes"] is not None
    structure = re.findall(r"<(?:header|main|section|aside|footer|article|details|nav)\b[^>]*>", document)
    assert "\n".join(structure) + "\n" == (ROOT / f"tests/snapshots/{fixture}.html.structure").read_text()
    cases = ElementTree.fromstring(render(result, "junit"))
    assert len(cases.findall("testcase")) == len(result.findings)


def test_schema_matches_models() -> None:
    assert json.loads((ROOT / "docs/schema/scan-result.v1.json").read_text()) == ScanResult.model_json_schema()


def test_default_history_and_pr_mode(scans: dict[str, ScanResult]) -> None:
    result = scans["py-cli"]
    default = next(f for f in result.findings if f.verdict == "default_changed")
    assert default.introduced_by is not None
    assert "worker" in default.introduced_by.summary.casefold()
    assert any("-" in e.text and "+" in e.text for e in default.evidence)
    assert "```text" in render(result, "markdown", RenderOptions(pr_mode=True))
    assert "```suggestion" not in render(result, "markdown", RenderOptions(pr_mode=True))


def test_nasty_content_and_truncation(scans: dict[str, ScanResult]) -> None:
    result = scans["py-cli"].model_copy(deep=True)
    nasty = "<script>alert(1)</script> ` | ``` </details>\n::error::injected"
    f = next(f for f in result.findings if f.is_drift)
    f.claim.raw_text = nasty
    f.claim.value = nasty
    f.suggestion = nasty
    f.evidence[0].text = nasty
    assert f.introduced_by is not None
    f.introduced_by.author_name = "PRIVATE_AUTHOR"
    document = render(result, "html")
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "PRIVATE_AUTHOR" not in document
    assert "PRIVATE_AUTHOR" in render(result, "html", RenderOptions(show_authors=True))
    markdown = render(result, "markdown")
    assert "&#124;" in markdown and "&#96;" in markdown
    assert "```suggestion" not in markdown
    capped = render(result, "markdown", RenderOptions(size_cap=1000, artifact_url="full.html"))
    assert len(capped.encode()) <= 1000 and "Report truncated" in capped and "full.html" in capped
    annotations = render(result, "github")
    assert "%0A::error::injected" in annotations
    assert all(line.startswith(("::warning file=", "::error file=")) for line in annotations.splitlines())
    assert github_remote("git@github.com:acme/tool.git") == "https://github.com/acme/tool"
    assert github_remote("https://evil.example/acme/tool") is None
    result.remote_url = "git@github.com:acme/tool.git"
    assert "https://github.com/acme/tool/commit/" in render(result, "html")


def test_multi_output_and_no_escapes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Use `--port`.\n")
    (tmp_path / "cli.py").write_text('import argparse\np=argparse.ArgumentParser()\np.add_argument("--listen")\n')
    json_file, sarif_file = tmp_path / "out.json", tmp_path / "out.sarif"
    r = CliRunner().invoke(
        app,
        [
            "scan",
            "--repo",
            str(tmp_path),
            "--no-color",
            "--output-json",
            str(json_file),
            "--output-sarif",
            str(sarif_file),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.output
    assert json.loads(json_file.read_text())["schema_version"] == 1
    assert json.loads(sarif_file.read_text())["version"] == "2.1.0"


def test_init_yes_and_unexpected_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = CliRunner().invoke(app, ["init", "--yes", "--repo", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert load_config(tmp_path) == Config.model_validate(load_config(tmp_path).model_dump())
    assert "# Documentation paths" in (tmp_path / ".docs-drift.toml").read_text()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("test secret traceback")

    monkeypatch.setattr("docs_drift.scanning.run_scan", broken)
    r = CliRunner().invoke(app, ["scan", "--repo", str(tmp_path)])
    assert r.exit_code == 1, r.output
    assert "Traceback" not in r.output and "last-error.log" in r.output
    assert "test secret traceback" in (tmp_path / ".docs-drift/last-error.log").read_text()
    r = CliRunner().invoke(app, ["scan", "--repo", str(tmp_path), "--debug"])
    assert "Traceback" in r.output


def test_setup_integrations_use_the_created_baseline(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Use `--port`.\n")
    (tmp_path / "cli.py").write_text('import argparse\np=argparse.ArgumentParser()\np.add_argument("--listen")\n')
    result = CliRunner().invoke(app, ["init", "--repo", str(tmp_path), "--yes", "-q"])
    assert result.exit_code == 0 and not result.output
    assert (tmp_path / ".docs-drift-baseline.json").is_file()
    assert "--baseline .docs-drift-baseline.json" in (tmp_path / ".github/workflows/docs-drift.yml").read_text()
    assert "--baseline .docs-drift-baseline.json" in (tmp_path / ".pre-commit-config.yaml").read_text()


def test_command_help_skips_broken_config(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text("not = valid toml !!!")
    result = CliRunner().invoke(app, ["scan", "--repo", str(tmp_path), "--help"])
    assert result.exit_code == 0 and "Example:" in result.output
