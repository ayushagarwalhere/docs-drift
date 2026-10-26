"""From a web scan to a pull request: fix bundles, patch/PR/issue routes and the report panel."""

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docs_drift.config import Config
from docs_drift.fix.bundle import FixBundle, FixStatus, build_fix_bundle
from docs_drift.fix.pull_request import (
    REVIEW_NOTICE,
    apply_commands,
    code,
    fenced,
    render_issue_drafts,
    render_pull_request,
)
from docs_drift.models import ScanResult
from docs_drift.report import ContributePanel, RenderOptions, render
from docs_drift.scanning import run_scan
from docs_drift.web.app import create_app
from docs_drift.web.jobs import Job, Store
from docs_drift.web.settings import Settings
from docs_drift.web.worker import fix_bundle, public_result
from tests.fixtures.build_fixtures import build_fixtures
from tests.test_fixes import rename_result

URL = "https://github.com/acme/py-cli"
FOOTER = "Found with docs-drift (https://github.com/ayushagarwalhere/docs-drift), verified by …"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixtures")
    build_fixtures(root)
    return root


@pytest.fixture(scope="module")
def scanned(fixtures: Path) -> tuple[ScanResult, FixBundle]:
    """A live scan as the worker produces it: plan fixes on the checkout, then publish the result."""
    raw = run_scan(fixtures / "py-cli", Config())
    bundle = fix_bundle(raw, fixtures / "py-cli")
    assert bundle is not None
    return public_result(raw, URL), bundle


def wait(client: TestClient, ident: str) -> dict[str, Any]:
    for _ in range(300):
        data: dict[str, Any] = client.get("/api/scans/" + ident).json()
        if data["status"] in {"done", "failed"}:
            return data
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def live_app(tmp_path: Path, result: ScanResult, bundle: FixBundle | None, **settings: Any) -> TestClient:
    async def runner(job: Job, workspace: Path) -> None:
        for stage in ("cloning", "indexing", "matching", "history"):
            job.status = stage
            await asyncio.sleep(0)
        job.result, job.fixes = result, bundle

    return TestClient(create_app(Settings(data_dir=tmp_path / "data", **settings), test_runner=runner))


def submit(client: TestClient, url: str = URL, ref: str | None = None) -> str:
    response = client.post(
        "/api/scans", json={"repo_url": url, "ref": ref}, headers={"origin": "http://127.0.0.1:8080"}
    )
    assert response.status_code == 202, response.text
    ident: str = response.json()["scan_id"]
    assert wait(client, ident)["status"] == "done"
    return ident


def git_apply_check(repo: Path, patch: bytes, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    target = tmp_path / "fix.patch"
    target.write_bytes(patch)
    command = ["git", "apply", "--check", str(target)]
    return subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)


def test_bundle_contains_only_certain_rule_edits_and_applies(fixtures: Path, tmp_path: Path) -> None:
    raw = run_scan(fixtures / "py-cli", Config())
    bundle = build_fix_bundle(raw, fixtures / "py-cli")
    statuses = bundle.status()
    drift = [f for f in raw.findings if f.is_drift]
    assert set(statuses) == {f.claim.claim_id for f in drift}
    assert bundle.count(FixStatus.patched) == 3 and bundle.count(FixStatus.manual) == 1
    assert [(f.path, f.edits, f.additions, f.deletions) for f in bundle.files] == [("README.md", 3, 3, 3)]
    assert bundle.head_sha == raw.head_sha
    assert "+- `serve` accepts `--listen` (default 8000)." in bundle.patch
    assert bundle.patch.startswith("--- a/README.md\n+++ b/README.md\n")
    assert git_apply_check(fixtures / "py-cli", bundle.patch.encode(), tmp_path).returncode == 0
    # A finding without a certain edit stays manual: its line is context at most, never changed.
    manual = next(f for f in drift if statuses[f.claim.claim_id] == FixStatus.manual)
    changed = [line for line in bundle.patch.splitlines() if line[:1] in "+-" and line[:3] not in {"+++", "---"}]
    assert not any(manual.claim.value in line for line in changed)


def test_bundle_withholds_secret_like_oversized_and_stale_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    result = rename_result(repo, "Use `--old`.\n")
    assert build_fix_bundle(result, repo).available
    limited = build_fix_bundle(result, repo, max_patch_bytes=10)
    assert not limited.available and limited.count(FixStatus.withheld) == 1
    assert "size limit" in limited.findings[0].note
    secret_repo = tmp_path / "secret"
    secret_scan = rename_result(secret_repo, "Use `--old`.\ntoken = ghp_" + "a" * 36 + "\n")
    withheld = build_fix_bundle(secret_scan, secret_repo)
    assert not withheld.available and withheld.count(FixStatus.withheld) == 1
    assert "ghp_" not in withheld.model_dump_json()
    (repo / "README.md").write_text("Use `--old` here.\n", encoding="utf-8")
    stale = build_fix_bundle(result, repo)
    assert not stale.available and "changed after the scan" in stale.notes[0]
    assert stale.count(FixStatus.manual) == 1
    assert fix_bundle(result, tmp_path / "missing") is not None


