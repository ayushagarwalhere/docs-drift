"""Exercise trusted CI orchestration and worker boundaries without network or model calls."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from docs_drift.baseline import save_baseline
from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.scanning import run_scan
from docs_drift.web import worker
from docs_drift.web.git import JobError
from docs_drift.web.settings import Settings
from scripts import action_runner, build_public_docs
from tests.fixtures.build_fixtures import build_fixtures
from tests.test_models import result as empty_result

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("trusted-ci")
    build_fixtures(parent)
    return parent / "py-cli"


@pytest.mark.parametrize(
    "args",
    [
        ["index"],
        ["index", "--tree"],
        ["index", "--list", "cli_flag"],
        ["index", "--find", "listen"],
        ["index", "--json"],
        ["insights"],
        ["insights", "--format", "json"],
        ["impact", "--base", "HEAD~1", "--format", "json"],
        ["scan", "--claims-only", "--show-suppressed"],
        ["cache", "clear"],
    ],
)
def test_cli_inspection_commands(repo: Path, args: list[str]) -> None:
    result = CliRunner().invoke(app, [*args, "--repo", str(repo), "--no-color"])
    assert result.exit_code == 0, result.output
    assert result.output.strip()


@pytest.mark.parametrize("fail", ["true", "false"])
def test_action_writes_parseable_reports_and_restores_cache_policy(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    for key, value in {
        "GITHUB_WORKSPACE": str(repo),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "FAIL_ON_DRIFT": fail,
    }.items():
        monkeypatch.setenv(key, value)
    if fail == "true":
        with pytest.raises(SystemExit) as raised:
            action_runner.main()
        assert raised.value.code == 1
    else:
        action_runner.main()
    assert not CACHE_DISABLED.get()
    output = Path(
        next(
            line.split("=", 1)[1]
            for line in (tmp_path / "output.txt").read_text().splitlines()
            if line.startswith("report-directory=")
        )
    )
    assert json.loads((output / "scan.json").read_text())["schema_version"] == 1
    assert json.loads((output / "scan.sarif").read_text())["version"] == "2.1.0"
    assert "<html" in (output / "scan.html").read_text(encoding="utf-8")
    assert "report-directory=" in (tmp_path / "output.txt").read_text()
    assert (tmp_path / "summary.md").stat().st_size > 0


def test_action_rejects_workspace_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SCAN_REPO", "../elsewhere")
    with pytest.raises(ValueError, match="inside"):
        action_runner.main()


def test_action_baseline_preserves_full_report_and_publishes_gate_outputs(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = run_scan(repo, Config())
    save_baseline(repo / "reviewed-baseline.json", scan.findings)
    monkeypatch.setattr(action_runner, "run_scan", lambda *args, **kwargs: scan)
    for key, value in {
        "GITHUB_WORKSPACE": str(repo),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "SCAN_BASELINE": "reviewed-baseline.json",
    }.items():
        monkeypatch.setenv(key, value)
    try:
        action_runner.main()
        directory = Path(
            next(
                line.split("=", 1)[1]
                for line in (tmp_path / "output.txt").read_text().splitlines()
                if line.startswith("report-directory=")
            )
        )
        gate = json.loads((directory / "gate.json").read_text())
        assert gate["status"] == "passed"
        assert gate["baseline_drift"] > 0
        assert json.loads((directory / "new.json").read_text())["findings"] == []
        assert len(json.loads((directory / "scan.json").read_text())["findings"]) == len(scan.findings)
        assert "gate-status=passed" in (tmp_path / "output.txt").read_text()
        assert "baseline accepted" in (tmp_path / "summary.md").read_text()
    finally:
        (repo / "reviewed-baseline.json").unlink()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SCAN_BASELINE", "../outside.json"),
        ("SCAN_BASELINE", "missing.json"),
        ("SCAN_MIN_CONFIDENCE", "nan"),
        ("SCAN_MAX_FINDINGS", "-1"),
        ("FAIL_ON_DRIFT", "tru"),
    ],
)
def test_action_invalid_adoption_policy_never_scans(
    repo: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    monkeypatch.setenv(key, value)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid policy must fail before the scan starts.")

    monkeypatch.setattr(action_runner, "run_scan", unexpected)
    with pytest.raises(ValueError):
        action_runner.main()


def test_multiple_action_steps_keep_independent_report_directories(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(action_runner, "run_scan", lambda *args, **kwargs: empty_result())
    for key, value in {
        "GITHUB_WORKSPACE": str(repo),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    action_runner.main()
    action_runner.main()
    directories = [
        Path(line.split("=", 1)[1])
        for line in (tmp_path / "output.txt").read_text().splitlines()
        if line.startswith("report-directory=")
    ]
    assert len(set(directories)) == 2
    assert all((path / "gate.json").is_file() and (path / "scan.html").is_file() for path in directories)


@pytest.mark.parametrize("failure", [None, "clone", "unexpected"])
def test_worker_protocol_results_and_sanitized_failures(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str | None
) -> None:
    request = {
        "workspace": str(tmp_path),
        "repo_url": "https://github.com/example/tool",
        "settings": {"data_dir": str(tmp_path)},
    }
    monkeypatch.setattr(
        worker, "sys", SimpleNamespace(stdin=io.StringIO(json.dumps(request) + "\ncontinue\n"), platform="win32")
    )
    result = run_scan(repo, Config())

    def resolve(*args: object) -> str:
        if failure == "clone":
            raise JobError("clone", "Clone failed", "Retry")
        if failure == "unexpected":
            raise RuntimeError("PRIVATE SOURCE SHOULD NEVER APPEAR")
        return result.head_sha

    monkeypatch.setattr(worker, "resolve", resolve)
    monkeypatch.setattr(worker, "checkout", lambda *args: repo)
    monkeypatch.setattr(worker, "execute", lambda *args: result)
    worker.main()
    output = capsys.readouterr().out
    messages = [json.loads(line) for line in output.splitlines()]
    assert messages[-1]["status"] == ("failed" if failure else "done")
    assert "PRIVATE SOURCE" not in output
    if not failure:
        public = json.loads((tmp_path / "result.json").read_text())
        assert public["remote_url"] == request["repo_url"]
        assert all(
            f["introduced_by"] is None or f["introduced_by"]["author_name"] == "Author hidden"
            for f in public["findings"]
        )


def test_worker_executes_only_static_scanner_and_enforces_input_limits(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Use `app.py`.\n")
    (tmp_path / "app.py").write_text('raise RuntimeError("must never execute target")\n')
    stages = []
    result = worker.execute(tmp_path, Settings(), lambda stage, counts: stages.append(stage))
    assert result.findings[0].verdict == "ok"
    assert stages == ["indexing", "matching", "history"]
    assert not CACHE_DISABLED.get()
    (tmp_path / "large.txt").write_text("x" * 8193)
    with pytest.raises(JobError, match="long line"):
        worker.execute(tmp_path, Settings(), lambda *args: None)


@pytest.mark.parametrize(
    "marker, text",
    [
        ("file", "<!-- docs-drift-ignore-file -->\nUse `missing.py`.\n"),
        ("next", "<!-- docs-drift-ignore-next-line -->\nUse `missing.py`.\n"),
        ("section", "# Usage <!-- docs-drift-ignore-section -->\nUse `missing.py`.\n"),
    ],
)
def test_suppression_has_an_auditable_record(tmp_path: Path, marker: str, text: str) -> None:
    (tmp_path / "README.md").write_text(text)
    result = scan_claims(tmp_path, Config())
    assert not result.claims, marker
    assert len(result.suppressed) == 1


def test_public_docs_generator_keeps_reference_and_navigation_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "SECURITY.md"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    monkeypatch.chdir(tmp_path)
    build_public_docs.main()
    docs = tmp_path / "docs"
    assert len(list(docs.glob("*.md"))) >= 20
    index = (docs / "index.md").read_text(encoding="utf-8")
    assert "](assets/scan-demo.gif)" in index
    assert "](license.md)" in index
    assert "Pydantic" in (docs / "configuration.md").read_text(encoding="utf-8") or "Config" in (
        docs / "configuration.md"
    ).read_text(encoding="utf-8")
    assert "MIT" in (docs / "license.md").read_text()
