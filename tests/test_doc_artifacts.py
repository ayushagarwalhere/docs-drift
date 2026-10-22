"""Generated examples and the demo replay must represent actual scanner output."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PIL.GifImagePlugin import GifImageFile

from docs_drift.config import Config
from docs_drift.scanning import run_scan
from docs_drift.web.validation import Repository
from scripts import build_doc_assets, build_examples, build_web_examples
from tests.fixtures.build_fixtures import build_fixtures


def test_web_example_rebuild_preserves_real_snapshot_and_fixture_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixtures(tmp_path / "tests/fixtures/repos")
    monkeypatch.chdir(tmp_path)
    folder = build_web_examples.EXAMPLES
    folder.mkdir(parents=True)
    report = run_scan(tmp_path / "tests/fixtures/repos/py-cli", Config())
    original = report.model_dump_json()
    (folder / "reviewed.json").write_text(original, encoding="utf-8")
    entry = {"scan_id": "reviewed", "file": "reviewed.json", "kind": "public", "verified": False}
    (folder / "manifest.json").write_text(json.dumps([entry]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["build_web_examples"])

    build_web_examples.main()

    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest[0] == entry
    assert (folder / "reviewed.json").read_text(encoding="utf-8") == original
    assert len(manifest) == 5
    assert all(item["kind"] == "fixture" and "Synthetic" in item["note"] for item in manifest[1:])


def test_public_example_acquisition_keeps_failures_out_and_requires_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    folder = build_web_examples.EXAMPLES
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("[]")
    (folder / "verification.json").write_text("[]")
    (tmp_path / "README.md").write_text("# Real scan input\n")
    report = run_scan(tmp_path, Config(), history=False)

    class Queue:
        def __init__(self, *args: object) -> None:
            self.tasks: set[object] = set()

        def submit(self, repository: Repository) -> SimpleNamespace:
            url = repository.url
            success = url.endswith("/success")
            return SimpleNamespace(
                repo_url=url,
                status="done" if success else "failed",
                error=None,
                metrics={},
                result=report if success else None,
            )

    monkeypatch.setattr(build_web_examples, "LocalQueue", Queue)
    asyncio.run(
        build_web_examples.real_repos(["https://github.com/example/success", "https://github.com/example/fail"])
    )

    manifest = json.loads((folder / "manifest.json").read_text())
    assert len(manifest) == 1 and manifest[0]["verified"] is False
    assert manifest[0]["kind"] == "public"
    assert not (folder / "fail.json").exists()


def test_example_builder_writes_valid_reports_and_normalized_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixtures(tmp_path / "tests/fixtures/repos")
    for folder in ("docs/schema", "examples", "tests/snapshots"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    build_examples.main()
    examples = tmp_path / "examples"
    result = json.loads((examples / "py-cli.json").read_text(encoding="utf-8"))
    assert len(result["findings"]) == 10
    assert json.loads((examples / "py-cli.sarif").read_text(encoding="utf-8"))["version"] == "2.1.0"
    assert "<sha>" in (tmp_path / "tests/snapshots/py-cli.txt").read_text(encoding="utf-8")
    assert "^" in (examples / "py-cli.txt").read_text(encoding="utf-8")
    assert len(list(examples.glob("*.html"))) == 4
    assert json.loads((tmp_path / "docs/schema/scan-result.v1.json").read_text())["title"] == "ScanResult"


def test_terminal_gif_keeps_actual_stdout_and_exact_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixtures(tmp_path / "tests/fixtures/repos")
    monkeypatch.setattr(build_doc_assets, "ROOT", tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    build_doc_assets.terminal_gif(assets)
    output = (assets / "scan-demo.txt").read_text(encoding="utf-8")
    assert "Introduced by" in output and "^" in output
    with Image.open(assets / "scan-demo.gif") as gif:
        assert isinstance(gif, GifImageFile)
        duration = 0
        for frame in range(gif.n_frames):
            gif.seek(frame)
            duration += gif.info["duration"]
        assert duration == 8000
        assert gif.size == (1200, 690)
