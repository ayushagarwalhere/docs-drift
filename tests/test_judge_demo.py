"""The judge's offline path, reproduced scan, and failure/execution boundaries."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docs_drift.models import ScanResult
from docs_drift.web.app import HERE, create_app
from docs_drift.web.git import JobError
from docs_drift.web.jobs import Job
from docs_drift.web.settings import Settings
from docs_drift.web.worker import execute
from scripts.build_judge_demo import NAME, README, build_demo
from tests.test_web import wait


def test_offline_judge_path_reproduces_scan_and_fix(tmp_path: Path) -> None:
    fresh = build_demo(tmp_path / "fixture")
    saved = ScanResult.model_validate_json((HERE / "examples/judge-fixture.json").read_text(encoding="utf-8"))
    assert len(fresh.findings) == len(saved.findings) == 1
    finding = fresh.findings[0]
    assert finding.is_drift and finding.claim.value == "--port" and finding.suggestion == "--listen"
    assert finding.introduced_by is not None
    assert finding.introduced_by == saved.findings[0].introduced_by
    assert finding.evidence == saved.findings[0].evidence
    assert fresh.head_sha == saved.head_sha == finding.introduced_by.sha
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", live_enabled=False))) as client:
        assert 'href="/scan/example-judge"' in client.get("/").text
        page = client.get("/scan/example-judge")
        assert page.status_code == 200
        for expected in (
            "SYNTHETIC FIXTURE",
            "not a real-world accuracy result",
            "README.md:3",
            "cli.py",
            "--port",
            "--listen",
            finding.introduced_by.short_sha,
            "Rename port to listen without updating README",
            "Proposed documentation fix",
            "not applied",
        ):
            assert expected in page.text
        assert "github.com/docs-drift/judge" not in page.text
        assert client.get("/api/scans/example-judge/result.json").json()["head_sha"] == fresh.head_sha
        assert "--listen" in client.get("/api/scans/example-judge/report.md").text
        paused = client.post("/api/scans", json={"repo_url": "https://github.com/a/b"})
        assert paused.status_code == 503 and "examples" in paused.json()["detail"]
    repo = tmp_path / "fixture" / NAME
    (repo / "README.md").write_text(README.replace("--port", "--listen"), encoding="utf-8")
    assert not any(f.is_drift for f in execute(repo, Settings(), lambda *args: None).findings)


def test_scan_failure_has_visible_recovery_and_no_report(tmp_path: Path) -> None:
    async def fail(job: Job, workspace: Path) -> None:
        raise JobError("clone", "Repository could not be fetched.", "Try the synthetic fixture or check the URL.")

    with TestClient(create_app(Settings(data_dir=tmp_path), test_runner=fail)) as client:
        invalid = client.post("/api/scans", json={"repo_url": "file:///untrusted"})
        assert invalid.status_code == 422
        ident = client.post("/api/scans", json={"repo_url": "https://github.com/a/b"}).json()["scan_id"]
        assert wait(client, ident)["status"] == "failed"
        page = client.get(f"/scan/{ident}").text
        assert 'role="alert"' in page and "Repository could not be fetched." in page
        assert "Try the synthetic fixture" in page and 'href="/"' in page
        assert client.get(f"/api/scans/{ident}/report.html").status_code == 409
    assert not list((tmp_path / "work").iterdir())


def test_repository_payloads_are_read_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_demo(tmp_path)
    repo = tmp_path / NAME
    marker = tmp_path / "EXECUTED"
    payload = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n'
    for name in ("setup.py", "conftest.py", "sitecustomize.py", "cli.py"):
        (repo / name).write_text(payload, encoding="utf-8")
    (repo / "package.json").write_text('{"scripts":{"postinstall":"touch EXECUTED"}}', encoding="utf-8")
    (repo / ".docs-drift.toml").write_text("[llm]\nenabled=true\n", encoding="utf-8")
    hook = repo / ".git/hooks/post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho executed > "{marker.as_posix()}"\n', encoding="utf-8")
    hook.chmod(0o755)
    original = subprocess.Popen
    calls = []

    def only_git(args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        assert args[0] == "git" and not kwargs.get("shell")
        assert "core.fsmonitor=false" in args
        if "log" in args or "diff" in args:
            assert any(str(arg).startswith("core.hooksPath=") for arg in args)
            assert "--no-ext-diff" in args and "--no-textconv" in args
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", only_git)
    result = execute(repo, Settings(), lambda *args: None)
    assert calls and result.docs and not result.config_used.llm.enabled
    assert not marker.exists()
