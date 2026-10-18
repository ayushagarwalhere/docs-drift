"""Compare independent builds and check the hand-authored key against literal history."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts.measure import load_key
from tests.fixtures.build_fixtures import _environment, build_fixtures, repositories


def git(repo: Path, *args: str) -> str:
    return (
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode("utf-8").strip()
    )


@pytest.fixture(scope="module")
def builds(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, tuple[str, ...]]]:
    first = tmp_path_factory.mktemp("fixtures first") / "repos"
    second = tmp_path_factory.mktemp("fixtures second") / "repos"
    first_history = build_fixtures(first)
    caller_config = second.parent / "caller.gitconfig"
    caller_config.write_text(
        "[core]\n autocrlf = true\n filemode = true\n[i18n]\n commitEncoding = ISO-8859-1\n[commit]\n gpgSign = true\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(caller_config))
        monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
        monkeypatch.setenv("GIT_INDEX_FILE", str(second.parent / "caller-index"))
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Unexpected Caller")
        second_history = build_fixtures(second)
    assert first_history == second_history
    return first, second, first_history


def test_identical_histories_and_worktrees(builds: tuple[Path, Path, dict[str, tuple[str, ...]]]) -> None:
    first, second, histories = builds
    assert set(histories) == {"py-cli", "fastapi-svc", "node-tool", "clean-repo"}
    for name, expected in histories.items():
        assert tuple(git(first / name, "rev-list", "--reverse", "HEAD").splitlines()) == expected
        assert tuple(git(second / name, "rev-list", "--reverse", "HEAD").splitlines()) == expected
        assert git(first / name, "status", "--porcelain") == ""
        assert git(second / name, "status", "--porcelain") == ""
        for filename in git(first / name, "ls-files").splitlines():
            assert (first / name / filename).read_bytes() == (second / name / filename).read_bytes()
            assert b"\r\n" not in (first / name / filename).read_bytes()


def test_fixed_commit_metadata(builds: tuple[Path, Path, dict[str, tuple[str, ...]]]) -> None:
    first, _, histories = builds
    for specification in repositories():
        for sha, commit in zip(histories[specification.name], specification.commits, strict=True):
            actual = git(first / specification.name, "show", "-s", "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B", sha)
            fields = actual.splitlines()
            assert fields[:2] == [commit.author, commit.email]
            assert fields[3:5] == [commit.author, commit.email]
            assert fields[6:] == [commit.message]
            # Git versions may render the same UTC offset as Z or +00:00.
            assert datetime.fromisoformat(fields[2]) == datetime.fromisoformat(commit.date)
            assert datetime.fromisoformat(fields[5]) == datetime.fromisoformat(commit.date)


def test_key_locations_and_introducing_commits(builds: tuple[Path, Path, dict[str, tuple[str, ...]]]) -> None:
    first, _, histories = builds
    key = load_key(Path(__file__).parent / "fixtures/answer_key.json")
    for claim in (*key.findings, *key.non_findings):
        lines = (first / claim.repo / claim.doc_path).read_text(encoding="utf-8").splitlines()
        assert claim.claim_text in lines[claim.line - 1], claim
    for finding in key.findings:
        repo = first / finding.repo
        assert finding.introduced_by in histories[finding.repo][1:]
        assert git(repo, "diff", f"{finding.introduced_by}^", finding.introduced_by, "--", finding.doc_path) == ""
        assert git(repo, "diff", "--name-only", f"{finding.introduced_by}^", finding.introduced_by)
    assert all(finding.repo != "clean-repo" for finding in key.findings)


def test_stale_readmes_are_unchanged(builds: tuple[Path, Path, dict[str, tuple[str, ...]]]) -> None:
    first, _, histories = builds
    for name in ("py-cli", "fastapi-svc", "node-tool"):
        assert (
            git(first / name, "show", f"{histories[name][0]}:README.md")
            == (first / name / "README.md").read_text().strip()
        )
    service = (first / "fastapi-svc/service.py").read_text()
    assert '@app.post("/login")' not in service
    assert "timeout_seconds: int = 60" in service
    assert "def build_app():" in service
    node = (first / "node-tool/src/cli.ts").read_text()
    assert "--dry-run" not in node
    package = json.loads((first / "node-tool/package.json").read_text())
    assert package["scripts"] == {"build": "tsc"}


def test_builder_refuses_existing_destinations(tmp_path: Path) -> None:
    destination = tmp_path / "py-cli"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="Choose a fresh output directory"):
        build_fixtures(tmp_path)
    assert marker.read_text() == "keep"
    assert not (tmp_path / "node-tool").exists()


def test_git_environment_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bad inherited Git settings must not redirect writes into the checkout or change the objects.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-the-fixture"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "not-the-index"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Caller")
    environment = _environment()
    assert "GIT_DIR" not in environment
    assert "GIT_INDEX_FILE" not in environment
    assert "GIT_DEFAULT_HASH" not in environment
    assert "GIT_AUTHOR_NAME" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
