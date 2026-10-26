"""Docs-health badges: CLI output, flat SVG escaping, and cheap web badges from cached scans."""

import asyncio
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from docs_drift import badge
from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from docs_drift.web.app import create_app
from docs_drift.web.jobs import Job, Store
from docs_drift.web.settings import Settings
from docs_drift.web.worker import public_result
from tests.fixtures.build_fixtures import build_fixtures

SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("badge-fixtures")
    build_fixtures(root)
    return root


@pytest.fixture(scope="module")
def py_cli(fixtures: Path) -> ScanResult:
    return public_result(run_scan(fixtures / "py-cli", Config()), "https://github.com/acme/py-cli")


def test_endpoint_payload_colours_and_label_validation() -> None:
    expected = {"schemaVersion": 1, "label": "docs drift", "message": "0 stale", "color": "brightgreen"}
    assert badge.endpoint(0) == expected
    assert badge.endpoint(2)["color"] == "yellow" and badge.endpoint(5)["color"] == "orange"
    assert badge.endpoint(12) == {"schemaVersion": 1, "label": "docs drift", "message": "12 stale", "color": "red"}
    assert badge.endpoint(None) == {
        "schemaVersion": 1,
        "label": "docs drift",
        "message": "not scanned",
        "color": "lightgrey",
    }
    for label in ("", " ", "x" * 41, "a\nb", "a\x00b"):
        with pytest.raises(ValueError):
            badge.validate_label(label)
    assert badge.validate_label("docs <health>") == "docs <health>"


def test_svg_is_well_formed_flat_and_escaped() -> None:
    hostile = '<a href="x">&\'</a>'
    document = badge.svg(3, hostile)
    root = ET.fromstring(document)
    assert root.tag == SVG + "svg" and root.get("height") == "20"
    assert root.get("aria-label") == f"{hostile}: 3 stale"
    assert root.findtext(SVG + "title") == f"{hostile}: 3 stale"
    texts = [node.text for node in root.iter(SVG + "text")]
    assert texts == [hostile, hostile, "3 stale", "3 stale"]
    assert "<a href" not in document
    rects = list(root.iter(SVG + "rect"))
    left, right = int(rects[1].get("width") or 0), int(rects[2].get("width") or 0)
    assert int(root.get("width") or 0) == left + right and rects[2].get("fill") == badge.COLORS["yellow"]
    clean = ET.fromstring(badge.svg(0))
    assert clean.get("aria-label") == "docs drift: 0 stale"
    assert next(clean.iter(SVG + "rect")).get("rx") == "3"
    # Widths track the text: a longer message makes a wider badge.
    assert int(ET.fromstring(badge.svg(1234)).get("width") or 0) > int(clean.get("width") or 0)


def test_cli_badge_prints_and_writes_json_and_svg(fixtures: Path, tmp_path: Path) -> None:
    repo = fixtures / "py-cli"
    runner = CliRunner()
    printed = runner.invoke(app, ["badge", "--repo", str(repo)])
    assert printed.exit_code == 0, printed.output
    payload = json.loads(printed.output)
    findings = run_scan(repo, Config()).findings
    expected = badge.stale_count(findings, Config().min_confidence)
    assert expected > 0 and payload == badge.endpoint(expected)
    json_path, svg_path = tmp_path / "out" / "badge.json", tmp_path / "out" / "badge.svg"
    written = runner.invoke(
        app, ["badge", "--repo", str(repo), "--json", str(json_path), "--svg", str(svg_path), "--label", "docs"]
    )
    assert written.exit_code == 0, written.output
    assert f"Badge: {expected} stale." in written.output
    assert json.loads(json_path.read_text(encoding="utf-8"))["label"] == "docs"
    assert ET.fromstring(svg_path.read_text(encoding="utf-8")).get("aria-label") == f"docs: {expected} stale"
    assert b"\r\n" not in json_path.read_bytes()
    strict = runner.invoke(app, ["badge", "--repo", str(repo), "--min-confidence", "1"])
    assert json.loads(strict.output)["message"] == f"{badge.stale_count(findings, 1)} stale"
    assert runner.invoke(app, ["badge", "--repo", str(repo), "--label", ""]).exit_code == 2
    assert runner.invoke(app, ["badge", "--repo", str(repo), "--min-confidence", "2"]).exit_code == 2


