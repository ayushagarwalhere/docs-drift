"""Generated examples and the demo replay must represent actual scanner output."""

import json
from pathlib import Path

import pytest
from PIL import Image
from PIL.GifImagePlugin import GifImageFile

from scripts import build_doc_assets, build_examples
from tests.fixtures.build_fixtures import build_fixtures


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
