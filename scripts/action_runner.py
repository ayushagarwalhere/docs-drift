"""Static, read-token-only composite Action runner; never posts comments or calls a model."""

import os
from pathlib import Path

from docs_drift.config import load_config
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.report import RenderOptions, render
from docs_drift.scanning import run_scan


def main() -> None:
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    repo = (workspace / os.getenv("SCAN_REPO", ".")).resolve()
    if not repo.is_relative_to(workspace):
        raise ValueError("Repository input must stay inside the checked-out workspace.")
    output = Path(os.environ["RUNNER_TEMP"]) / "docs-drift-reports"
    output.mkdir(parents=True, exist_ok=True)
    token = CACHE_DISABLED.set(True)
    try:
        result = run_scan(repo, load_config(repo), no_cache=True)
    finally:
        CACHE_DISABLED.reset(token)
    for format, suffix in [("html", "html"), ("json", "json"), ("sarif", "sarif"), ("markdown", "md")]:
        (output / ("scan." + suffix)).write_text(render(result, format, RenderOptions(pr_mode=True)), encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("report-directory=" + output.as_posix() + "\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(render(result, "markdown"))
    if os.getenv("FAIL_ON_DRIFT", "true") == "true" and any(
        f.is_drift and f.confidence >= result.config_used.min_confidence for f in result.findings
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
