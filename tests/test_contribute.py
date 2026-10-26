"""Contributor mode: deterministic, escaped issue drafts with patches, and the docs health card."""

import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.contribute import (
    CARD_END,
    CARD_START,
    ContributionReport,
    _short_ids,
    build_contributions,
    digest_markdown,
    health_card,
    health_card_markdown,
    issue_filename,
    issue_markdown,
)
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from tests.fixtures.build_fixtures import build_fixtures


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("contribute")
    build_fixtures(root)
    return root


@pytest.fixture(scope="module")
def py_cli(fixtures: Path) -> ScanResult:
    return run_scan(fixtures / "py-cli", Config())

# Assembled at runtime so secret scanners never see a key-shaped literal in the repository.
FAKE_SECRET = "abc" + "def" + "123456"


def _git(repo: Path, *args: str, date: str = "2026-01-01T00:00:00+00:00", author_date: str | None = None) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_AUTHOR_DATE=author_date or date,
        GIT_COMMITTER_DATE=date,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _renamed_twice(root: Path) -> Path:
    """The same renamed flag documented in two sections, so it cannot merge into one claim."""
    repo = root / "renamed"
    repo.mkdir()
    _git(repo, "init", "-q")
    readme = (
        "# tool\n\n## Usage\n\nPass `--port` to choose a port.\n\n## Reference\n\nThe `--port` flag picks the port.\n"
    )
    (repo / "README.md").write_bytes(readme.encode())
    source = 'import argparse\np = argparse.ArgumentParser()\nsub = p.add_subparsers()\ns = sub.add_parser("serve")\n'
    (repo / "cli.py").write_text(source + 's.add_argument("--port")\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "initial")
    (repo / "cli.py").write_text(source + 's.add_argument("--listen")\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "Rename port", date="2026-01-11T00:00:00+00:00")
    return repo


def _port_repo(root: Path, documents: int = 1, *, rename_date: str, author_date: str | None = None) -> Path:
    """Documents that each name `--port`, then a commit renaming the flag to `--listen`."""
    repo = root / "port"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "docs").mkdir()
    for number in range(documents):
        page = f"# Page {number}\n\nPass `--port` to choose a port.\n"
        (repo / "docs" / f"page{number}.md").write_bytes(page.encode())
    source = 'import argparse\np = argparse.ArgumentParser()\nsub = p.add_subparsers()\ns = sub.add_parser("serve")\n'
    (repo / "cli.py").write_text(source + 's.add_argument("--port")\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "initial", date="2025-10-01T00:00:00+00:00")
    (repo / "cli.py").write_text(source + 's.add_argument("--listen")\n', encoding="utf-8")
    _git(repo, "add", ".")
    commit = ["-c", "commit.gpgSign=false", "commit", "-q", "-m", "Rename port"]
    _git(repo, *commit, date=rename_date, author_date=author_date)
    return repo


def test_py_cli_drafts_are_good_first_issues_with_patches(py_cli: ScanResult) -> None:
    report = build_contributions(py_cli)
    assert (report.total_drift, report.eligible_drift, report.skipped_drift) == (4, 3, 1)
    titles = [issue.title for issue in report.issues]
    assert titles == [
        "Docs: replace renamed `--port` with `--listen`",
        "Docs: default for `--workers` is now 8",
        "Docs: replace renamed `migrate` with `db-upgrade`",
    ]
    first = report.issues[0]
    assert first.difficulty == "good-first-issue"
    assert first.labels == ["documentation", "docs-drift", "good first issue", "drift: renamed"]
    location = first.locations[0]
    assert (location.file, location.line, location.certain_fix) == ("README.md", 4, True)
    assert location.proposed_text == "- `serve` accepts `--listen` (default 8000)."
    assert location.stale_since is not None and location.age_days == 3
    assert first.patch is not None and "+- `serve` accepts `--listen` (default 8000)." in first.patch
    assert first.commits[0].summary == "Rename port to listen"
    assert first.verify[0] == "docs-drift explain claim_8ed4cec49e3a"
    assert first.verify[-1] == "docs-drift check --no-color"
    assert first.issue_id.startswith("issue_") and len(first.issue_id) == 22


def test_drafts_are_deterministic_and_round_trip(py_cli: ScanResult) -> None:
    once = build_contributions(py_cli).model_dump_json()
    assert once == build_contributions(py_cli).model_dump_json()
    assert ContributionReport.model_validate_json(once).issues[0].locations[0].line == 4
    assert digest_markdown(build_contributions(py_cli)) == digest_markdown(build_contributions(py_cli))


def test_low_confidence_findings_join_their_section(py_cli: ScanResult) -> None:
    report = build_contributions(py_cli, min_confidence=0)
    grouped = next(issue for issue in report.issues if len(issue.locations) > 1)
    assert grouped.grouping == "section"
    assert [loc.line for loc in grouped.locations] == [6, 7]
    assert grouped.title == "Docs: fix 2 stale references in README.md § py-cli"
    # The missing variable has no certain edit, so the issue needs investigation.
    assert grouped.difficulty == "medium"
    assert "good first issue" not in grouped.labels
    assert grouped.locations[1].proposed_text is None
    assert "Replace 'APP_DEBUG' with 'APP_LOG_LEVEL'" in issue_markdown(grouped)


def test_limit_and_threshold(py_cli: ScanResult) -> None:
    limited = build_contributions(py_cli, limit=1)
    assert len(limited.issues) == 1 and limited.truncated
    assert "raise `--limit`" in digest_markdown(limited)
    assert build_contributions(py_cli, min_confidence=1).issues == []


def test_same_rename_in_two_sections_is_one_issue(tmp_path: Path) -> None:
    result = run_scan(_renamed_twice(tmp_path), Config())
    report = build_contributions(result)
    assert len(report.issues) == 1, [i.title for i in report.issues]
    issue = report.issues[0]
    assert issue.grouping == "rename"
    assert [loc.line for loc in issue.locations] == [5, 9]
    assert issue.title == "Docs: replace renamed `--port` with `--listen` (2 places)"
    assert issue.difficulty == "easy"  # Certain, but more than one line.
    assert issue.patch is not None
    added = [line for line in issue.patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert added == ["+Pass `--listen` to choose a port.", "+The `--listen` flag picks the port."]


def test_links_are_pinned_to_head_only_for_github_remotes(py_cli: ScanResult) -> None:
    remote = py_cli.model_copy(update={"remote_url": "git@github.com:acme/tool.git"})
    issue = build_contributions(remote).issues[0]
    assert issue.locations[0].url == f"https://github.com/acme/tool/blob/{py_cli.head_sha}/README.md#L4"
    assert issue.commits[0].url == f"https://github.com/acme/tool/commit/{issue.commits[0].sha}"
    assert f"[README.md:4](https://github.com/acme/tool/blob/{py_cli.head_sha}/README.md#L4)" in issue_markdown(issue)
    for url in ("https://user:token@github.com/acme/tool.git", "https://gitlab.com/acme/tool.git", None):
        plain = build_contributions(py_cli.model_copy(update={"remote_url": url})).issues[0]
        assert plain.locations[0].url is None and plain.commits[0].url is None
    separate = remote.model_copy(update={"docs_repo_path": py_cli.repo_path})
    assert build_contributions(separate).issues[0].locations[0].url is None


def test_untrusted_text_is_escaped_and_secrets_masked(py_cli: ScanResult) -> None:
    hostile = py_cli.model_copy(deep=True)
    finding = next(f for f in hostile.findings if f.claim.value == "--port" and f.is_drift)
    finding.doc_text = f"<script>alert(1)</script> @maintainers [x](javascript:y) ```` api_key = {FAKE_SECRET}"
    finding.introduced_by = finding.introduced_by.model_copy(update={"summary": "<img src=x> @everyone"})  # type: ignore[union-attr]
    issue = next(i for i in build_contributions(hostile).issues if i.locations[0].line == 4)
    markdown = issue_markdown(issue)
    assert "<script>" not in markdown and "<img" not in markdown
    assert "@maintainers" not in markdown and "@everyone" not in markdown
    assert FAKE_SECRET not in markdown and "[REDACTED]" in markdown
    assert "&lt;img src=x&gt; &#64;everyone" in markdown


def test_short_ids_extend_until_unique() -> None:
    first = "claim_" + "a" * 64
    second = "claim_" + "a" * 20 + "b" * 44
    findings = [type("F", (), {"claim": type("C", (), {"claim_id": claim_id})()})() for claim_id in (first, second)]
    short = _short_ids(findings)
    assert short[first] != short[second]
    assert first.startswith(short[first]) and second.startswith(short[second])


def test_health_card_never_names_stale_values(py_cli: ScanResult, fixtures: Path) -> None:
    card = health_card(py_cli)
    assert (card.drift, card.issue_drafts, card.good_first_issues, card.overdue) == (3, 3, 3, 0)
    assert card.oldest_drift is not None and card.oldest_drift.age_days == 3
    assert card.stale_documents[0].path == "README.md"
    text = health_card_markdown(card)
    assert text.startswith(CARD_START) and text.rstrip().endswith(CARD_END)
    for stale in ("--port", "migrate", "--workers", "APP_DEBUG"):
        assert stale not in text
    clean = health_card(run_scan(fixtures / "clean-repo", Config()))
    assert clean.drift == 0 and clean.oldest_drift is None and clean.good_first_issues == 0
    assert "Oldest drift" not in health_card_markdown(clean)


def test_contribute_command_outputs(fixtures: Path, tmp_path: Path) -> None:
    repo = str(fixtures / "py-cli")
    digest = CliRunner().invoke(app, ["contribute", "--repo", repo])
    assert digest.exit_code == 0, digest.output
    assert digest.output.startswith("# Documentation contribution opportunities")
    as_json = CliRunner().invoke(app, ["contribute", "--repo", repo, "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    assert len(json.loads(as_json.output)["issues"]) == 3
    card = CliRunner().invoke(app, ["contribute", "--summary", "--repo", repo])
    assert card.exit_code == 0 and CARD_START in card.output
    card_json = CliRunner().invoke(app, ["contribute", "--summary", "--repo", repo, "--format", "json"])
    assert json.loads(card_json.output)["good_first_issues"] == 3
    sarif = CliRunner().invoke(app, ["contribute", "--repo", repo, "--format", "sarif"])
    assert sarif.exit_code == 2
    output = tmp_path / "digest.md"
    written = CliRunner().invoke(app, ["contribute", "--repo", repo, "--output", str(output)])
    assert written.exit_code == 0 and output.read_text(encoding="utf-8") == digest.output


def test_out_dir_writes_one_draft_per_issue(fixtures: Path, tmp_path: Path, py_cli: ScanResult) -> None:
    destination = tmp_path / "drafts"
    args = ["contribute", "--repo", str(fixtures / "py-cli"), "--out-dir", str(destination), "--format", "json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    report = build_contributions(py_cli)
    expected = [issue_filename(n, issue) for n, issue in enumerate(report.issues, 1)]
    assert sorted(p.name for p in destination.iterdir()) == sorted([*expected, "issues.json"])
    assert expected[0].startswith("001-docs-replace-renamed-port-with-listen-")
    assert (destination / expected[0]).read_text(encoding="utf-8") == issue_markdown(report.issues[0])
    assert "Wrote 3 issue drafts" in result.output


def test_issue_draft_shows_one_utc_landing_date_per_commit(tmp_path: Path) -> None:
    # Authored 2025-11-12 local (2025-11-11 UTC), rebased onto the branch 2026-01-11 local (2026-01-10 UTC).
    repo = _port_repo(tmp_path, rename_date="2026-01-11T02:00:00+05:30", author_date="2025-11-12T02:00:00+05:30")
    report = build_contributions(run_scan(repo, Config()))
    issue = report.issues[0]
    location = issue.locations[0]
    assert location.stale_since == issue.commits[0].date == date(2026, 1, 10)
    assert report.scan_date == date(2026, 1, 10)
    text = issue_markdown(issue)
    assert "(2026-01-10)" in text and "stale since 2026-01-10 (0 days)" in text
    assert "2025-11-1" not in text


def test_health_card_counts_every_document_with_drift(tmp_path: Path) -> None:
    repo = _port_repo(tmp_path, documents=8, rename_date="2026-01-11T00:00:00+00:00")
    card = health_card(run_scan(repo, Config()))
    assert (card.drift, card.documents_with_drift, len(card.stale_documents)) == (8, 8, 5)
    assert "8 drift findings in 8 documents" in health_card_markdown(card)


def test_health_card_uses_one_threshold_and_counts_drafts_beyond_the_limit(py_cli: ScanResult) -> None:
    strict = health_card(py_cli, build_contributions(py_cli, min_confidence=1))
    assert (strict.drift, strict.documents_with_drift, strict.overdue, strict.issue_drafts) == (0, 0, 0, 0)
    assert strict.oldest_drift is None and strict.stale_documents == []
    limited = build_contributions(py_cli, limit=1)
    assert len(limited.issues) == 1 and limited.available_issues == 3
    card = health_card(py_cli, limited)
    assert (card.issue_drafts, card.good_first_issues) == (3, 3)
    assert "Issue drafts ready: 3, of which 3" in health_card_markdown(card)


def test_contribute_summary_honours_min_confidence_and_limit(fixtures: Path) -> None:
    repo = str(fixtures / "py-cli")
    args = ["contribute", "--summary", "--repo", repo, "--format", "json"]
    limited = json.loads(CliRunner().invoke(app, [*args, "--limit", "1"]).output)
    assert (limited["drift"], limited["issue_drafts"], limited["good_first_issues"]) == (3, 3, 3)
    strict = json.loads(CliRunner().invoke(app, [*args, "--min-confidence", "1"]).output)
    assert (strict["drift"], strict["issue_drafts"], strict["documents_with_drift"]) == (0, 0, 0)
