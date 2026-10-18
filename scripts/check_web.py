"""Capture both demo pages at desktop/360px and run actual Lighthouse accessibility audits."""

import json
import subprocess
from pathlib import Path
from typing import Literal

from playwright.sync_api import sync_playwright

MIN_ACCESSIBILITY = 95


def main() -> None:
    themes: tuple[Literal["light", "dark"], ...] = ("light", "dark")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=["--remote-debugging-port=9222"])
        for name, path in [("landing", "/"), ("result", "/scan/example-py-cli")]:
            for device, width, height in [("desktop", 1440, 1050), ("phone", 360, 800)]:
                for theme in themes:
                    page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme)
                    page.goto("http://127.0.0.1:8000" + path)
                    page.wait_for_timeout(1200)
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), (name, device)
                    page.screenshot(path=f"examples/web-{name}-{device}-{theme}.png", full_page=True)
                    page.close()
            output = f".docs-drift/lighthouse-{name}.json"
            subprocess.run(
                [
                    "npm.cmd",
                    "exec",
                    "--cache",
                    ".npm-cache",
                    "--yes",
                    "--package=lighthouse",
                    "--",
                    "lighthouse",
                    "http://127.0.0.1:8000" + path,
                    "--port=9222",
                    "--only-categories=accessibility",
                    "--output=json",
                    f"--output-path={output}",
                    "--quiet",
                ],
                check=True,
                timeout=90,
            )
            report = json.loads(Path(output).read_text(encoding="utf-8"))
            score = report["categories"]["accessibility"]["score"] * 100
            print(f"{name}: Lighthouse accessibility {score:.0f}/100", flush=True)
            for key, audit in report["audits"].items():
                if audit.get("score") == 0:
                    print(key, json.dumps(audit.get("details", {}))[:3000], flush=True)
            assert score >= MIN_ACCESSIBILITY, score
        browser.close()


if __name__ == "__main__":
    main()
