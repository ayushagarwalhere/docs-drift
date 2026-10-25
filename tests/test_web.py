"""Public demo boundary, lifecycle, expiry, fallback and rendering contracts."""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docs_drift.config import Config
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from docs_drift.web import jobs
from docs_drift.web.app import create_app
from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.jobs import Job, LocalQueue, Store
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository
from docs_drift.web.worker import public_result
from scripts import smoke


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com.evil.com/a/b",
        "https://github.com@evil.com/a/b",
        "https://user:pass@github.com/a/b",
        "https://github.com:443/a/b",
        "https://127.0.0.1/a/b",
        "https://[::1]/a/b",
        "https://g\u0456thub.com/a/b",
        "file:///etc/passwd",
        "C:\\repo",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b#fragment",
        "https://github.com/a/../b",
        "https://github.com/a/b/tree/--upload-pack=evil",
        "https://github.com/a/b/tree/main..evil",
        "https://github.com/a/b/tree/main%0aevil",
        "https://github.com/a/b/tree/a.lock",
        "https://github.com/a/b\n",
        "https://github.com/a/b/tree/@{evil}",
        "https://github.com/a/b/other",
    ],
)
def test_ssrf_table(url: str) -> None:
    with pytest.raises(ValueError):
        validate_repository(url)


def test_url_normalization_and_ref_injection() -> None:
    assert validate_repository("https://github.com/OWNER/Repo.git").url == "https://github.com/owner/repo"
    assert validate_repository("https://github.com/a/b/tree/feature/x").ref == "feature/x"
    for ref in ["--upload-pack=evil", "-x", "refs/../evil", "x y", "a\\b", "a\nb"]:
        with pytest.raises(ValueError):
            validate_repository("https://github.com/a/b", ref)


@pytest.fixture
def result(tmp_path: Path) -> ScanResult:
    (tmp_path / "README.md").write_text("# Demo\n\nUse `--missing`. <script>alert(1)</script>\n", encoding="utf-8")
    (tmp_path / "cli.py").write_text('import argparse\np=argparse.ArgumentParser()\np.add_argument("--current")\n')
    return public_result(run_scan(tmp_path, Config()), "https://github.com/acme/test")


def wait(client: TestClient, ident: str) -> dict[str, Any]:
    for _ in range(200):
        data: dict[str, Any] = client.get("/api/scans/" + ident).json()
        if data["status"] in {"done", "failed"}:
            return data
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_lifecycle_rendering_examples_and_headers(tmp_path: Path, result: ScanResult) -> None:
    async def runner(job: Job, workspace: Path) -> None:
        for stage in ("cloning", "indexing", "matching", "history"):
            job.status = stage
            await asyncio.sleep(0.02)
        job.result = result

    app = create_app(Settings(data_dir=tmp_path / "data"), test_runner=runner)
    with TestClient(app) as client:
        landing = client.get("/")
        assert landing.status_code == 200 and "Inspect the real case" in landing.text
        assert "pipx install docs-drift" in landing.text
        case = client.get("/case-study")
        assert case.status_code == 200 and "567f6c84" in case.text
        assert "False positive" in case.text
        reviewed = client.get("/api/scans/example-rich-cli-reviewed/result.json")
        assert reviewed.status_code == 200
        assert reviewed.json()["head_sha"] == "46f4d2469097be395558768714a5f07ebccf1412"
        assert 'href="https://github.com/ayushagarwalhere/docs-drift"' in landing.text
        assert 'href="https://pypi.org/project/docs-drift/"' in landing.text
        for asset in ("/static/site.css", "/static/site.js", "/static/fonts/InstrumentSerif-400.woff2"):
            assert client.get(asset).status_code == 200
        assert len(landing.content) < 300_000
        assert "frame-ancestors 'none'" in landing.headers["content-security-policy"]
        assert landing.headers["x-content-type-options"] == "nosniff"
        assert landing.headers["referrer-policy"] == "no-referrer"
        examples = client.get("/api/examples").json()
        assert len(examples) >= 4
        for example in examples:
            assert client.get("/scan/" + example["scan_id"]).status_code == 200
        submitted = client.post(
            "/api/scans",
            json={"repo_url": "https://github.com/acme/test"},
            headers={"origin": "http://127.0.0.1:8080"},
        )
        assert submitted.status_code == 202
        ident = submitted.json()["scan_id"]
        assert "data-scan-id" in client.get("/scan/" + ident).text
        data = wait(client, ident)
        assert data["status"] == "done" and data["result"]["schema_version"] == 1
        report = client.get(f"/api/scans/{ident}/report.html")
        assert "<script>alert(1)</script>" not in report.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report.text
        assert "sha256-" in report.headers["content-security-policy"]
        assert client.get(f"/api/scans/{ident}/result.json").json()["schema_version"] == 1
        review = client.get(f"/api/scans/{ident}/report.md")
        assert review.status_code == 200
        assert review.headers["content-type"].startswith("text/markdown")
        assert review.headers["content-disposition"] == 'attachment; filename="documentation-review.md"'
        assert "not an applicable patch" in review.text
        assert f"http://127.0.0.1:8080/scan/{ident}" in review.text
        assert "<script>alert(1)</script>" not in review.text
        assert review.headers["cache-control"] == "no-store"
        graph = client.get(f"/api/scans/{ident}/graph").json()
        assert len(graph["nodes"]) <= 1000
        assert "placeholder" in client.get("/built-with-voice").text.lower()
        assert client.get("/healthz").json()["status"] == "ok"
    assert not list((tmp_path / "data/work").iterdir())


