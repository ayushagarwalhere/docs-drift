"""Drift age and fix-by policy: since when a statement is stale, and until when it must be fixed.

Ages are measured from the date the introducing commit landed on the branch (its committer date) to
the HEAD commit date (``ScanResult.scan_time``, also a committer date), never the wall clock, so the
same checkout always produces the same ages and due dates. The author date is not used: a rebase
merge or cherry-pick keeps it, so drift that landed today could otherwise be overdue on arrival.
"""

import json
import statistics
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from docs_drift.config import PolicyConfig
from docs_drift.models import Finding, ScanResult

# Histogram buckets are inclusive upper bounds in days; the final bucket is open-ended.
AGE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-7", 7),
    ("8-30", 30),
    ("31-90", 90),
    ("91-365", 365),
    ("366+", None),
)
OLDEST_LIMIT = 10
MAX_TREND_REPORTS = 100
MAX_TREND_REPORT_BYTES = 25_000_000
_DRIFT_VERDICTS = {"missing", "renamed", "default_changed", "invalid_command"}


class AgeStatus(StrEnum):
    """How a finding stands against the drift age policy."""

    unknown = "unknown"
    fresh = "fresh"
    warning = "warning"
    overdue = "overdue"


def days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def utc_date(value: datetime) -> date:
    return value.astimezone(UTC).date()


def finding_age(finding: Finding, head_date: date, policy: PolicyConfig) -> dict[str, Any]:
    """Return the age fields for one finding; non-drift and unattributed findings carry none."""
    if not finding.is_drift or finding.introduced_by is None:
        return {"stale_since": None, "age_days": None, "fix_by": None}
    since = utc_date(finding.introduced_by.landed)
    # Rewritten or clock-skewed history can date a commit after HEAD; such drift is new, not negative.
    age = max(0, (head_date - since).days)
    due = since + timedelta(days=policy.max_drift_age_days) if policy.max_drift_age_days is not None else None
    return {"stale_since": since, "age_days": age, "fix_by": due}


def annotate_ages(result: ScanResult, policy: PolicyConfig | None = None) -> ScanResult:
    """Copy the result with stale_since, age_days and fix_by set from history attribution."""
    selected = policy or result.config_used.policy
    if not result.head_sha.strip("0"):
        # Unversioned input has no HEAD date to measure against.
        return result
    findings = age_findings(result.findings, result.scan_time, result.head_sha, selected)
    config = result.config_used.model_copy(update={"policy": selected})
    return result.model_copy(update={"findings": findings, "config_used": config})


def age_findings(
    findings: Sequence[Finding], head_time: datetime, head_sha: str, policy: PolicyConfig
) -> list[Finding]:
    """Age each finding against the HEAD commit time; an unversioned snapshot leaves findings unchanged."""
    if not head_sha.strip("0"):
        return list(findings)
    head_date = utc_date(head_time)
    return [f.model_copy(update=finding_age(f, head_date, policy)) for f in findings]


def age_status(finding: Finding, policy: PolicyConfig) -> AgeStatus:
    """Ages are compared strictly: drift fails once it is older than the configured number of days."""
    if finding.age_days is None:
        return AgeStatus.unknown
    if policy.max_drift_age_days is not None and finding.age_days > policy.max_drift_age_days:
        return AgeStatus.overdue
    if policy.warn_drift_age_days is not None and finding.age_days > policy.warn_drift_age_days:
        return AgeStatus.warning
    return AgeStatus.fresh


def with_max_age(policy: PolicyConfig, max_age: int | None) -> PolicyConfig:
    """Apply a command-line maximum age; a warning threshold above it is dropped rather than rejected."""
    if max_age is None:
        return policy
    warn = policy.warn_drift_age_days
    if warn is not None and warn > max_age:
        warn = None
    return policy.model_copy(update={"max_drift_age_days": max_age, "warn_drift_age_days": warn})


def policy_active(policy: PolicyConfig) -> bool:
    return policy.max_drift_age_days is not None or policy.warn_drift_age_days is not None


def gate_by_age(findings: Sequence[Finding], policy: PolicyConfig) -> list[Finding]:
    """Keep only failures the age policy allows to fail; without a maximum age every failure stays."""
    if policy.max_drift_age_days is None:
        return list(findings)
    return [
        f
        for f in findings
        if age_status(f, policy) == AgeStatus.overdue
        or (age_status(f, policy) == AgeStatus.unknown and policy.fail_unknown_age)
    ]


def policy_summary(findings: Sequence[Finding], policy: PolicyConfig) -> str | None:
    """One human-readable line describing how the policy classified the given drift."""
    if not policy_active(policy):
        return None
    counts = {status: 0 for status in AgeStatus}
    for finding in findings:
        if finding.is_drift:
            counts[age_status(finding, policy)] += 1
    parts = []
    if policy.max_drift_age_days is not None:
        parts.append(f"{counts[AgeStatus.overdue]} older than {days(policy.max_drift_age_days)} (overdue)")
    if policy.warn_drift_age_days is not None:
        parts.append(f"{counts[AgeStatus.warning]} older than {days(policy.warn_drift_age_days)} (warning)")
    parts.append(f"{counts[AgeStatus.fresh]} within policy")
    unknown = f"{counts[AgeStatus.unknown]} without history attribution"
    if policy.max_drift_age_days is not None:
        unknown += " (gated as overdue)" if policy.fail_unknown_age else " (not gated)"
    parts.append(unknown)
    return "Drift age policy: " + ", ".join(parts) + "."


