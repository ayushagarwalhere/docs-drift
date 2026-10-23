"""Explicit CI adoption policy; full scan results are never modified by a gate."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.baseline import FindingBaseline, new_findings
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import Finding, ScanResult


class GatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min_confidence: float = Field(default=0.6, ge=0, le=1)
    max_findings: int = Field(default=0, ge=0, strict=True)
    fail_on_drift: bool = True


class GateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    head_sha: str
    policy: GatePolicy
    baseline_used: bool
    claims_extracted: int
    unverifiable_findings: int
    total_drift: int
    baseline_drift: int
    new_drift: int
    qualifying_new_drift: int
    below_threshold_new_drift: int
    reviewed_drift: int = 0
    status: Literal["passed", "failed", "report_only"]
    warnings: list[str]


@dataclass(frozen=True)
class GateEvaluation:
    summary: GateSummary
    new_findings: list[Finding]


def evaluate_gate(
    result: ScanResult, policy: GatePolicy, baseline: FindingBaseline | None = None, suppressed: set[str] | None = None
) -> GateEvaluation:
    """Only qualifying *new* drift counts toward the explicitly allowed budget."""
    drift = [finding for finding in result.findings if finding.is_drift]
    new = drift if baseline is None else new_findings(drift, baseline)
    suppressed = suppressed or set()
    qualifying = sum(
        finding.confidence >= policy.min_confidence and finding.claim.claim_id not in suppressed for finding in new
    )
    status: Literal["passed", "failed", "report_only"] = "report_only"
    if policy.fail_on_drift:
        status = "failed" if qualifying > policy.max_findings else "passed"
    warnings = [mask_secrets(warning) for warning in result.warnings]
    if not result.claims:
        warnings.append("No documentation claims were extracted. Zero drift does not establish scan coverage.")
    if not result.head_sha.strip("0"):
        warnings.append("No Git revision was identified. Treat this result as an unversioned source snapshot.")
    return GateEvaluation(
        summary=GateSummary(
            head_sha=result.head_sha,
            policy=policy,
            baseline_used=baseline is not None,
            claims_extracted=len(result.claims),
            unverifiable_findings=sum(finding.verdict == "unverifiable" for finding in result.findings),
            total_drift=len(drift),
            baseline_drift=len(drift) - len(new),
            new_drift=len(new),
            qualifying_new_drift=qualifying,
            below_threshold_new_drift=sum(f.confidence < policy.min_confidence for f in new),
            reviewed_drift=sum(f.claim.claim_id in suppressed for f in new),
            status=status,
            warnings=warnings,
        ),
        new_findings=new,
    )