def test_review_download_requires_an_existing_finished_scan(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False))
    with TestClient(app) as client:
        app.state.store.jobs["pending"] = Job("pending", "https://github.com/acme/test")
        assert client.get("/api/scans/pending/report.md").status_code == 409
        assert client.get("/api/scans/not-a-scan/report.md").status_code == 404


def test_real_case_serves_applicable_patch_and_targeted_static_proof(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", live_enabled=False))
    with TestClient(app) as client:
        assert "/static/evidence/rich-cli-correction.patch" in client.get("/case-study").text
        correction = client.get("/static/evidence/rich-cli-correction.patch")
        assert correction.status_code == 200 and "+Add the `--line-numbers`" in correction.text
        proof = client.get("/static/evidence/correction-verification.json").json()
        assert proof["git_apply_check"] == "passed" and proof["target_code_executed"] is False
        assert len(proof["rows"]) == 2 and all(row["after"] == "ok" for row in proof["rows"])


def test_shutdown_waits_for_in_progress_workspace_cleanup(
    tmp_path: Path, result: ScanResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()
    original_remove = remove_workspace

    def blocked_remove(root: Path, workspace: Path) -> None:
        started.set()
        assert release.wait(timeout=5)
        original_remove(root, workspace)

    async def runner(job: Job, workspace: Path) -> None:
        (workspace / "temporary").write_text("scan artifact")
        job.result = result

    async def exercise() -> None:
        settings = Settings(data_dir=tmp_path)
        queue = LocalQueue(settings, Store(settings), test_runner=runner)
        queue.submit(validate_repository("https://github.com/example/tool"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            closing = asyncio.create_task(queue.close())
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
        await closing
        assert not list(queue.work.iterdir())

    monkeypatch.setattr(jobs, "remove_workspace", blocked_remove)
    asyncio.run(exercise())


def test_deployment_smoke_exercises_examples_and_completed_scan(tmp_path: Path, result: ScanResult) -> None:
    async def runner(job: Job, workspace: Path) -> None:
        job.result = result

    app = create_app(Settings(data_dir=tmp_path), test_runner=runner)
    with TestClient(app) as client:

        def request(base: str, path: str, payload: dict[str, str] | None = None) -> tuple[bytes, Any]:
            response = client.request("POST" if payload else "GET", path, json=payload)
            response.raise_for_status()
            return response.content, response.headers

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(smoke, "request", request)
            smoke.smoke("http://testserver")


def test_limits_timeout_failure_and_cleanup(tmp_path: Path) -> None:
    async def runner(job: Job, workspace: Path) -> None:
        (workspace / "temporary").write_text("data")
        await asyncio.sleep(1)

    app = create_app(Settings(data_dir=tmp_path, scan_timeout=0.03), test_runner=runner)
    with TestClient(app) as client:
        ids = [
            client.post("/api/scans", json={"repo_url": "https://github.com/a/b"}).json()["scan_id"] for _ in range(5)
        ]
        assert client.post("/api/scans", json={"repo_url": "https://github.com/a/b"}).status_code == 429
        assert wait(client, ids[0])["error"]["kind"] == "timeout"
        assert client.post("/api/scans", content=b"x" * 4097).status_code == 413
        assert (
            client.post(
                "/api/scans", json={"repo_url": "https://github.com/a/b"}, headers={"origin": "https://evil.example"}
            ).status_code
            == 403
        )
    assert not list((tmp_path / "work").iterdir())


def test_store_cache_expiry_and_circuit(tmp_path: Path, result: ScanResult) -> None:
    settings = Settings(data_dir=tmp_path, max_results=2)
    store = Store(settings)
    job = Job("abc", "https://github.com/acme/test", status="done", result=result)
    store.save(job)
    assert store.cache[store.key(job.repo_url, result.head_sha)] == "abc"
    store.jobs.clear()
    cached = store.get("abc")
    assert cached is not None and cached.result is not None
    assert cached.result.head_sha == result.head_sha
    assert store.get("..\\secret") is None
    loaded = store.get("abc")
    assert loaded is not None
    loaded.created = time.time() - 90000
    assert store.get("abc") is None

    async def exercise() -> None:
        async def fail(job: Job, workspace: Path) -> None:
            job.status = "failed"
            job.error = {"kind": "clone", "message": "Unavailable", "hint": "Try an example"}

        queue = LocalQueue(settings, store, test_runner=fail)
        jobs = [queue.submit(validate_repository("https://github.com/a/b")) for _ in range(3)]
        await asyncio.gather(*list(queue.tasks))
        assert all(j.status == "failed" for j in jobs) and not queue.available
        await queue.close()

    asyncio.run(exercise())
