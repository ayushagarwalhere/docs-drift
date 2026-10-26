"""Stable, typed entry point for using docs-drift as a library.

``check()`` mirrors ``docs-drift check --fail-on drift``: it runs the same scan,
applies an optional baseline and the confidence threshold, and returns a small
immutable result instead of exiting the process. ``run_scan`` remains available
for callers that need the full versioned ``ScanResult``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.models import Finding, ScanResult, Verdict

PathInput = str | os.PathLike[str]

_DIFF_VERDICTS = {Verdict.renamed, Verdict.default_changed}


class CheckSummary(BaseModel):
    """Counts behind a ``CheckResult``; every field is JSON-serialisable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str
    docs_scanned: list[str] = Field(default_factory=list)
    claims: int = Field(default=0, ge=0)
    drift: int = Field(default=0, ge=0, description="Drift findings before the baseline and threshold.")
    baselined: int = Field(default=0, ge=0, description="Drift findings accepted by the baseline.")
    below_threshold: int = Field(default=0, ge=0, description="New drift findings under min_confidence.")
    failing: int = Field(default=0, ge=0, description="Findings that make the check fail.")
    unverifiable: int = Field(default=0, ge=0)
    min_confidence: float = Field(ge=0, le=1)
    history: bool = True
    strict: bool = False
    by_verdict: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class CheckResult(BaseModel):
    """Outcome of ``docs_drift.check()``.

    ``findings`` holds the drift findings that fail the check (new relative to
    the baseline and at or above ``min_confidence``). ``unverifiable`` lists
    claims the static index could not decide; they only fail a strict check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[Finding] = Field(default_factory=list)
    unverifiable: list[Finding] = Field(default_factory=list)
    summary: CheckSummary
    passed: bool

    @property
    def failures(self) -> list[Finding]:
        """Findings that fail the check, ordered by document, line and claim."""
        items = [*self.findings, *(self.unverifiable if self.summary.strict else [])]
        return sorted(items, key=_order)

    def for_doc(self, doc_path: str) -> list[Finding]:
        """Failing findings located in one documentation file (POSIX path relative to the docs root)."""
        return [finding for finding in self.failures if finding.claim.doc_path == doc_path]

    def report(self, doc_path: str | None = None) -> str:
        """Plain-text, diff-style report of failing findings, optionally for one document."""
        failures = self.failures if doc_path is None else self.for_doc(doc_path)
        count = len(self.summary.docs_scanned)
        scope = doc_path or f"{count} documentation file{'' if count == 1 else 's'}"
        if not failures:
            return f"docs-drift: no drift in {scope}.\n"
        noun = "finding" if len(failures) == 1 else "findings"
        lines = [f"docs-drift: {len(failures)} {noun} in {scope}", ""]
        for finding in failures:
            lines.extend(format_finding(finding))
            lines.append("")
        if self.summary.baselined:
            lines.append(f"{self.summary.baselined} known drift findings were accepted by the baseline.")
        lines.append("Run `docs-drift explain <claim-id>` for evidence, or `docs-drift fix` to preview patches.")
        return "\n".join(lines).rstrip() + "\n"


def _order(finding: Finding) -> tuple[str, int, int, str]:
    return finding.claim.doc_path, finding.claim.line, finding.claim.column, finding.claim.claim_id


def _proposed_line(finding: Finding) -> str | None:
    """Return the documentation line with the certain rule fix applied, or ``None``.

    The edit comes from the same rule ``docs-drift fix`` uses (unique word-bounded
    span, rename confidence gate, unit-preserving defaults), so a report never
    proposes a change the fixer would refuse or make differently.
    """
    from docs_drift.fix.rules import rule_edit  # noqa: PLC0415 - keep import of the facade light.

    line = finding.doc_text
    if not line or finding.verdict not in _DIFF_VERDICTS:
        return None
    edit = rule_edit(finding, line)
    if edit is None or edit.line != edit.end_line:
        return None
    start, end = edit.column - 1, edit.end_column - 1
    if line[start:end] != edit.old_text:
        return None
    return line[:start] + edit.new_text + line[end:]


def format_finding(finding: Finding) -> list[str]:
    """Render one finding as readable lines: location, the doc line, a proposed line and evidence."""
    claim = finding.claim
    lines = [
        f"{claim.doc_path}:{claim.line}: {finding.verdict.value} {claim.kind.value} "
        f"`{claim.value}` (confidence {finding.confidence:.2f})"
    ]
    current = finding.doc_text or claim.raw_text
    proposed = _proposed_line(finding)
    if proposed is not None and proposed != current:
        lines.extend([f"  - {current.strip()}", f"  + {proposed.strip()}"])
    else:
        lines.append(f"    {current.strip()}")
        if finding.suggestion:
            lines.append(f"    closest match: {finding.suggestion}")
    lines.append(f"    why: {finding.reasons[0]}")
    evidence = finding.evidence[0]
    lines.append(f"    evidence: {evidence.file}:{evidence.line} {evidence.text.strip()}".rstrip())
    if finding.introduced_by is not None:
        commit = finding.introduced_by
        lines.append(f"    introduced by: {commit.short_sha} {commit.summary} ({commit.date.date().isoformat()})")
    lines.append(f"    claim id: {claim.claim_id}")
    return lines


def _resolve(repo: Path, value: PathInput) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo / path


def evaluate(
    result: ScanResult,
    *,
    min_confidence: float | None = None,
    baseline: PathInput | None = None,
    strict: bool = False,
    history: bool = True,
) -> CheckResult:
    """Apply baseline, threshold and strictness to an existing scan without rescanning."""
    from docs_drift.baseline import load_baseline, new_findings  # noqa: PLC0415 - keep import of the facade light.

    threshold = result.config_used.min_confidence if min_confidence is None else min_confidence
    if not 0 <= threshold <= 1:
        raise ValueError("Choose a min_confidence between 0 and 1.")
    drift = [finding for finding in result.findings if finding.is_drift]
    new = drift
    if baseline is not None:
        new = new_findings(drift, load_baseline(_resolve(result.repo_path, baseline)))
    failing = sorted((finding for finding in new if finding.confidence >= threshold), key=_order)
    unverifiable = sorted((f for f in result.findings if f.verdict == Verdict.unverifiable), key=_order)
    summary = CheckSummary(
        head_sha=result.head_sha,
        docs_scanned=sorted(doc.path for doc in result.docs),
        claims=len(result.claims),
        drift=len(drift),
        baselined=len(drift) - len(new),
        below_threshold=len(new) - len(failing),
        failing=len(failing) + (len(unverifiable) if strict else 0),
        unverifiable=len(unverifiable),
        min_confidence=threshold,
        history=history,
        strict=strict,
        by_verdict=result.summary()["by_verdict"],
        warnings=list(result.warnings),
    )
    return CheckResult(
        findings=failing,
        unverifiable=unverifiable,
        summary=summary,
        passed=summary.failing == 0,
    )


def check(  # noqa: PLR0913 - keyword-only options of the public facade.
    path: PathInput = ".",
    *,
    docs: str | Sequence[str] | None = None,
    min_confidence: float | None = None,
    history: bool = True,
    baseline: PathInput | None = None,
    config: PathInput | None = None,
    strict: bool = False,
) -> CheckResult:
    """Scan ``path`` and report documentation drift, like ``docs-drift check``.

    Args:
        path: Repository root to inspect.
        docs: Glob pattern(s) that replace the configured documentation globs.
        min_confidence: Threshold for failing drift; defaults to the repository configuration.
        history: Inspect Git history for attribution and history-proven renames.
        baseline: Baseline JSON (relative to ``path``); its known drift does not fail.
        config: Explicit TOML configuration, relative to the working directory.
        strict: Also fail on unverifiable claims.

    Raises:
        ValueError: Invalid options, configuration or baseline (``ConfigError`` is a ``ValueError``).
        docs_drift.errors.DocsDriftError: The repository cannot be scanned.
    """
    from docs_drift.config import load_config  # noqa: PLC0415 - keep `import docs_drift` cheap.
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - keep `import docs_drift` cheap.

    if min_confidence is not None and not 0 <= min_confidence <= 1:
        raise ValueError("Choose a min_confidence between 0 and 1.")
    repo = Path(path).resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository directory does not exist: {repo}. Choose an existing directory.")
    settings = load_config(repo, Path(config) if config is not None else None)
    if docs is not None:
        patterns = [docs] if isinstance(docs, str) else list(docs)
        if not patterns:
            raise ValueError("Pass at least one documentation glob in docs, or None for the configuration.")
        settings = settings.model_copy(update={"docs": patterns})
    result = run_scan(repo, settings, history=history)
    return evaluate(result, min_confidence=min_confidence, baseline=baseline, strict=strict, history=history)


__all__ = ["CheckResult", "CheckSummary", "check", "evaluate", "format_finding"]