def test_markdown_helpers_cannot_be_broken_out_of() -> None:
    assert code("a`b") == "`` a`b ``"
    assert code("x\n# heading\r\n[link](http://evil)") == "`x # heading  [link](http://evil)`"
    assert code("``` @someone") == "```` ``` @someone ````"
    assert fenced("```diff\nx\n````\n").startswith("`````text\n")
    assert apply_commands("https://github.com/acme/py-cli", "a" * 40) == [
        "git clone https://github.com/acme/py-cli.git && cd py-cli",
        "git checkout -b docs-drift-fixes " + "a" * 40,
        "git apply fix.patch",
    ]


def test_pull_request_and_issue_drafts_pin_evidence_and_escape_values(scanned: tuple[ScanResult, FixBundle]) -> None:
    result, bundle = scanned
    remote = URL
    text = render_pull_request(result, bundle, remote=remote)
    title, blank, *_ = text.splitlines()
    assert title == "docs: fix 3 stale references in README.md" and blank == ""
    assert REVIEW_NOTICE in text and text.rstrip().endswith(FOOTER)
    assert f"{URL}/blob/{result.head_sha}/README.md#L4" in text
    assert f"{URL}/commit/{result.head_sha}" in text
    assert "```diff\n--- a/README.md" in text
    assert "## Not changed" in text and text.index("## What's wrong") < text.index("## The change")
    issues = render_issue_drafts(result, bundle, remote=remote)
    assert issues.count("## Issue ") == sum(f.is_drift for f in result.findings)
    assert issues.count(FOOTER) == issues.count("## Issue ")
    assert "good first contribution" in issues and "no certain mechanical replacement" in issues
    # Hostile documentation text stays inside code spans and fences.
    hostile = result.model_copy(deep=True)
    finding = next(f for f in hostile.findings if f.is_drift)
    finding.claim.value = "`<img src=x onerror=alert(1)>` @maintainer [x](javascript:alert(1))"
    finding.doc_text = "```\n<script>alert(1)</script>\n```"
    drafted = render_issue_drafts(hostile, bundle, remote=remote)
    assert "`` `<img src=x onerror=alert(1)>` @maintainer [x](javascript:alert(1)) ``" in drafted
    assert "````text\n```\n<script>alert(1)</script>\n```\n````" in drafted
    no_remote = render_pull_request(result, bundle, remote=None)
    assert "https://github.com" not in no_remote.replace("https://github.com/ayushagarwalhere/docs-drift", "")


def test_live_scan_patch_pr_issue_routes_and_panel(
    tmp_path: Path, fixtures: Path, scanned: tuple[ScanResult, FixBundle]
) -> None:
    result, bundle = scanned
    with live_app(tmp_path, result, bundle) as client:
        ident = submit(client)
        assert client.get("/api/scans/" + ident).json()["fixes"] == {
            "available": True,
            "files": 1,
            "patched": 3,
            "manual": 1,
            "withheld": 0,
        }
        patch = client.get(f"/api/scans/{ident}/fix.patch")
        assert patch.status_code == 200
        assert patch.headers["content-type"].startswith("text/x-diff")
        assert patch.headers["content-disposition"] == 'attachment; filename="fix.patch"'
        assert patch.headers["cache-control"] == "no-store"
        assert f"Base commit: {result.head_sha}" in patch.text and REVIEW_NOTICE in patch.text
        check = git_apply_check(fixtures / "py-cli", patch.content, tmp_path)
        assert check.returncode == 0, check.stderr
        pull = client.get(f"/api/scans/{ident}/pr.md")
        assert pull.status_code == 200 and pull.headers["content-type"].startswith("text/markdown")
        assert REVIEW_NOTICE in pull.text and FOOTER in pull.text
        assert f"{URL}/blob/{result.head_sha}/README.md#L4" in pull.text
        issues = client.get(f"/api/scans/{ident}/issues.md")
        assert issues.status_code == 200 and "## Issue 4" in issues.text
        page = client.get("/scan/" + ident)
        html = page.text
        assert "Turn this into a PR" in html
        assert f'href="/api/scans/{ident}/fix.patch" download="fix.patch"' in html
        assert f'data-copy-url="/api/scans/{ident}/pr.md"' in html
        assert f'data-copy-url="/api/scans/{ident}/issues.md"' in html
        assert f"git checkout -b docs-drift-fixes {result.head_sha}" in html
        assert "git clone https://github.com/acme/py-cli.git &amp;&amp; cd py-cli" in html
        assert "Review every change before opening a PR." in html
        assert html.count('data-fix="patched"') == 3 and html.count('data-fix="manual"') == 1
        assert "[![docs drift](http://127.0.0.1:8080/badge/acme/py-cli.svg)](http://127.0.0.1:8080/)" in html
        assert 'src="data:image/svg+xml;base64,' in html
        assert "img.shields.io/endpoint?url=http%3A%2F%2F127.0.0.1%3A8080%2Fbadge%2Facme%2Fpy-cli.json" in html
        assert "Turn this into a PR" in client.get(f"/api/scans/{ident}/report.html").text
        # CSP is unchanged: every executable script is either same-origin or hash-pinned; no inline handlers.
        policy = page.headers["content-security-policy"]
        script_src = policy.split("script-src ")[1].split(";")[0]
        assert "'unsafe-inline'" not in script_src and "img-src 'self' data:" in policy
        assert len(re.findall(r"'sha256-", script_src)) == len(re.findall(r"<script[ >]", html))
        assert not re.search(r"\son[a-z]+=", html)