def age_label(finding: Finding, policy: PolicyConfig) -> str | None:
    """Compact text for reports, for example 'stale since 2026-10-02 (3 days) · fix by 2026-11-01'."""
    if finding.stale_since is None or finding.age_days is None:
        return None
    text = f"stale since {finding.stale_since.isoformat()} ({days(finding.age_days)} at HEAD)"
    if finding.fix_by is not None:
        text += f" · fix by {finding.fix_by.isoformat()}"
    status = age_status(finding, policy)
    if status in {AgeStatus.overdue, AgeStatus.warning}:
        text += f" · {status.value.upper()}"
    return text


def histogram(ages: Iterable[int | None]) -> list[dict[str, Any]]:
    """Ordered buckets (a list, so sorted-key JSON output keeps the age order)."""
    counts = {label: 0 for label, _ in AGE_BUCKETS}
    counts["unknown"] = 0
    for age in ages:
        if age is None:
            counts["unknown"] += 1
            continue
        label = next(label for label, limit in AGE_BUCKETS if limit is None or age <= limit)
        counts[label] += 1
    limits = dict(AGE_BUCKETS)
    return [{"bucket": label, "max_days": limits.get(label), "count": count} for label, count in counts.items()]


def age_insights(drift: Sequence[Finding], policy: PolicyConfig) -> dict[str, Any]:
    """Histogram, oldest drift and policy counts for ``docs-drift insights``."""
    aged = [f for f in drift if f.age_days is not None]
    oldest = sorted(aged, key=lambda f: (-(f.age_days or 0), f.claim.doc_path, f.claim.line, f.claim.claim_id))
    statuses = [age_status(f, policy) for f in drift]
    return {
        "histogram": histogram(f.age_days for f in drift),
        "median_age_days": statistics.median_low([f.age_days or 0 for f in aged]) if aged else None,
        "oldest": [
            {
                "claim_id": f.claim.claim_id,
                "doc_path": f.claim.doc_path,
                "line": f.claim.line,
                "verdict": f.verdict.value,
                "value": f.claim.value,
                "stale_since": f.stale_since.isoformat() if f.stale_since else None,
                "age_days": f.age_days,
                "fix_by": f.fix_by.isoformat() if f.fix_by else None,
                "status": age_status(f, policy).value,
                "commit": f.introduced_by.short_sha if f.introduced_by else None,
            }
            for f in oldest[:OLDEST_LIMIT]
        ],
        "policy": policy.model_dump(mode="json"),
        "status": {status.value: statuses.count(status) for status in AgeStatus},
    }


class TrendPoint(BaseModel):
    """Drift totals and ages from one saved JSON report."""

    model_config = ConfigDict(extra="forbid")

    file: str
    head_sha: str
    scan_time: str
    drift: int
    with_age: int
    median_age_days: int | None
    oldest_age_days: int | None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _report_point(path: Path, name: str) -> TrendPoint | None:
    """Read only the fields a trend needs, so reports saved before age fields existed still count."""
    if path.stat().st_size > MAX_TREND_REPORT_BYTES:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    scanned = _parse_time(data.get("scan_time"))
    head = data.get("head_sha")
    if scanned is None or not isinstance(head, str):
        return None
    config = data.get("config_used")
    threshold = config.get("min_confidence", 0.6) if isinstance(config, dict) else 0.6
    threshold = threshold if isinstance(threshold, int | float) else 0.6
    ages: list[int] = []
    drift = 0
    for item in data["findings"]:
        if not isinstance(item, dict) or item.get("verdict") not in _DRIFT_VERDICTS:
            continue
        confidence = item.get("confidence", 0)
        if not isinstance(confidence, int | float) or confidence < threshold:
            continue
        drift += 1
        age = item.get("age_days")
        if not isinstance(age, int):
            commit = item.get("introduced_by")
            introduced = (
                _parse_time(commit.get("committed")) or _parse_time(commit.get("date"))
                if isinstance(commit, dict)
                else None
            )
            age = max(0, (utc_date(scanned) - utc_date(introduced)).days) if introduced else None
        if age is not None:
            ages.append(age)
    return TrendPoint(
        file=name,
        head_sha=head[:40],
        scan_time=scanned.astimezone(UTC).isoformat(),
        drift=drift,
        with_age=len(ages),
        median_age_days=statistics.median_low(ages) if ages else None,
        oldest_age_days=max(ages) if ages else None,
    )


def load_trend(directory: Path) -> tuple[list[TrendPoint], list[str]]:
    """Summarize saved JSON scan reports in one directory, ordered by HEAD commit time."""
    if not directory.is_dir():
        raise ValueError(f"Trend directory does not exist: {directory}. Choose a folder of saved JSON reports.")
    warnings: list[str] = []
    candidates = sorted(path for path in directory.glob("*.json") if path.is_file() and not path.is_symlink())
    if len(candidates) > MAX_TREND_REPORTS:
        warnings.append(f"Read the first {MAX_TREND_REPORTS} of {len(candidates)} reports by file name.")
        candidates = candidates[:MAX_TREND_REPORTS]
    points: list[TrendPoint] = []
    for path in candidates:
        point = _report_point(path, path.name)
        if point is None:
            warnings.append(f"Skipped {path.name}: not a readable docs-drift JSON scan report.")
            continue
        points.append(point)
    points.sort(key=lambda p: (p.scan_time, p.head_sha, p.file))
    return points, warnings
