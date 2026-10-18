"""Versioned report renderers; every renderer returns a complete string."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from docs_drift.models import ScanResult


@dataclass
class RenderOptions:
    color: bool = False
    hyperlinks: bool = False
    width: int = 100
    verbose: bool = False
    show_all: bool = False
    show_low_confidence: bool = False
    show_authors: bool = False
    pr_mode: bool = False
    size_cap: int = 60_000
    artifact_url: str = "scan.html"
    command: str = "docs-drift scan"
    share_id: str | None = None
    build_sha: str = "development"


def render(scan_result: ScanResult, format: str = "terminal", options: RenderOptions | None = None) -> str:
    """Render a full scan without writing files or changing the input result."""
    from importlib import import_module  # noqa: PLC0415 - optional dependency or shared pipeline import.

    if format not in {"terminal", "json", "sarif", "markdown", "junit", "github", "html"}:
        raise ValueError(
            f"Unknown report format {format!r}; choose terminal, json, sarif, markdown, junit, github or html."
        )
    renderer = cast(Callable[[ScanResult, RenderOptions], str], import_module(f"docs_drift.report.{format}").render)
    from docs_drift.llm.privacy import redact_result  # noqa: PLC0415 - shared serialization boundary.

    return renderer(redact_result(scan_result), options or RenderOptions())
