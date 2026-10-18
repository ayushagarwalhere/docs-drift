"""Stable, complete JSON contract."""

import json

from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import ordered


def render(result: ScanResult, options: RenderOptions) -> str:
    data = result.model_dump(mode="json")
    # Measurements belong to the benchmark/run log, not a reproducible snapshot.
    data["timings"] = {}
    data["repo_path"] = "."
    data["docs_repo_path"] = "docs" if result.docs_repo_path else None
    data["findings"] = [f.model_dump(mode="json") for f in ordered(result)]
    return json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
