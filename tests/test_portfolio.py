"""Organization scans preserve evidence, isolate failures and publish coherent run links."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift import portfolio
from docs_drift.baseline import save_baseline
from docs_drift.cli import app
from tests.fixtures.build_fixtures import build_fixtures
from tests.test_models import finding, result


def manifest(root: Path, text: str) -> Path:
    path = root / "organization.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_portfolio_continues_after_error_and_never_links_a_stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("broken", "healthy"):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".git").mkdir()
    source = manifest(
        tmp_path,
        '[[repositories]]\nname="broken"\npath="broken"\n[[repositories]]\nname="healthy"\npath="healthy"\n',
    )
    calls: list[str] = []

    def scan(repo: Path, *args: object, **kwargs: object) -> object:
        calls.append(repo.name)
        if repo.name == "broken":
            raise ValueError('<img src=x onerror="alert(1)">')
        return result()

    monkeypatch.setattr(portfolio, "run_scan", scan)
    output = tmp_path / "reports"
    evaluated = portfolio.scan_portfolio(tmp_path, source, output)
    assert calls == ["broken", "healthy"]
    assert evaluated.exit_code == 2
    assert evaluated.repositories[0].report is None
    assert evaluated.repositories[1].status == "passed"
    index = (output / "index.html").read_text(encoding="utf-8")
    assert "<img src=x" not in index
    assert "&lt;img" in index
    assert evaluated.repositories[1].report is not None
    assert (output / evaluated.repositories[1].report).is_file()
    assert f"{evaluated.run_id}/portfolio.json" in index
    archived = (output / evaluated.run_id / "index.html").read_text(encoding="utf-8")
    assert 'href="repo-healthy/scan.html"' in archived
    assert 'href="portfolio.json"' in archived
    second = portfolio.scan_portfolio(tmp_path, source, output)
    assert second.run_id != evaluated.run_id
    assert evaluated.run_id not in (output / "index.html").read_text(encoding="utf-8")
    assert (output / evaluated.run_id / "index.html").is_file()


def test_portfolio_uses_per_repository_policy_and_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / ".git").mkdir()
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / ".git").mkdir()
    save_baseline(tmp_path / "api" / "baseline.json", [finding()])
    monkeypatch.setattr(portfolio, "run_scan", lambda *args, **kwargs: result([finding()]))
    source = manifest(
        tmp_path,
        '[[repositories]]\nname="api"\npath="api"\nbaseline="baseline.json"\n'
        '[[repositories]]\nname="cli"\npath="cli"\nmin_confidence=0.9\nmax_findings=0\n',
    )
    output = tmp_path / "reports"
    evaluated = portfolio.scan_portfolio(tmp_path, source, output)
    assert evaluated.exit_code == 1
    assert [item.status for item in evaluated.repositories] == ["passed", "failed"]
    accepted = evaluated.repositories[0]
    assert accepted.gate and accepted.gate.baseline_drift == 1
    assert accepted.report
    full = json.loads((output / accepted.report).with_suffix(".json").read_text())
    assert len(full["findings"]) == 1
    assert portfolio.scan_portfolio(tmp_path, source, output, report_only=True).exit_code == 0


@pytest.mark.parametrize(
    "text",
    [
        '[[repositories]]\nname="bad"\npath="../escape"',
        '[[repositories]]\nname="bad"\npath="missing"',
        '[[repositories]]\nname="bad"\npath="api"\nbaseline="../outside.json"',
        '[[repositories]]\nname="api"\npath="api"\n[[repositories]]\nname="api"\npath="cli"',
        '[[repositories]]\nname="api"\npath="api"\n[[repositories]]\nname="duplicate"\npath="api"',
        '[[repositories]]\nname="../unsafe-name"\npath="api"',
        '[[repositories]]\nname="api"\npath="api"\nmin_confidence=nan',
        '[[repositories]]\nname="api"\npath="api"\nmax_findings=-1',
        "repositories=[]",
    ],
)
def test_manifest_preflight_rejects_invalid_input_before_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / ".git").mkdir()
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / ".git").mkdir()

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("No scan should start after failed preflight.")

    monkeypatch.setattr(portfolio, "run_scan", unexpected)
    with pytest.raises(ValueError):
        portfolio.scan_portfolio(tmp_path, manifest(tmp_path, text), tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


def test_portfolio_output_cannot_pollute_scanned_repository(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / ".git").mkdir()
    with pytest.raises(ValueError, match="outside"):
        portfolio.scan_portfolio(
            tmp_path, manifest(tmp_path, '[[repositories]]\nname="api"\npath="api"'), tmp_path / "api" / "reports"
        )


def test_portfolio_cli_scans_real_fixture_checkouts(tmp_path: Path) -> None:
    build_fixtures(tmp_path)
    manifest(
        tmp_path,
        '[[repositories]]\nname="cli"\npath="py-cli"\n[[repositories]]\nname="clean"\npath="clean-repo"\n',
    )
    source = tmp_path / "organization.toml"
    source.write_text("\ufeff" + source.read_text(encoding="utf-8"), encoding="utf-8")
    invocation = CliRunner().invoke(
        app,
        [
            "portfolio",
            "--repo",
            str(tmp_path),
            "--manifest",
            "organization.toml",
            "--report-dir",
            "reports",
            "--report-only",
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    payload = json.loads((tmp_path / "reports" / "portfolio.json").read_text())
    assert len(payload["repositories"]) == 2
    assert all(item["status"] == "report_only" for item in payload["repositories"])
    assert "Dashboard:" in invocation.output


def test_portfolio_cli_invalid_manifest_is_usage_error(tmp_path: Path) -> None:
    source = manifest(tmp_path, "repositories=[]")
    invocation = CliRunner().invoke(
        app, ["portfolio", "--repo", str(tmp_path), "--manifest", str(source), "--report-dir", "reports"]
    )
    assert invocation.exit_code == 2
    assert not (tmp_path / "reports").exists()


def test_wrapper_requires_explicit_snapshot_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "wrapper").mkdir()
    source = manifest(tmp_path, '[[repositories]]\nname="wrapper"\npath="wrapper"\n')
    with pytest.raises(ValueError, match="checkout root"):
        portfolio.scan_portfolio(tmp_path, source, tmp_path / "reports")
    monkeypatch.setattr(
        portfolio, "run_scan", lambda *args, **kwargs: result().model_copy(update={"head_sha": "0" * 40})
    )
    source.write_text(source.read_text() + "require_git=false\n", encoding="utf-8")
    scan = portfolio.scan_portfolio(tmp_path, source, tmp_path / "reports", report_only=True)
    assert scan.exit_code == 0
    assert scan.repositories[0].gate
    assert any("unversioned" in warning for warning in scan.repositories[0].gate.warnings)
