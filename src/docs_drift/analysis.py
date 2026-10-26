"""Aggregate evidence-backed findings and select findings touched by a Git change."""

import re
from collections import Counter
from pathlib import Path
from typing import Any

from docs_drift.config import Config
from docs_drift.docs.pipeline import _head
from docs_drift.drift_age import age_insights
from docs_drift.history import _run
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan


def insights(result: ScanResult) -> dict[str, Any]:
    drift = [f for f in result.findings if f.is_drift and f.confidence >= result.config_used.min_confidence]
    return {
        "head_sha": result.head_sha,
        "claims": len(result.claims),
        "verdicts": result.summary()["by_verdict"],
        "drift_by_document": dict(sorted(Counter(f.claim.doc_path for f in drift).items())),
        "drift_by_package": dict(sorted(Counter(f.package or "." for f in drift).items())),
        "with_introducing_commit": sum(f.introduced_by is not None for f in drift),
        "drift_age": age_insights(drift, result.config_used.policy),
        "warnings": result.warnings,
    }


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    for ref in (base, head):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./~^{}-]{0,199}", ref) or ".." in ref:
            raise ValueError("Use a single Git revision without options, whitespace or revision ranges.")
    result = _run(repo, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base, head, "--"])
    if result is None:
        raise ValueError("Cannot compare these revisions. Fetch the base commit or choose a revision that exists.")
    return sorted(p.replace("\\", "/") for p in result.stdout.split("\0") if p)


def impact(repo: Path, config: Config, base: str, head: str = "HEAD") -> ScanResult:
    """Report current findings linked to changed docs/source; not a runtime dependency proof."""
    paths = set(changed_paths(repo, base, head))
    resolved = _run(repo, ["rev-parse", "--verify", head])
    if not resolved or resolved.stdout.strip() != _head(repo):
        raise ValueError("Impact scans the current checkout; --head must resolve to its HEAD.")
    result = run_scan(repo, config)
    selected = [
        f
        for f in result.findings
        if f.claim.doc_path in paths
        or (f.matched_symbol and f.matched_symbol.file in paths)
        or any(e.file in paths for e in f.evidence)
    ]
    return result.model_copy(update={"findings": selected, "claims": [f.claim for f in selected]})
