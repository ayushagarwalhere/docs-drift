"""Exercise organization workflows against real, static Git repositories."""

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from docs_drift import snapshots, workflow_cli
from docs_drift.cli import app
from docs_drift.collaboration import fix_branch, pr_comparison, static_scan, support_history, write_pr_report
from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.ownership import coverage_report, ownership
from docs_drift.policy import GatePolicy, evaluate_gate
from docs_drift.review import ReviewKind, load_reviews, record_review, suppressed_ids
from docs_drift.snapshots import materialize_snapshot, resolve_revision
from scripts import action_runner


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_workflow_registration_exposes_real_command_and_option_source_evidence(tmp_path: Path) -> None:
    (tmp_path / "workflows.py").write_bytes(Path(workflow_cli.__file__).read_bytes())
    symbols = build_index(tmp_path, Config(), no_cache=True).symbols
    commands = {s.name for s in symbols if s.kind == "cli_command"}
    assert {"pr", "coverage", "timeline", "review", "record", "list", "fix-branch"} <= commands
    flags = {(s.name, s.qualified_name.split(".")[-2]) for s in symbols if s.kind == "cli_flag"}
    assert ("--important", "coverage") in flags
    assert ("--branch", "fix-branch") in flags and ("--destination", "fix-branch") in flags


def commit(repo: Path, message: str) -> str:
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def source(repo: Path, default: int) -> None:
    (repo / "app.py").write_text(
        f'import argparse\np = argparse.ArgumentParser()\np.add_argument("--workers", default={default})\n',
        encoding="utf-8",
    )


def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("| Flag | Default |\n| --- | --- |\n| `--workers` | `4` |\n", encoding="utf-8")
    source(repo, 4)
    base = commit(repo, "supported")
    source(repo, 8)
    head = commit(repo, "changed default")
    return repo, base, head


def test_pr_history_and_review_artifacts(tmp_path: Path) -> None:
    repo, base, head = repository(tmp_path)
    full, selected, comparison = pr_comparison(repo, Config(), base)
    changed = next(f for f in selected.findings if f.verdict == "default_changed")
    assert comparison["base_sha"] == base and comparison["head_sha"] == head
    assert any(e.file == "app.py" and base in e.text for e in changed.evidence)
    assert comparison["observations"][0]["supported_at_base"]
    output = tmp_path / "report"
    payload = write_pr_report(selected, comparison, output, full_result=full)
    assert payload["fixable_edits"] == 1
    assert "+| `--workers` | `8` |" in (output / "fix.patch").read_text()
    assert "app.py:" in (output / "annotations.txt").read_text()
    assert "```suggestion" not in (output / "pr.md").read_text()
    assert (output / "pr.html").is_file()
    assert len(payload["coverage"]["documents"]) == len(full.docs)
    with pytest.raises(ValueError, match="new report directory"):
        write_pr_report(selected, comparison, output)
    timeline = support_history(repo, Config(), changed.claim.claim_id)
    assert timeline["last_statically_supported"] == base
    assert timeline["first_observed_drift"] == head
    assert support_history(repo, Config(), changed.claim.claim_id, 1)["last_statically_supported"] is None
    assert not pr_comparison(repo, Config(), head)[1].findings