def test_no_certain_fixes_returns_clear_404s(tmp_path: Path, fixtures: Path) -> None:
    raw = run_scan(fixtures / "clean-repo", Config())
    bundle = build_fix_bundle(raw, fixtures / "clean-repo")
    result = public_result(raw, "https://github.com/acme/clean")
    with live_app(tmp_path, result, bundle) as client:
        ident = submit(client, "https://github.com/acme/clean")
        for route in ("fix.patch", "pr.md"):
            response = client.get(f"/api/scans/{ident}/{route}")
            assert response.status_code == 404
            assert "no certain documentation edits" in response.json()["detail"]
        missing = client.get(f"/api/scans/{ident}/issues.md")
        assert missing.status_code == 404 and "no stale statements" in missing.json()["detail"]
        html = client.get("/scan/" + ident).text
        assert "nothing to patch" in html and "fix.patch" not in html
        assert client.get("/api/scans/not-a-scan/fix.patch").status_code == 404


def test_scan_without_prepared_bundle_offers_issue_drafts(
    tmp_path: Path, scanned: tuple[ScanResult, FixBundle]
) -> None:
    result, _ = scanned
    with live_app(tmp_path, result, None) as client:
        ident = submit(client)
        assert client.get(f"/api/scans/{ident}/fix.patch").status_code == 404
        assert client.get(f"/api/scans/{ident}/issues.md").status_code == 200
        html = client.get("/scan/" + ident).text
        assert "No patch was prepared for this scan" in html and "Copy issue drafts" in html


def test_pending_and_failed_status_pages_preview_the_panel(tmp_path: Path) -> None:
    async def slow(job: Job, workspace: Path) -> None:
        job.status = "cloning"
        await asyncio.sleep(5)

    with TestClient(create_app(Settings(data_dir=tmp_path / "data", scan_timeout=0.2), test_runner=slow)) as client:
        ident = client.post("/api/scans", json={"repo_url": URL}).json()["scan_id"]
        pending = client.get("/scan/" + ident).text
        assert 'id="pr-preview"' in pending and "Turn this into a PR" in pending
        assert "git clone https://github.com/acme/py-cli.git" in pending
        assert wait(client, ident)["status"] == "failed"
        failed = client.get("/scan/" + ident).text
        assert re.search(r'id="pr-preview"[^>]*hidden', failed)
        assert client.get(f"/api/scans/{ident}/fix.patch").status_code == 409


def test_baked_fixture_examples_ship_applicable_patches(tmp_path: Path, fixtures: Path) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", live_enabled=False))) as client:
        for example, name in (("example-py-cli", "py-cli"), ("example-node-tool", "node-tool")):
            patch = client.get(f"/api/scans/{example}/fix.patch")
            assert patch.status_code == 200
            check = git_apply_check(fixtures / name, patch.content, tmp_path)
            assert check.returncode == 0, check.stderr
            html = client.get("/scan/" + example).text
            assert "Synthetic fixture" in html and f"cd tests/fixtures/repos/{name}" in html
            assert "github.com/docs-drift" not in client.get(f"/api/scans/{example}/pr.md").text
            assert "/badge/" not in html
        reviewed = client.get("/scan/example-rich-cli-reviewed").text
        assert "Turn this into a PR" in reviewed and "No patch was prepared" in reviewed


def test_stored_jobs_keep_their_fix_bundle(tmp_path: Path, scanned: tuple[ScanResult, FixBundle]) -> None:
    result, bundle = scanned
    settings = Settings(data_dir=tmp_path)
    store = Store(settings)
    store.save(Job("abc", URL, status="done", result=result, fixes=bundle))
    store.jobs.clear()
    loaded = store.get("abc")
    assert loaded is not None and loaded.fixes == bundle
    raw = json.loads((store.directory / "abc.json").read_text(encoding="utf-8"))
    assert raw["fixes"]["head_sha"] == result.head_sha


def test_report_panel_escapes_repository_values(scanned: tuple[ScanResult, FixBundle]) -> None:
    result, _ = scanned
    panel = ContributePanel(
        scan_id="abc",
        prepared=True,
        patch_available=True,
        patched=1,
        manual=0,
        withheld=0,
        issues_available=True,
        commands=["git clone https://github.com/a/b.git && cd b", "git checkout -b docs-drift-fixes x", "git apply"],
        files=[('<img src=x onerror="alert(1)">.md', 1, 1)],
        note="<script>alert(1)</script>",
    )
    html = render(result, "html", RenderOptions(share_id="abc", contribute=panel))
    assert "<img src=x" not in html and "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;.md" in html
    assert "<script>alert(1)" not in html
    assert 'id="contribute"' not in render(result, "html")
