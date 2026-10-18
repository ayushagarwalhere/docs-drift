"""Render actual CLI output and reports; label the unposted PR comment as a preview."""

import json
import os
import subprocess
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def terminal_gif(assets: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "from docs_drift.cli import app; app()",
        "scan",
        "--repo",
        "tests/fixtures/repos/py-cli",
        "--no-color",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "COLUMNS": "100"},
        check=True,
    )
    lines = ["$ docs-drift scan --repo tests/fixtures/repos/py-cli --no-color", *result.stdout.splitlines()]
    if not any("^" in line for line in lines) or not any("Introduced by" in line for line in lines):
        raise ValueError("The real scan must contain a caret and commit attribution.")
    font_path = Path("C:/Windows/Fonts/consola.ttf")
    font = ImageFont.truetype(str(font_path), 17) if font_path.exists() else ImageFont.load_default(size=17)
    frames = []
    for frame in range(16):
        canvas = Image.new("RGB", (1200, 690), "#101b22")
        draw = ImageDraw.Draw(canvas)
        draw.text((24, 16), "docs-drift / actual CLI output / eight-second replay", font=font, fill="#83decb")
        visible = lines[: max(8, (frame + 1) * len(lines) // 16)]
        for number, line in enumerate(visible[-27:]):
            color = "#f6c773" if "^" in line else "#9faeb8" if "Introduced by" in line else "#e7edf1"
            draw.text((24, 55 + number * 22), line, font=font, fill=color)
        frames.append(canvas)
    frames[0].save(assets / "scan-demo.gif", save_all=True, append_images=frames[1:], duration=500, loop=0)
    (assets / "scan-demo.txt").write_text(result.stdout, encoding="utf-8")


def main() -> None:
    assets = ROOT / "docs/assets"
    assets.mkdir(parents=True, exist_ok=True)
    terminal_gif(assets)
    markdown = (ROOT / "examples/py-cli.md").read_text(encoding="utf-8")
    preview = (
        '<!doctype html><meta charset="utf-8"><title>Unposted PR comment preview</title>'
        "<style>body{font:16px system-ui;background:#f5f7f9;color:#172a35;padding:40px;max-width:1000px;"
        "margin:auto}article{background:white;border:1px solid #ccd5df;border-radius:12px;padding:28px}"
        "table{border-collapse:collapse;font-size:14px}td,th{padding:8px;border:1px solid #cbd5df}"
        "pre{white-space:pre-wrap;background:#edf2f6;padding:14px}a{color:#006e68}</style>"
        "<p>docs-drift · actual Markdown report · unposted comment preview</p><article>"
        + MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
        + "</article>"
    )
    preview_file = ROOT / ".docs-drift/comment-preview.html"
    preview_file.write_text(preview, encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1280}, color_scheme="dark")
        page.goto((ROOT / "examples/py-cli.html").as_uri())
        page.wait_for_function("typeof cy !== 'undefined' && cy.nodes().length > 0")
        page.locator("#graph").scroll_into_view_if_needed()
        page.screenshot(path=str(assets / "graph-view.png"), full_page=False)
        page.goto(preview_file.as_uri())
        page.screenshot(path=str(assets / "pr-comment-preview.png"), full_page=True)
        browser.close()
    (assets / "provenance.json").write_text(
        json.dumps(
            {
                "terminal": "Actual scan subprocess stdout, replayed over eight seconds; scan-demo.txt retained.",
                "graph": "Actual examples/py-cli.html in installed Edge, graph panel visible.",
                "comment": "Actual examples/py-cli.md rendered locally; not a posted GitHub comment.",
                "vscode": "Pending actual editor recording; no simulated squiggle GIF supplied.",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
