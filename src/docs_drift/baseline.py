"""Read and write explicit finding baselines without running scanned code."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.models import Finding, Verdict


class BaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    verdict: Verdict
    doc_path: str = Field(min_length=1)
    claim: str = Field(min_length=1)


class FindingBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    findings: list[BaselineEntry] = Field(default_factory=list)


def finding_id(finding: Finding) -> str:
    """Claim IDs are stable across line shifts and suitable for baseline updates."""
    return finding.claim.claim_id


def load_baseline(path: Path) -> FindingBaseline:
    try:
        return FindingBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read baseline {path}: {exc}. Check the path and permissions.") from exc


def save_baseline(path: Path, findings: list[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        BaselineEntry(
            finding_id=finding_id(item),
            verdict=item.verdict,
            doc_path=item.claim.doc_path,
            claim=item.claim.value,
        )
        for item in findings
        if item.is_drift
    ]
    entries.sort(key=lambda item: (item.doc_path, item.claim, item.finding_id))
    path.write_text(FindingBaseline(findings=entries).model_dump_json(indent=2) + "\n", encoding="utf-8")


def new_findings(findings: list[Finding], baseline: FindingBaseline) -> list[Finding]:
    known = {entry.finding_id for entry in baseline.findings}
    return [item for item in findings if item.is_drift and finding_id(item) not in known]
