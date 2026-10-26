"""Drift age (stale since / age at HEAD) and the fix-by policy, against dated fixture histories."""

import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config, ConfigError, PolicyConfig, load_config
from docs_drift.drift_age import (
    AgeStatus,
    age_insights,
    age_status,
    annotate_ages,
    gate_by_age,
    histogram,
    load_trend,
    policy_summary,
    with_max_age,
)
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions, render
from docs_drift.scanning import run_scan
from tests.fixtures.build_fixtures import build_fixtures


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("age")
    build_fixtures(root)
    return root


@pytest.fixture(scope="module")
def py_cli(fixtures: Path) -> ScanResult:
    return run_scan(fixtures / "py-cli", Config())


def test_ages_are_measured_from_the_introducing_commit_to_head(py_cli: ScanResult) -> None:
    # Fixture commits are dated 2026-10-01..05 and HEAD is the 2026-10-05 commit.
    assert py_cli.scan_time.date() == date(2026, 10, 5)
    port = next(f for f in py_cli.findings if f.claim.value == "--port" and f.verdict == "renamed")
    assert (port.stale_since, port.age_days, port.fix_by) == (date(2026, 10, 2), 3, None)
    migrate = next(f for f in py_cli.findings if f.claim.value == "migrate")
    assert (migrate.stale_since, migrate.age_days) == (date(2026, 10, 5), 0)
    for finding in py_cli.findings:
        if not finding.is_drift:
            assert finding.age_days is None and finding.stale_since is None


def test_policy_sets_due_dates_and_statuses(py_cli: ScanResult) -> None:
    policy = PolicyConfig(max_drift_age_days=2, warn_drift_age_days=1)
    result = annotate_ages(py_cli, policy)
    assert result.config_used.policy == policy
    statuses = {f.claim.value: age_status(f, policy) for f in result.findings if f.is_drift}
    assert statuses["--port"] == AgeStatus.overdue  # 3 days > 2
    assert statuses["--workers"] == AgeStatus.warning  # 2 days: not older than 2, but older than 1
    assert statuses["migrate"] == AgeStatus.fresh
    port = next(f for f in result.findings if f.claim.value == "--port" and f.is_drift)
    assert port.fix_by == date(2026, 10, 4)
    workers = next(f for f in result.findings if f.claim.value == "--workers" and f.is_drift)
    assert workers.age_days == 2
    assert age_status(workers, PolicyConfig(max_drift_age_days=5, warn_drift_age_days=1)) == AgeStatus.warning
    summary = policy_summary(result.findings, policy)
    assert summary is not None and "older than 2 days (overdue)" in summary and "older than 1 day" in summary


def test_unversioned_results_and_future_commits(py_cli: ScanResult) -> None:
    unversioned = py_cli.model_copy(update={"head_sha": "0" * 40})
    assert annotate_ages(unversioned) is unversioned
    earlier_head = py_cli.model_copy(update={"scan_time": datetime(2026, 9, 1, tzinfo=UTC)})
    ages = {f.age_days for f in annotate_ages(earlier_head).findings if f.is_drift and f.introduced_by}
    assert ages == {0}


def test_gate_by_age_keeps_default_behaviour_and_handles_unknown(py_cli: ScanResult) -> None:
    drift = [f for f in py_cli.findings if f.is_drift]
    assert gate_by_age(drift, PolicyConfig()) == drift
    assert gate_by_age(drift, PolicyConfig(warn_drift_age_days=1)) == drift
    unknown = drift[0].model_copy(update={"age_days": None, "stale_since": None})
    assert gate_by_age([unknown], PolicyConfig(max_drift_age_days=1)) == [unknown]
    assert gate_by_age([unknown], PolicyConfig(max_drift_age_days=1, fail_unknown_age=False)) == []
    assert policy_summary(drift, PolicyConfig()) is None


def test_histogram_boundaries_are_inclusive_and_ordered() -> None:
    buckets = histogram([0, 7, 8, 30, 31, 90, 91, 365, 366, None])
    assert [(b["bucket"], b["count"]) for b in buckets] == [
        ("0-7", 2),
        ("8-30", 2),
        ("31-90", 2),
        ("91-365", 2),
        ("366+", 1),
        ("unknown", 1),
    ]


