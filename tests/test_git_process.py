"""Bounded Git process failures never expose stderr or bypass cleanup."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from docs_drift.web import git as git_module
from docs_drift.web.git import JobError, git, git_environment
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository


def test_real_git_uses_isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.pwn")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!untrusted")
    environment = git_environment(tmp_path)
    assert "GITHUB_TOKEN" not in environment and "GIT_CONFIG_COUNT" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    repo = tmp_path / "repo"
    git(["init", "--quiet", "--template=", str(repo)], tmp_path, Settings(), time.monotonic() + 10)
    assert (repo / ".git/config").is_file()


@pytest.mark.parametrize("failure", ["timeout", "disk", "exit", "output"])
def test_git_budgets_cleanup_and_opaque_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    calls: list[Any] = []
    process = SimpleNamespace(
        poll=lambda: None if failure in {"timeout", "disk"} else 1, returncode=1 if failure == "exit" else 0, stdin=None
    )

    def popen(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "credential.helper=" in command
        assert "protocol.file.allow=never" in command
        kwargs["stderr"].write(b"PRIVATE REPOSITORY CONTENT")
        kwargs["stdout"].write(b"too-long")
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(git_module, "kill_process", calls.append)
    monkeypatch.setattr(git_module, "disk_bytes", lambda root: 100 if failure == "disk" else 0)
    monkeypatch.setattr(git_module, "MAX_GIT_OUTPUT", 2)
    with pytest.raises(JobError) as raised:
        git(["status"], tmp_path, Settings(max_bytes=10), time.monotonic() + (-1 if failure == "timeout" else 10))
    assert (
        raised.value.kind
        == {"timeout": "timeout", "disk": "too_large", "exit": "clone", "output": "too_large"}[failure]
    )
    assert "PRIVATE" not in str(raised.value)
    assert calls == [process]


def test_git_stdin_and_success_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io  # noqa: PLC0415

    class Input(io.BytesIO):
        def close(self) -> None:
            self.saved = self.getvalue()
            super().close()

    stream = Input()
    process = SimpleNamespace(poll=lambda: 0, returncode=0, stdin=stream)

    def popen(command: list[str], **kwargs: Any) -> Any:
        kwargs["stdout"].write(b"okay")
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(git_module, "kill_process", lambda value: None)
    assert git(["status"], tmp_path, Settings(), time.monotonic() + 10, stdin=b"patterns\n") == b"okay"
    assert stream.saved == b"patterns\n"


@pytest.mark.parametrize(
    "ref, reply, expected",
    [
        (None, b"a" * 40 + b"\tHEAD\n", "a" * 40),
        ("v1", b"a" * 40 + b"\trefs/tags/v1\n" + b"b" * 40 + b"\trefs/tags/v1^{}\n", "b" * 40),
        ("A" * 40, b"", "a" * 40),
        (None, b"", None),
        (None, b"bad\tHEAD\n", None),
    ],
)
def test_resolved_refs_are_pinned_and_annotated_tags_are_dereferenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str | None, reply: bytes, expected: str | None
) -> None:
    calls = []

    def remote(args: list[str], *other: Any) -> bytes:
        calls.append(args)
        assert args[:2] == ["ls-remote", "--exit-code"]
        assert args[2] == "https://github.com/example/tool"
        return reply

    monkeypatch.setattr(git_module, "git", remote)
    repository = validate_repository("https://github.com/example/tool", ref)
    if expected is None:
        with pytest.raises(JobError):
            git_module.resolve(repository, tmp_path, Settings(), time.monotonic() + 1)
    else:
        assert git_module.resolve(repository, tmp_path, Settings(), time.monotonic() + 1) == expected
    if ref and len(ref) == 40:
        assert not calls


@pytest.mark.parametrize("system", ["nt", "posix"])
def test_child_cleanup_uses_process_tree_on_windows_and_waits(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    calls: list[Any] = []
    process = SimpleNamespace(
        poll=lambda: None, pid=7, kill=lambda: calls.append("kill"), wait=lambda **kwargs: calls.append(kwargs)
    )
    monkeypatch.setattr(git_module, "os", SimpleNamespace(name=system))
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    git_module.kill_process(cast(subprocess.Popen[bytes], process))
    assert calls[-1] == {"timeout": 10}
    assert calls[0] == (["taskkill", "/PID", "7", "/T", "/F"] if system == "nt" else "kill")