@pytest.mark.parametrize("relative", [False, True])
def test_snapshot_accepts_noncanonical_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    repo, base, _ = repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    root = Path("snapshot") if relative else tmp_path / "alias" / ".." / "snapshot"
    assert materialize_snapshot(repo, base, root) == base
    assert "default=4" in (root / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("unsafe", ["../escape.txt", ".git/config", "file:stream", "/absolute.txt"])
def test_snapshot_still_rejects_unsafe_entries_with_noncanonical_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    repo, base, _ = repository(tmp_path)
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("safe.txt", "must not be extracted")
        output.writestr(unsafe, "unsafe")

    def open_archive(_path: Path) -> ZipFile:
        return ZipFile(archive)

    monkeypatch.setattr(snapshots, "ZipFile", open_archive)
    root = tmp_path / "alias" / ".." / "snapshot"
    with pytest.raises(ValueError, match="unsafe path"):
        materialize_snapshot(repo, base, root)
    assert not (root / "safe.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_scoped_review_reopens_on_source_change_and_never_hides_reports(tmp_path: Path) -> None:
    repo, _, _ = repository(tmp_path)
    scan = static_scan(repo, Config())
    finding = next(f for f in scan.findings if f.verdict == "default_changed")
    path = repo / ".docs-drift-reviews.json"
    record_review(
        scan, path, finding.claim.claim_id, ReviewKind.false_positive, "Maintainer checked source", "maintainer"
    )
    active = suppressed_ids(static_scan(repo, Config()), load_reviews(path))
    assert active == {finding.claim.claim_id}
    gate = evaluate_gate(scan, GatePolicy(), suppressed=active)
    assert gate.summary.status == "passed" and gate.summary.reviewed_drift == 1
    assert gate.summary.below_threshold_new_drift == 0
    assert finding in scan.findings and finding in gate.new_findings
    source(repo, 16)
    assert suppressed_ids(static_scan(repo, Config()), load_reviews(path)) == set()
    assert len(load_reviews(path).decisions) == 1
    with pytest.raises(ValueError, match="reason"):
        record_review(scan, path, finding.claim.claim_id, ReviewKind.stale, " ", "maintainer")
    with pytest.raises(ValueError, match="not present"):
        record_review(scan, path, "nonexistent", ReviewKind.stale, "review", "maintainer")


def test_stale_decision_stays_actionable_and_pr_suppression_uses_current_evidence(tmp_path: Path) -> None:
    repo, base, _ = repository(tmp_path)
    full, selected, comparison = pr_comparison(repo, Config(), base)
    finding = next(f for f in full.findings if f.verdict == "default_changed")
    path = repo / ".docs-drift-reviews.json"
    record_review(full, path, finding.claim.claim_id, ReviewKind.stale, "Please correct", "maintainer")
    assert not suppressed_ids(full, load_reviews(path))
    record_review(full, path, finding.claim.claim_id, ReviewKind.intentional, "Migration documentation", "maintainer")
    payload = write_pr_report(selected, comparison, tmp_path / "report", load_reviews(path), full_result=full)
    assert payload["fixable_edits"] == 0
    assert finding.claim.claim_id in payload["reviews"]


def test_ownership_precedence_coverage_and_missing_important_docs(tmp_path: Path) -> None:
    repo, _, _ = repository(tmp_path)
    (repo / "CODEOWNERS").write_text("* @ignored\n", encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github/CODEOWNERS").write_text("* @team\n/README.md @docs\n!unsafe @no\n", encoding="utf-8")
    owners, warnings = ownership(repo, ["README.md", "guide.md"])
    assert owners == {"README.md": ["@docs"], "guide.md": ["@team"]}
    assert warnings
    coverage = coverage_report(static_scan(repo, Config()), ["missing.md"])
    missing = next(row for row in coverage["documents"] if row["path"] == "missing.md")
    assert missing["status"] == "not-checked" and missing["important"]


def test_fix_branch_verifies_patch_and_preserves_source_checkout(tmp_path: Path) -> None:
    repo, _, head = repository(tmp_path)
    scan = static_scan(repo, Config())
    finding = next(f for f in scan.findings if f.verdict == "default_changed")
    destination = tmp_path / "proposal"
    result = fix_branch(repo, Config(), finding.claim.claim_id, "docs/fix", destination)
    assert result["verified"] and not result["pushed"]
    assert git(repo, "rev-parse", "HEAD") == head and not git(repo, "status", "--porcelain")
    assert "| `--workers` | `4` |" in (repo / "README.md").read_text()
    assert "| `--workers` | `8` |" in (destination / "README.md").read_text()
    assert not any(f.is_drift for f in static_scan(destination, Config()).findings)
    with pytest.raises(ValueError, match="absent"):
        fix_branch(repo, Config(), "nonexistent", "docs/no", tmp_path / "none")
    with pytest.raises(ValueError, match="branch name"):
        fix_branch(repo, Config(), finding.claim.claim_id, "--bad", tmp_path / "none")
    with pytest.raises(ValueError, match="new worktree"):
        fix_branch(repo, Config(), finding.claim.claim_id, "docs/no", repo / "nested")


def test_cli_workflows_and_invalid_revision(tmp_path: Path) -> None:
    repo, base, _ = repository(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["pr", "--repo", str(repo), "--base", base, "--report-dir", str(tmp_path / "pr")])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["fixable_edits"] == 1
    finding = next(f for f in static_scan(repo, Config()).findings if f.verdict == "default_changed")
    result = runner.invoke(
        app,
        [
            "review",
            "record",
            finding.claim.claim_id,
            "--repo",
            str(repo),
            "--decision",
            "intentional",
            "--reason",
            "Migration guide",
            "--reviewer",
            "Test",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["review", "list", "--repo", str(repo)])
    assert result.exit_code == 0 and finding.claim.claim_id in json.loads(result.output)["active"]
    result = runner.invoke(app, ["coverage", "--repo", str(repo), "--important", "missing.md"])
    assert result.exit_code == 0 and "not-checked" in result.output
    result = runner.invoke(app, ["timeline", finding.claim.claim_id, "--repo", str(repo), "--max-commits", "1"])
    assert result.exit_code == 0 and json.loads(result.output)["last_statically_supported"] is None
    result = runner.invoke(app, ["check", "--repo", str(repo), "--reviews", ".docs-drift-reviews.json"])
    assert result.exit_code == 0, result.output
    for revision in ["--help", "HEAD other", "", "nonexistent"]:
        with pytest.raises(ValueError):
            resolve_revision(repo, revision)


def test_action_pr_review_retains_full_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, _ = repository(tmp_path)
    scan = static_scan(repo, Config())
    finding = next(f for f in scan.findings if f.verdict == "default_changed")
    path = repo / ".docs-drift-reviews.json"
    record_review(scan, path, finding.claim.claim_id, ReviewKind.intentional, "Migration", "Reviewer")
    for key, value in {
        "GITHUB_WORKSPACE": str(repo),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "SCAN_BASE_REF": base,
        "SCAN_REVIEWS": path.name,
    }.items():
        monkeypatch.setenv(key, value)
    action_runner.main()
    output = Path(
        next(
            line.split("=", 1)[1]
            for line in (tmp_path / "output.txt").read_text().splitlines()
            if line.startswith("report-directory=")
        )
    )
    gate = json.loads((output / "gate.json").read_text())
    assert gate["status"] == "passed" and gate["reviewed_drift"] == 1
    full = json.loads((output / "scan.json").read_text())
    assert any(f["verdict"] == "default_changed" for f in full["findings"])
    assert json.loads((output / "pr/review.json").read_text())["fixable_edits"] == 0
    monkeypatch.setenv("SCAN_REVIEWS", "../outside.json")
    with pytest.raises(ValueError, match="Reviews"):
        action_runner.main()


def test_review_validation_dirty_checkout_and_uncertain_fix(tmp_path: Path) -> None:
    repo, _, _ = repository(tmp_path)
    path = repo / "bad.json"
    path.write_text('{"schema_version":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="review decisions"):
        load_reviews(path)
    with pytest.raises(ValueError, match="clean source"):
        fix_branch(repo, Config(), "missing", "docs/fix", tmp_path / "unused")
    path.unlink()
    (repo / "README.md").write_text("Use `--unrelated`.\n", encoding="utf-8")
    commit(repo, "uncertain claim")
    finding = next(f for f in static_scan(repo, Config()).findings if f.is_drift)
    with pytest.raises(ValueError, match="No certain rule edit"):
        fix_branch(repo, Config(), finding.claim.claim_id, "docs/no", tmp_path / "unused")
    assert not git(repo, "branch", "--list", "docs/no")
    with pytest.raises(ValueError, match="bounded"):
        support_history(repo, Config(), finding.claim.claim_id, 51)
    assert support_history(repo, Config(), "not-present")["last_statically_supported"] is None


def test_review_only_scopes_one_claim_and_html_escapes_doc_text(tmp_path: Path) -> None:
    repo, base, _ = repository(tmp_path)
    (repo / "README.md").write_text(
        "| Flag | Default |\n| --- | --- |\n| `--workers` | `4` |\n\nUse `--unknown`. <script>alert(1)</script>\n",
        encoding="utf-8",
    )
    commit(repo, "another claim")
    full, selected, comparison = pr_comparison(repo, Config(), base)
    finding = next(f for f in full.findings if f.verdict == "default_changed")
    path = repo / ".docs-drift-reviews.json"
    record_review(full, path, finding.claim.claim_id, ReviewKind.intentional, "Migration", "Reviewer")
    active = suppressed_ids(full, load_reviews(path))
    assert all(f.claim.claim_id not in active for f in full.findings if f.claim.value == "--unknown")
    report = tmp_path / "report"
    write_pr_report(selected, comparison, report, load_reviews(path), full_result=full)
    text = (report / "pr.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_fix_branch_never_runs_repository_filters_or_hooks(tmp_path: Path) -> None:
    repo, _, _ = repository(tmp_path)
    marker = tmp_path / "filter-ran"
    script = tmp_path / "filter.py"
    script.write_text(
        f"from pathlib import Path\nimport sys\nPath({str(marker)!r}).touch()\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    # Configure the synthetic filter only after committing the fixture.
    (repo / ".gitattributes").write_text("README.md filter=probe\n", encoding="utf-8")
    commit(repo, "declare filter")
    command = f'"{sys.executable}" "{script}"'
    git(repo, "config", "filter.probe.clean", command)
    git(repo, "config", "filter.probe.smudge", command)
    finding = next(f for f in static_scan(repo, Config()).findings if f.verdict == "default_changed")
    assert not marker.exists(), "static scan executed a filter"
    result = fix_branch(repo, Config(), finding.claim.claim_id, "docs/safe", tmp_path / "safe-proposal")
    assert result["verified"] and not marker.exists()
