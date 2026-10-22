"""Git history tests use plain text files and never import repository content."""

import os
import subprocess
from pathlib import Path

from docs_drift.config import HistoryConfig
from docs_drift.history import GitHistory


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=merged)
    return result.stdout.strip()


def _commit(repo: Path, body: str, date: str) -> str:
    (repo / "app.py").write_text(body, encoding="utf-8")
    _git(repo, "add", "app.py")
    env = {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    _git(repo, "commit", "-m", "text update", env=env)
    return _git(repo, "rev-parse", "HEAD")


def test_history_finds_removal_replacement_and_author(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, 'parser.add_argument("--port")\n', "2026-01-01T00:00:00+00:00")
    sha = _commit(repo, 'parser.add_argument("--listen")\n', "2026-01-02T00:00:00+00:00")

    match = GitHistory(repo).search("--port", ("--listen",))

    assert match.commit is not None
    assert match.commit.sha == sha
    assert match.commit.author_name == "Test Author"
    assert match.rename is not None
    assert match.rename.file == "app.py"
    assert "--listen" in match.rename.hunk_excerpt


def test_non_git_repository_returns_warning(tmp_path: Path) -> None:
    match = GitHistory(tmp_path).search("missing")
    assert match.commit is None
    assert match.warning and "isn't a git repository" in match.warning


def test_unrelated_addition_in_same_hunk_is_not_a_rename(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, 'print("tool --help")\n', "2026-01-01T00:00:00+00:00")
    sha = _commit(tmp_path, 'parser.add_argument("--repo")\n', "2026-01-02T00:00:00+00:00")

    match = GitHistory(tmp_path).search("--help", ("--repo",))

    assert match.commit is not None and match.commit.sha == sha
    assert match.rename is None


def test_document_blame_is_utc_cached_and_email_is_opt_in(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# Usage\nUse the tool.\n")
    _git(tmp_path, "add", "README.md")
    original = _commit(tmp_path, "value = 1\n", "2026-01-01T00:00:00+00:00")
    _commit(tmp_path, "value = 2\n", "2026-01-02T00:00:00+00:00")
    history = GitHistory(tmp_path)
    touched = history.last_touched("README.md", 2)
    assert touched is not None and touched.sha == original
    assert touched.author_email is None and touched.date.isoformat().endswith("+00:00")
    assert history.last_touched("README.md", 2) is touched
    visible = GitHistory(tmp_path, HistoryConfig(show_emails=True)).last_touched("README.md", 2)
    assert visible is not None and visible.author_email == "test@example.invalid"
    assert history.last_touched("README.md", 200) is None
    assert history.last_touched("README.md", 200) is None


def test_history_query_can_be_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "value = 1\n", "2026-01-01T00:00:00+00:00")
    assert GitHistory(repo, HistoryConfig(max_commits=0)).search("value").commit is None