def test_cli_badge_respects_baseline_and_configuration(fixtures: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("README.md", "cli.py", "Makefile"):
        (repo / name).write_bytes((fixtures / "py-cli" / name).read_bytes())
    runner = CliRunner()
    assert json.loads(runner.invoke(app, ["badge", "--repo", str(repo)]).output)["message"] != "0 stale"
    baseline = tmp_path / "baseline.json"
    assert runner.invoke(app, ["baseline", "update", "--path", str(baseline), "--repo", str(repo)]).exit_code == 0
    accepted = runner.invoke(app, ["badge", "--repo", str(repo), "--baseline", str(baseline)])
    assert json.loads(accepted.output) == badge.endpoint(0)
    (repo / ".docs-drift.toml").write_text("min_confidence = 1.0\n", encoding="utf-8")
    configured = json.loads(runner.invoke(app, ["badge", "--repo", str(repo)]).output)
    assert configured["message"] == "0 stale"
    assert runner.invoke(app, ["badge", "--repo", str(repo), "--baseline", "missing.json"]).exit_code == 2


def live(tmp_path: Path, result: ScanResult) -> TestClient:
    async def runner(job: Job, workspace: Path) -> None:
        await asyncio.sleep(0)
        job.result = result

    return TestClient(create_app(Settings(data_dir=tmp_path / "data"), test_runner=runner))


def finish(client: TestClient, url: str, ref: str | None = None) -> str:
    ident: str = client.post("/api/scans", json={"repo_url": url, "ref": ref}).json()["scan_id"]
    for _ in range(300):
        done = client.get("/api/scans/" + ident).json()["status"] == "done"
        # The job is saved (and the badge index updated) just after its status turns "done".
        if done and client.get("/api/stats").json()["active_or_queued"] == 0:
            return ident
        time.sleep(0.01)
    raise AssertionError("scan did not finish")


def test_web_badges_serve_latest_cached_scan_without_scanning(tmp_path: Path, py_cli: ScanResult) -> None:
    stale = badge.stale_count(py_cli.findings, py_cli.config_used.min_confidence)
    with live(tmp_path, py_cli) as client:
        missing = client.get("/badge/acme/py-cli.svg")
        assert missing.status_code == 200 and "not scanned" in missing.text
        assert missing.headers["content-type"] == "image/svg+xml"
        assert missing.headers["cache-control"] == "public, max-age=60"
        assert client.get("/api/stats").json()["active_or_queued"] == 0
        assert client.get("/api/stats").json()["scanned_repos"] == 0
        finish(client, "https://github.com/acme/py-cli")
        svg = client.get("/badge/acme/py-cli.svg")
        assert svg.status_code == 200 and f"{stale} stale" in svg.text
        assert svg.headers["cache-control"] == "public, max-age=300"
        assert "frame-ancestors 'none'" in svg.headers["content-security-policy"]
        ET.fromstring(svg.text)
        assert client.get("/badge/ACME/Py-Cli.svg").text == svg.text
        assert client.get("/badge/acme/py-cli.git.svg").text == svg.text
        cached = client.get("/badge/acme/py-cli.svg", headers={"If-None-Match": svg.headers["etag"]})
        assert cached.status_code == 304 and not cached.content
        endpoint = client.get("/badge/acme/py-cli.json")
        assert endpoint.headers["content-type"] == "application/json"
        assert endpoint.json() == badge.endpoint(stale)
        assert client.get("/badge/someone/else.json").json() == badge.endpoint(None)
        # A scan of a branch never changes the default-branch badge.
        finish(client, "https://github.com/acme/other", ref="feature")
        assert client.get("/badge/acme/other.json").json()["message"] == "not scanned"
        stats = client.get("/api/stats").json()
        assert stats["scanned_repos"] == 2 and stats["active_or_queued"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/badge/-acme/repo.svg",
        "/badge/acme/..svg",
        "/badge/acme/.json",
        "/badge/" + "a" * 40 + "/repo.svg",
        "/badge/acme/" + "r" * 101 + ".svg",
        "/badge/ac_me/repo.svg",
        "/badge/acme/re%20po.svg",
        "/badge/acme/repo%2F..%2Fx.svg",
        "/badge/acme/repo.png",
        "/badge/acme/repo",
        "/badge/acme/re<po.svg",
    ],
)
def test_web_badge_rejects_invalid_names(tmp_path: Path, path: str) -> None:
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", live_enabled=False))) as client:
        response = client.get(path)
        assert response.status_code == 404
        assert "image/svg+xml" not in response.headers.get("content-type", "")


def test_badge_index_persists_and_expires(tmp_path: Path, py_cli: ScanResult) -> None:
    settings = Settings(data_dir=tmp_path, ttl=100)
    store = Store(settings)
    store.save(Job("abc", "https://github.com/acme/py-cli", status="done", result=py_cli))
    store.save(Job("ref", "https://github.com/acme/branch", ref="dev", status="done", result=py_cli))
    entry = Store(settings).badge("https://github.com/acme/py-cli")
    assert entry is not None and entry["scan_id"] == "abc" and entry["sha"] == py_cli.head_sha
    assert Store(settings).badge("https://github.com/acme/branch") is None
    store.latest["https://github.com/acme/py-cli"]["created"] = time.time() - 101
    assert store.badge("https://github.com/acme/py-cli") is None
    (store.directory / "latest.json").write_text('{"x": {"scan_id": 1}, "y": []}', encoding="utf-8")
    assert Store(settings).latest == {}
    (store.directory / "latest.json").write_text("not json", encoding="utf-8")
    assert Store(settings).latest == {}