def test_policy_configuration_is_validated(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text(
        "[policy]\nmax_drift_age_days = 30\nwarn_drift_age_days = 14\n", encoding="utf-8"
    )
    assert load_config(tmp_path).policy == PolicyConfig(max_drift_age_days=30, warn_drift_age_days=14)
    (tmp_path / ".docs-drift.toml").write_text(
        "[policy]\nmax_drift_age_days = 7\nwarn_drift_age_days = 14\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="warn_drift_age_days"):
        load_config(tmp_path)
    (tmp_path / ".docs-drift.toml").write_text("[policy]\nmax_drift_age_days = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    assert with_max_age(PolicyConfig(warn_drift_age_days=14), 7) == PolicyConfig(max_drift_age_days=7)
    assert with_max_age(PolicyConfig(warn_drift_age_days=3), 7).warn_drift_age_days == 3
    assert Config().policy == PolicyConfig()


def test_check_without_policy_is_unchanged(fixtures: Path) -> None:
    result = CliRunner().invoke(app, ["check", "--repo", str(fixtures / "py-cli"), "--no-color"])
    assert result.exit_code == 1, result.output
    assert "Drift age policy" not in result.output
    assert "Stale since 2026-10-02 (3 days at HEAD)" in result.output
    assert "fix by" not in result.output


def test_check_fails_only_for_drift_older_than_the_maximum(fixtures: Path) -> None:
    repo = str(fixtures / "py-cli")
    fresh = CliRunner().invoke(app, ["check", "--repo", repo, "--no-color", "--max-drift-age", "5"])
    assert fresh.exit_code == 0, fresh.output
    old = CliRunner().invoke(app, ["check", "--repo", repo, "--no-color", "--max-drift-age", "1"])
    assert old.exit_code == 1, old.output
    assert "fix by 2026-10-03 · OVERDUE" in old.output
    assert "Drift age policy: 2 older than 1 day (overdue)" in old.output
    negative = CliRunner().invoke(app, ["check", "--repo", repo, "--max-drift-age", "-1"])
    assert negative.exit_code == 2


def test_no_history_with_an_age_policy_says_why_every_age_is_unknown(fixtures: Path) -> None:
    repo = str(fixtures / "py-cli")
    result = CliRunner().invoke(app, ["check", "--repo", repo, "--no-color", "--no-history", "--max-drift-age", "5"])
    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())
    assert "without history attribution (gated as overdue)" in output
    assert "--no-history skips attribution, so no drift has an age under the policy." in output


def test_policy_from_configuration_and_unknown_ages(fixtures: Path, tmp_path: Path) -> None:
    repo = str(fixtures / "py-cli")
    config = tmp_path / "policy.toml"
    config.write_text("[policy]\nmax_drift_age_days = 5\n", encoding="utf-8")
    assert CliRunner().invoke(app, ["check", "--repo", repo, "--config", str(config)]).exit_code == 0
    # Unverifiable findings have no age; by default unknown ages still fail the gate.
    args = ["check", "--repo", repo, "--config", str(config), "--fail-on", "any", "--min-confidence", "0"]
    any_finding = CliRunner().invoke(app, args)
    assert any_finding.exit_code == 1, any_finding.output
    assert "(gated as overdue)" in any_finding.output
    config.write_text("[policy]\nmax_drift_age_days = 5\nfail_unknown_age = false\n", encoding="utf-8")
    relaxed = CliRunner().invoke(app, args)
    assert relaxed.exit_code == 0, relaxed.output


def test_reports_show_ages_and_json_stays_backward_compatible(py_cli: ScanResult) -> None:
    result = annotate_ages(py_cli, PolicyConfig(max_drift_age_days=30))
    data = json.loads(render(result, "json"))
    port = next(f for f in data["findings"] if f["claim"]["value"] == "--port" and f["verdict"] == "renamed")
    assert (port["stale_since"], port["age_days"], port["fix_by"]) == ("2026-10-02", 3, "2026-11-01")
    for finding in data["findings"]:
        for key in ("stale_since", "age_days", "fix_by"):
            finding.pop(key)
    assert ScanResult.model_validate(data).findings[0].age_days is None
    markdown = render(result, "markdown")
    assert "fix by 2026-11-01" in markdown and "Stale since 2026-10-02 (3 days at HEAD)" in markdown
    html = render(result, "html")
    assert "stale 3 days at HEAD" in html and "fix by 2026-11-01" in html
    terminal = render(result, "terminal", RenderOptions())
    assert "Stale since 2026-10-02 (3 days at HEAD) · fix by 2026-11-01" in terminal


def test_insights_age_histogram_and_oldest(py_cli: ScanResult) -> None:
    drift = [f for f in py_cli.findings if f.is_drift and f.confidence >= 0.6]
    data = age_insights(drift, PolicyConfig(max_drift_age_days=2))
    assert [item["age_days"] for item in data["oldest"]] == [3, 2, 0]
    assert data["oldest"][0]["status"] == "overdue"
    assert data["status"]["overdue"] == 1
    assert data["median_age_days"] == 2
    assert data["histogram"][0] == {"bucket": "0-7", "max_days": 7, "count": 3}


def test_insights_trend_reads_old_and_new_reports(fixtures: Path, py_cli: ScanResult, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    new = json.loads(render(py_cli, "json"))
    old = json.loads(render(py_cli, "json"))
    old["scan_time"] = "2026-10-04T09:00:00Z"
    for finding in old["findings"]:
        for key in ("stale_since", "age_days", "fix_by"):
            finding.pop(key)
    (reports / "b-new.json").write_text(json.dumps(new), encoding="utf-8")
    (reports / "a-old.json").write_text(json.dumps(old), encoding="utf-8")
    (reports / "notes.json").write_text("[1, 2]", encoding="utf-8")
    (reports / "broken.json").write_text("{", encoding="utf-8")
    points, warnings = load_trend(reports)
    assert [p.file for p in points] == ["a-old.json", "b-new.json"]
    assert [p.oldest_age_days for p in points] == [2, 3]  # Old report ages derive from commit dates.
    assert [p.drift for p in points] == [3, 3]
    assert len(warnings) == 2
    result = CliRunner().invoke(app, ["insights", "--repo", str(fixtures / "py-cli"), "--trend", str(reports)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [p["file"] for p in payload["trend"]] == ["a-old.json", "b-new.json"]
    assert payload["drift_age"]["oldest"][0]["stale_since"] == "2026-10-02"
    missing = CliRunner().invoke(app, ["insights", "--repo", str(fixtures / "py-cli"), "--trend", str(tmp_path / "x")])
    assert missing.exit_code == 2


def test_trend_reads_a_bounded_number_of_reports(
    py_cli: ScanResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docs_drift import drift_age  # noqa: PLC0415

    monkeypatch.setattr(drift_age, "MAX_TREND_REPORTS", 2)
    text = render(py_cli, "json")
    for name in ("1", "2", "3"):
        (tmp_path / f"{name}.json").write_text(text, encoding="utf-8")
    points, warnings = load_trend(tmp_path)
    assert len(points) == 2
    assert warnings == ["Read the first 2 of 3 reports by file name."]


def _commit(repo: Path, message: str, *, committed: str, authored: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_AUTHOR_DATE=authored,
        GIT_COMMITTER_DATE=committed,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    for args in (["add", "."], ["-c", "commit.gpgSign=false", "commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_age_starts_when_drift_lands_not_when_it_was_authored(tmp_path: Path) -> None:
    """A rebase merge or cherry-pick keeps an old author date; the drift is new on the branch."""
    repo = tmp_path / "rebased"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_bytes(b"# tool\n\nPass `--port` to choose a port.\n")
    source = 'import argparse\np = argparse.ArgumentParser()\nsub = p.add_subparsers()\ns = sub.add_parser("serve")\n'
    (repo / "cli.py").write_text(source + 's.add_argument("--port")\n', encoding="utf-8")
    _commit(repo, "initial", committed="2025-10-01T00:00:00+00:00", authored="2025-10-01T00:00:00+00:00")
    (repo / "cli.py").write_text(source + 's.add_argument("--listen")\n', encoding="utf-8")
    _commit(repo, "Rename port", committed="2026-01-11T00:00:00+00:00", authored="2025-11-12T02:00:00+05:30")
    policy = PolicyConfig(max_drift_age_days=30)
    result = annotate_ages(run_scan(repo, Config()), policy)
    port = next(f for f in result.findings if f.claim.value == "--port" and f.is_drift)
    assert port.introduced_by is not None
    assert port.introduced_by.date == datetime.fromisoformat("2025-11-12T02:00:00+05:30")
    assert port.introduced_by.committed == datetime(2026, 1, 11, tzinfo=UTC)
    assert (port.stale_since, port.age_days, port.fix_by) == (date(2026, 1, 11), 0, date(2026, 2, 10))
    assert age_status(port, policy) == AgeStatus.fresh
    assert gate_by_age([port], policy) == []
    # The committer date survives the JSON report, so trends from saved reports agree.
    saved = json.loads(render(result, "json"))
    for finding in saved["findings"]:
        finding.pop("age_days", None)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r.json").write_text(json.dumps(saved), encoding="utf-8")
    points, _ = load_trend(tmp_path / "reports")
    assert points[0].oldest_age_days == 0


def test_reports_without_a_committer_date_fall_back_to_the_author_date(py_cli: ScanResult) -> None:
    port = next(f for f in py_cli.findings if f.claim.value == "--port" and f.verdict == "renamed")
    assert port.introduced_by is not None
    legacy = port.introduced_by.model_copy(update={"committed": None})
    assert legacy.landed == legacy.date
    aged = annotate_ages(py_cli.model_copy(update={"findings": [port.model_copy(update={"introduced_by": legacy})]}))
    assert aged.findings[0].stale_since == date(2026, 10, 2)


def test_docs_do_not_call_fail_unknown_age_false_a_grace_period() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "drift-age.md").read_text(encoding="utf-8")
    assert "same grace period" not in text
    assert "`fail_unknown_age = false` is not a grace period" in text
    summary = policy_summary([], PolicyConfig(max_drift_age_days=30, fail_unknown_age=False))
    assert summary is not None and "(not gated)" in summary
