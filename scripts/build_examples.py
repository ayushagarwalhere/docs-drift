"""Regenerate output examples, the v1 schema, and normalized report snapshots."""

import json
import re
from pathlib import Path

from docs_drift.config import Config
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions, render
from docs_drift.scanning import run_scan


def main() -> None:
    schemas = Path("src/docs_drift/schemas")
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "scan-result.v1.json").write_text(
        json.dumps(ScanResult.model_json_schema(), indent=2) + "\n", encoding="utf-8"
    )
    Path("docs/schema/scan-result.v1.json").write_text(
        json.dumps(ScanResult.model_json_schema(), indent=2) + "\n", encoding="utf-8"
    )
    for repo in sorted(Path("tests/fixtures/repos").iterdir()):
        if not repo.is_dir():
            continue
        result = run_scan(repo, Config())
        formats = (
            ["terminal", "json", "sarif", "markdown", "junit", "github", "html"] if repo.name == "py-cli" else ["html"]
        )
        for format in formats:
            suffix = {"terminal": "txt", "markdown": "md", "junit": "xml", "github": "annotations.txt"}.get(
                format, format
            )
            Path(f"examples/{repo.name}.{suffix}").write_text(render(result, format), encoding="utf-8")
        terminal = render(result, "terminal", RenderOptions(show_low_confidence=True))
        normalized = re.sub(r"\d+(?:\.\d+)?s\b", "<time>", terminal)
        normalized = re.sub(r"\d+ days ago", "<age>", normalized)
        normalized = re.sub(r"\b[0-9a-f]{8}\b", "<sha>", normalized)
        Path(f"tests/snapshots/{repo.name}.txt").write_text(normalized, encoding="utf-8")
        tags = re.findall(r"<(?:header|main|section|aside|footer|article|details|nav)\b[^>]*>", render(result, "html"))
        Path(f"tests/snapshots/{repo.name}.html.structure").write_text("\n".join(tags) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
