"""One serializable unit of publishable findings shared by the CLI, the Action and the App.

A bundle is computed once from a local checkout (static scan, optional base
comparison, baseline, reviews and the existing gate) and can then be published
without the repository: it carries redacted findings, the gate summary and
whole-line suggestions derived from the existing exact-span rule fixes.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.baseline import FindingBaseline
from docs_drift.collaboration import pr_comparison, static_scan
from docs_drift.config import Config
from docs_drift.fix.edits import FixPlan, StaleDocumentError, TextEdit, decode, safe_path
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.github.diff import changed_lines
from docs_drift.history import _run
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.llm.privacy import mask_secrets, redact_result
from docs_drift.models import Finding, ScanResult
from docs_drift.policy import GateEvaluation, GatePolicy, GateSummary, evaluate_gate
from docs_drift.review import ReviewBook, suppressed_ids

BUNDLE_NAME = "github-bundle.json"


class LineSuggestion(BaseModel):
    """A whole-line replacement, as GitHub ```suggestion blocks require."""

    model_config = ConfigDict(extra="forbid")

    path: str
    line: int = Field(ge=1)
    original: str
    replacement: str
    claim_ids: list[str]


class ReviewBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    head_sha: str
    base_sha: str | None = None
    gate: GateSummary
    findings: list[Finding] = Field(default_factory=list)
    # Pre-existing drift on lines the PR edits: worth fixing while there, never counted by the gate.
    touched: list[Finding] = Field(default_factory=list)
    suggestions: list[LineSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Scanned directory relative to the Git top level; already applied to every path above.
    path_prefix: str = ""

    @property
    def clean(self) -> bool:
        return not self.findings and not self.touched


def line_suggestions(result: ScanResult, plan: FixPlan) -> list[LineSuggestion]:
    """Collapse single-line exact-span edits into whole-line replacements; skip anything uncertain."""
    root = result.docs_repo_path or result.repo_path
    grouped: dict[tuple[str, int], list[TextEdit]] = {}
    for edit in plan.edits:
        if edit.line != edit.end_line or "\n" in edit.new_text or "\r" in edit.new_text:
            continue
        grouped.setdefault((edit.file, edit.line), []).append(edit)
    texts: dict[str, list[str] | None] = {}
    suggestions = []
    for (path, line), edits in sorted(grouped.items()):
        if path not in texts:
            try:
                texts[path] = decode(safe_path(root, path).read_bytes())[0].splitlines()
            except (OSError, UnicodeError, ValueError):
                texts[path] = None
        lines = texts[path]
        if lines is None or line > len(lines):
            continue
        original = updated = lines[line - 1]
        last = len(original) + 1
        valid = True
        for edit in sorted(edits, key=lambda e: e.column, reverse=True):
            start, end = edit.column - 1, edit.end_column - 1
            if end > last or original[start:end] != edit.old_text:
                valid = False
                break
            updated = updated[:start] + edit.new_text + updated[end:]
            last = start
        # A masked secret would change the exact bytes a suggestion commits; leave it to a human.
        if not valid or updated == original or mask_secrets(original) != original or mask_secrets(updated) != updated:
            continue
        suggestions.append(
            LineSuggestion(
                path=path,
                line=line,
                original=original,
                replacement=updated,
                claim_ids=sorted({edit.claim_id for edit in edits}),
            )
        )
    return suggestions


def repository_prefix(repo: Path) -> str:
    """``sub/dir/`` when the scan ran in a subdirectory of its Git checkout (the Action's ``repo:`` input)."""
    result = _run(repo, ["rev-parse", "--show-prefix"])
    prefix = result.stdout.strip() if result else ""
    return prefix if not prefix or prefix.endswith("/") else prefix + "/"


def _prefixed(findings: list[Finding], prefix: str) -> list[Finding]:
    return [
        f.model_copy(update={"claim": f.claim.model_copy(update={"doc_path": prefix + f.claim.doc_path})})
        for f in findings
    ]


def bundle_from_evaluation(  # noqa: PLR0913, PLR0917 - the gate's complete inputs.
    result: ScanResult,
    evaluation: GateEvaluation,
    policy: GatePolicy,
    suppressed: set[str] | None = None,
    base_sha: str | None = None,
    touched: list[Finding] | None = None,
) -> ReviewBundle:
    """Publish exactly what the gate counts (qualifying new drift without active reviews), plus touched lines."""
    suppressed = suppressed or set()
    findings = [
        f
        for f in evaluation.new_findings
        if f.confidence >= policy.min_confidence and f.claim.claim_id not in suppressed
    ]
    chosen = {f.claim.claim_id for f in findings}
    extra = [
        f
        for f in touched or []
        if f.is_drift
        and f.confidence >= policy.min_confidence
        and f.claim.claim_id not in suppressed
        and f.claim.claim_id not in chosen
    ]
    selected = result.model_copy(update={"findings": [*findings, *extra]})
    warnings = list(evaluation.summary.warnings)
    suggestions: list[LineSuggestion] = []
    token = CACHE_DISABLED.set(True)
    try:
        suggestions = line_suggestions(selected, plan_rule_fixes(selected))
    except (StaleDocumentError, OSError, ValueError):
        warnings.append("Suggestions omitted: documents changed during the scan or could not be read safely.")
    finally:
        CACHE_DISABLED.reset(token)
    # GitHub paths (comments, permalinks, annotations) are relative to the repository root.
    prefix = repository_prefix(result.docs_repo_path or result.repo_path)
    redacted = _prefixed(redact_result(selected).findings, prefix)
    return ReviewBundle(
        head_sha=result.head_sha,
        base_sha=base_sha,
        gate=evaluation.summary,
        findings=redacted[: len(findings)],
        touched=redacted[len(findings) :],
        suggestions=[s.model_copy(update={"path": prefix + s.path}) for s in suggestions],
        warnings=warnings,
        path_prefix=prefix,
    )


def build_bundle(  # noqa: PLR0913 - mirrors the Action/CLI adoption inputs.
    repo: Path,
    config: Config,
    policy: GatePolicy,
    *,
    base: str | None = None,
    baseline: FindingBaseline | None = None,
    reviews: ReviewBook | None = None,
) -> ReviewBundle:
    """Scan the checkout; with ``base`` only drift introduced or touched since that revision is selected."""
    base_sha = None
    touched: list[Finding] = []
    if base:
        full, gate_result, comparison = pr_comparison(repo, config, base)
        base_sha = str(comparison["base_sha"])
        drift_docs = {f.claim.doc_path for f in full.findings if f.is_drift}
        touched = touched_findings(full, changed_lines(repo, base_sha, paths=drift_docs))
    else:
        full = gate_result = static_scan(repo, config)
    suppressed = suppressed_ids(full, reviews) if reviews else set()
    evaluation = evaluate_gate(gate_result, policy, baseline, suppressed)
    return bundle_from_evaluation(full, evaluation, policy, suppressed, base_sha, touched)


def touched_findings(result: ScanResult, lines: dict[str, set[int]]) -> list[Finding]:
    """Current drift whose documentation line the PR added or modified."""
    return [f for f in result.findings if f.is_drift and f.claim.line in lines.get(f.claim.doc_path, set())]


def load_bundle(path: Path) -> ReviewBundle:
    try:
        return ReviewBundle.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"Cannot read a docs-drift GitHub bundle at {path}; rerun the scan step.") from None
