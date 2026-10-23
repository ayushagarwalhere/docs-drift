"""Worker cache protocol, Git argument/path guards, render limits and explicit UI states."""

import asyncio
import json
import time
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.models import ScanResult
from docs_drift.report import render
from docs_drift.web.app import create_app
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.git import JobError, checkout
from docs_drift.web.jobs import Job, LocalQueue, Store
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository
from docs_drift.web.worker import execute


def fixture_result() -> ScanResult:
    return ScanResult.model_validate_json(files("docs_drift").joinpath("web/examples/py-cli.json").read_text())


def test_all_page_states_and_empty(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        for state in ("queued", "cloning", "indexing", "matching", "history", "failed"):
            job = Job(
                "state-" + state,
                "https://github.com/a/b",
                status=state,
                error={"kind": "scan", "message": "Could not finish", "hint": "Try an example"}
                if state == "failed"
                else None,
            )
            app.state.store.save(job)
            response = client.get("/scan/" + job.scan_id)
            assert response.status_code == 200
            assert (
                "Could not finish" in response.text
                if state == "failed"
                else "Checking your documentation" in response.text
            )
        assert "No drift found" in client.get("/scan/example-clean-repo").text
        assert client.get("/scan/missing").status_code == 404
        assert client.get("/api/scans/state-queued/report.html").status_code == 409
        assert client.get("/api/scans/state-queued/graph").status_code == 409
        assert client.get("/api/scans/state-queued/result.json").status_code == 409


def test_worker_cache_hit_stops_before_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = fixture_result()
    settings = Settings(data_dir=tmp_path)
    store = Store(settings)
    original = Job("original", "https://github.com/a/b", status="done", result=result)
    store.save(original)

    class Input:
        writes: ClassVar[list[bytes]] = []

        def write(self, value: bytes) -> None:
            self.writes.append(value)

        async def drain(self) -> None:
            pass

    class Output:
        async def readline(self) -> bytes:
            return json.dumps({"resolved_sha": result.head_sha}).encode() + b"\n"

    class Process:
        stdin = Input()
        stdout = Output()
        returncode: int | None = None
        pid = 99999999

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    process = Process()

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def exercise() -> None:
        queue = LocalQueue(settings, store)
        job = Job("next", original.repo_url)
        await queue.worker(job, tmp_path)
        assert job.cached and job.result == result
        assert b"stop\n" in process.stdin.writes and b"continue\n" not in process.stdin.writes

    asyncio.run(exercise())


def test_sparse_checkout_never_selects_symlinks_env_or_submodules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def git(args: list[str], root: Path, settings: Settings, deadline: float, *, stdin: bytes | None = None) -> bytes:
        calls.append((args, stdin))
        if "ls-tree" in args:
            return (
                b"100644 blob abc\tREADME.md\0"
                + b"120000 blob def\tleak.py\0"
                + b"100644 blob aaa\t.env\0"
                + b"160000 commit aaa\tsubmodule\0"
            )
        return b""

    monkeypatch.setattr("docs_drift.web.git.git", git)
    checkout(validate_repository("https://github.com/a/b"), "a" * 40, tmp_path, Settings(), time.monotonic() + 10)
    sparse = next(stdin for args, stdin in calls if "--stdin" in args)
    assert sparse == b"/README.md\n"
    assert all(isinstance(args, list) for args, _ in calls)
    assert any("protocol.file.allow" in args for args, _ in calls)


def test_clone_path_and_file_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(args: list[str], *unused: Any, **kwargs: Any) -> bytes:
        return b"100644 blob abc\tbad\nfile.py\0" if "ls-tree" in args else b""

    monkeypatch.setattr("docs_drift.web.git.git", git)
    with pytest.raises(JobError, match="unsupported filenames"):
        checkout(validate_repository("https://github.com/a/b"), "a" * 40, tmp_path, Settings(), time.monotonic() + 10)


def test_all_serializers_scrub_secrets() -> None:
    result = fixture_result()
    result.findings[0].evidence[0].text = 'password="NEVER_LEAK_THIS"'
    result.findings[0].doc_text = 'api_key="NEVER_LEAK_THIS"'
    for format in ("terminal", "json", "html", "markdown", "sarif", "junit", "github"):
        assert "NEVER_LEAK_THIS" not in render(result, format)
    assert "NEVER_LEAK_THIS" in result.findings[0].doc_text


def test_click_options_aliases_and_environment_are_static(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text("""import click
@click.command()
@click.option("--line-numbers", "-n", is_flag=True)
@click.option("--theme", envvar="RICH_THEME", default="monokai")
@click.version_option("--version")
def main():
    raise RuntimeError("MUST NEVER EXECUTE")
""")
    symbols = build_index(tmp_path, Config()).symbols
    flags = {s.name: s for s in symbols if s.kind == "cli_flag"}
    assert flags["--line-numbers"].aliases == ["-n"]
    assert flags["--theme"].default == "monokai"
    assert any(s.name == "RICH_THEME" and s.kind == "env_var" for s in symbols)
    assert any(s.name == "main" and s.kind == "cli_command" for s in symbols)
    assert not any(s.name == "--version" and s.kind == "cli_command" for s in symbols)


def test_disk_watchdog_cancels_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned: list[bool] = []

    async def worker(self: LocalQueue, job: Job, workspace: Path) -> None:
        try:
            await asyncio.sleep(10)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(LocalQueue, "worker", worker)
    (tmp_path / "oversized").write_bytes(b"1234")
    settings = Settings(data_dir=tmp_path / "data", max_bytes=2)

    async def exercise() -> None:
        queue = LocalQueue(settings, Store(settings))
        with pytest.raises(JobError, match="200 MB scan limit"):
            await queue.bounded_worker(Job("test", "https://github.com/a/b"), tmp_path)

    asyncio.run(exercise())
    assert cleaned == [True]


def test_readonly_cleanup_and_scope_guard(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    pack = workspace / "packfile"
    pack.write_bytes(b"git pack")
    pack.chmod(0o400)
    remove_workspace(tmp_path, workspace)
    assert not workspace.exists()
    with pytest.raises(ValueError, match="outside"):
        remove_workspace(tmp_path / "other", tmp_path)


def test_public_scan_bypasses_repo_supplied_index_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def scan(*args: Any, **kwargs: Any) -> ScanResult:
        seen.update(kwargs)
        return fixture_result()

    monkeypatch.setattr("docs_drift.scanning.run_scan", scan)
    result = execute(tmp_path, Settings(), lambda *args: None)
    assert seen["no_cache"] is True and result.docs


def test_external_index_cache_is_neither_read_nor_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repo / ".docs-drift").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    (repo / "cli.py").write_text('import argparse\np=argparse.ArgumentParser()\np.add_argument("--safe")')

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("External cache must not be accessed")

    monkeypatch.setattr("docs_drift.index._read_cache", forbidden)
    monkeypatch.setattr("docs_drift.index._write_cache", forbidden)
    assert any(s.name == "--safe" for s in build_index(repo, Config()).symbols)
    assert not list(outside.iterdir())
