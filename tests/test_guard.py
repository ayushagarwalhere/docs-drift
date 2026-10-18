"""Exercise actual Git indexes and staged blobs without creating commits."""

import subprocess
from pathlib import Path

import pytest

from scripts.guard import MAX_FILE_BYTES, inspect_staged, main, secret_findings, staged_files


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    return tmp_path


def stage(repo: Path, filename: str, content: bytes) -> Path:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(["git", "-C", str(repo), "add", "--", filename], check=True)
    return path


def test_clean_index(repo: Path) -> None:
    assert staged_files(repo) == []
    assert main(repo) == 0


@pytest.mark.parametrize("filename", [".env", "nested/.env.local", "nested/.ENV", ".env.example"])
def test_env_files_are_blocked(repo: Path, filename: str) -> None:
    stage(repo, filename, b"PORT=8000\n")
    assert any(".env files must not be committed" in finding for finding in inspect_staged(repo))


@pytest.mark.parametrize("size,blocked", [(MAX_FILE_BYTES, False), (MAX_FILE_BYTES + 1, True)])
def test_size_boundary(repo: Path, size: int, blocked: bool) -> None:
    stage(repo, "artifact.bin", b"\0" * size)
    assert bool(inspect_staged(repo)) is blocked


@pytest.mark.parametrize(
    "prefix,suffix",
    [("sk-ant-", "a" * 32), ("ghp_", "b" * 36), ("github_pat_", "c" * 82), ("AKIA", "D" * 16), ("ASIA", "E" * 16)],
)
def test_secrets_blocked_without_echoing_values(
    repo: Path, capsys: pytest.CaptureFixture[str], prefix: str, suffix: str
) -> None:
    token = prefix + suffix
    stage(repo, "nested dir/key.txt", b"safe first line\r\n" + token.encode() + b"\r\n")
    assert main(repo) == 1
    output = capsys.readouterr().err
    assert "key.txt:2: possible" in output
    assert token not in output


def test_staged_secret_cannot_be_hidden_by_working_tree(repo: Path) -> None:
    path = stage(repo, "settings.txt", ("ghp_" + "a" * 36).encode())
    path.write_text("clean now\n", encoding="utf-8")
    assert inspect_staged(repo)


def test_unstaged_content_is_not_scanned(repo: Path) -> None:
    path = stage(repo, "settings.txt", b"clean staged contents\n")
    path.write_text("ghp_" + "a" * 36, encoding="utf-8")
    (repo / ".env").write_text("not staged\n", encoding="utf-8")
    assert inspect_staged(repo) == []


def test_paths_with_spaces_and_unicode_and_lf_normalization(repo: Path) -> None:
    path = stage(repo, "nested dir/caf\u00e9.txt", b"ordinary text\r\n")
    files = staged_files(repo)
    assert [file.path for file in files] == [path.relative_to(repo)]
    blob = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", files[0].object_id],
        check=True,
        capture_output=True,
    ).stdout
    assert blob == b"ordinary text\n"
    assert inspect_staged(repo) == []


def test_removed_staged_file_is_ignored(repo: Path) -> None:
    stage(repo, "removed.txt", b"clean\n")
    subprocess.run(["git", "-C", str(repo), "rm", "--cached", "--", "removed.txt"], check=True)
    assert inspect_staged(repo) == []


def test_renamed_staged_file_is_checked(repo: Path) -> None:
    stage(repo, "old.txt", b"clean\n")
    subprocess.run(["git", "-C", str(repo), "mv", "--", "old.txt", ".env.local"], check=True)
    assert any(".env.local" in finding for finding in inspect_staged(repo))


def test_non_repository_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A test scratch directory may live inside the checkout; stop Git walking into its parent.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert main(tmp_path) == 1
    assert "inside a Git repository" in capsys.readouterr().err


def test_prefix_mentions_and_ordinary_text_are_allowed() -> None:
    assert secret_findings(Path("guide.md"), b"Prefixes: sk-ant- ghp_ github_pat_ AKIA ASIA\n") == []
