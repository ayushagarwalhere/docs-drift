"""Read and write explicit finding baselines without running scanned code."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docs_drift.models import DecidedBy, Finding, Verdict
from docs_drift.safe_files import metadata_text


class BaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    verdict: Verdict
    doc_path: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    decided_by: DecidedBy = DecidedBy.rules
    replayed: bool = False


class FindingBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    findings: list[BaselineEntry] = Field(default_factory=list)


def finding_id(finding: Finding) -> str:
    """Claim IDs are stable across line shifts and suitable for baseline updates."""
    return finding.claim.claim_id


def load_baseline(path: Path) -> FindingBaseline:
    try:
        return FindingBaseline.model_validate_json(metadata_text(path))
    except ValidationError:
        # Pydantic's default diagnostics can echo claim text from the input file.
        raise ValueError(
            f"Invalid baseline JSON or schema at {path}. Regenerate it from a reviewed scan with baseline update."
        ) from None
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
            decided_by=item.decided_by,
            replayed=item.replayed,
        )
        for item in findings
        if item.is_drift
    ]
    entries.sort(key=lambda item: (item.doc_path, item.claim, item.finding_id))
    path.write_text(FindingBaseline(findings=entries).model_dump_json(indent=2) + "\n", encoding="utf-8")


def new_findings(findings: list[Finding], baseline: FindingBaseline) -> list[Finding]:
    # A changed verdict must be reviewed again even when the claim stayed put.
    known = {(entry.finding_id, entry.verdict) for entry in baseline.findings}
    return [item for item in findings if item.is_drift and (finding_id(item), item.verdict) not in known]
