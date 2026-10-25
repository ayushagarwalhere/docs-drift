"""Reviewable PR comparisons, bounded support history and verified local fix branches."""

import json
import re
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, select_autoescape

from docs_drift.analysis import changed_paths
from docs_drift.config import Config
from docs_drift.fix.edits import apply_file, unified_patch
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.git_safety import checkout_options
from docs_drift.history import _run
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.llm.privacy import mask_secrets, redact_result, redact_value
from docs_drift.models import Evidence, Finding, ScanResult, Verdict
from docs_drift.ownership import coverage_report
from docs_drift.report import render
from docs_drift.review import ReviewBook, review_status
from docs_drift.scanning import run_scan
from docs_drift.snapshots import materialize_snapshot, resolve_revision, scan_snapshot

MAX_HISTORY_COMMITS = 50


def static_scan(repo: Path, config: Config) -> ScanResult:
    token = CACHE_DISABLED.set(True)
    try:
        return run_scan(repo, config, no_cache=True)
    finally:
        CACHE_DISABLED.reset(token)


def _same_drift(old: Finding, new: Finding) -> bool:
    if (old.verdict, old.suggestion, old.reasons) == (new.verdict, new.suggestion, new.reasons):
        return True
    # Base snapshots are scanned without history, so a history-confirmed rename at the
    # head appears as plain missing drift at the base. That drift is pre-existing, not new.
    absent = {Verdict.missing, Verdict.renamed}
    return old.verdict in absent and new.verdict in absent


def pr_comparison(repo: Path, config: Config, base: str) -> tuple[ScanResult, ScanResult, dict[str, Any]]:
    base_sha = resolve_revision(repo, base)
    before = scan_snapshot(repo, base_sha, config)
    current = static_scan(repo, config)
    previous = {f.claim.claim_id: f for f in before.findings}
    changed = changed_paths(repo, base_sha, "HEAD")
    selected = []
    observations = []
    for finding in current.findings:
        old = previous.get(finding.claim.claim_id)
        if not finding.is_drift or (old and _same_drift(old, finding)):
            continue
        if old is None and finding.claim.doc_path not in changed:
            continue
        evidence = list(finding.evidence)
        if old and old.matched_symbol:
            symbol = old.matched_symbol
            evidence.append(
                Evidence(
                    file=symbol.file,
                    line=symbol.line,
                    text=f"Statically supported at base {base_sha}: {symbol.qualified_name}",
                )
            )
        selected.append(finding.model_copy(update={"evidence": evidence}))
        observations.append(
            {
                "claim_id": finding.claim.claim_id,
                "doc_path": finding.claim.doc_path,
                "line": finding.claim.line,
                "before": old.verdict.value if old else "not-present",
                "after": finding.verdict.value,
                "supported_at_base": bool(old and old.verdict == "ok"),
                "divergence_range": {"after": base_sha, "through": current.head_sha},
                "attributed_commit": finding.introduced_by.sha if finding.introduced_by else None,
            }
        )
    return (
        current,
        current.model_copy(update={"findings": selected}),
        {
            "schema_version": 1,
            "base_sha": base_sha,
            "head_sha": current.head_sha,
            "changed_paths": changed,
            "observations": observations,
            "warnings": [*before.warnings, *current.warnings],
            "limits": "Only new/changed drift at the current checkout is selected. A base/head range is not an exact "
            "breaking-commit attribution. Static support is not runtime behavior.",
        },
    )


def write_pr_report(  # noqa: PLR0913 - report inputs and optional audit context.
    result: ScanResult,
    comparison: dict[str, Any],
    output: Path,
    reviews: ReviewBook | None = None,
    *,
    important: list[str] | None = None,
    full_result: ScanResult | None = None,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Choose a new report directory; previous review artifacts are preserved.")
    active = review_status(full_result or result, reviews) if reviews else {}
    coverage = coverage_report(full_result or result, important)
    owners = {row["path"]: row["owners"] for row in coverage["documents"]}
    audit = {key: entry.model_dump(mode="json") for key, entry in active.items()}
    actionable = result.model_copy(
        update={
            "findings": [
                f
                for f in result.findings
                if f.claim.claim_id not in active or active[f.claim.claim_id].decision == "stale"
            ]
        }
    )
    token = CACHE_DISABLED.set(True)
    try:
        plan = plan_rule_fixes(actionable)
    finally:
        CACHE_DISABLED.reset(token)
    patch = unified_patch(result.repo_path, plan)
    if mask_secrets(patch) != patch:
        patch = ""
        comparison = {
            **comparison,
            "warnings": [
                *comparison["warnings"],
                "Patch omitted: masking would change exact source bytes. Review locally.",
            ],
        }
    comparison = redact_value(comparison)
    coverage = redact_value(coverage)
    active = {
        key: entry.model_copy(update={"reason": mask_secrets(entry.reason), "reviewer": mask_secrets(entry.reviewer)})
        for key, entry in active.items()
    }
    audit = {key: entry.model_dump(mode="json") for key, entry in active.items()}
    payload = {
        **comparison,
        "reviews": audit,
        "coverage": coverage,
        "fixable_edits": len(plan.edits),
        "fix_suggestions": plan.suggestions,
    }
    payload = cast(dict[str, Any], redact_value(payload))
    output.mkdir(parents=True)
    for format, name in [("json", "pr.json"), ("markdown", "pr.md"), ("github", "annotations.txt")]:
        (output / name).write_text(render(actionable, format), encoding="utf-8")
    (output / "review.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (output / "fix.patch").write_bytes(patch.encode())
    environment = Environment(loader=FileSystemLoader(Path(__file__).parent / "report"), autoescape=select_autoescape())
    (output / "pr.html").write_text(
        environment.get_template("workflow.html").render(
            result=redact_result(result),
            comparison=comparison,
            reviews=active,
            owners=owners,
            coverage=coverage,
            fixable=len(plan.edits),
        ),
        encoding="utf-8",
    )
    return payload


def support_history(repo: Path, config: Config, claim_id: str, max_commits: int = 20) -> dict[str, Any]:
    if not 1 <= max_commits <= MAX_HISTORY_COMMITS:
        raise ValueError("History scans must be bounded to 1-50 commits.")
    history = _run(repo, ["rev-list", "--first-parent", f"--max-count={max_commits}", "HEAD"])
    if not history:
        raise ValueError("Cannot traverse history; fetch the relevant commits first.")
    observations = []
    last_supported = first_drift = None
    newer = None
    for sha in history.stdout.splitlines():
        scan = scan_snapshot(repo, sha, config)
        finding = next((f for f in scan.findings if f.claim.claim_id == claim_id), None)
        if finding is None or finding.verdict == "unverifiable":
            observations.append({"sha": sha, "verdict": "absent-or-unverifiable"})
            break
        observations.append({"sha": sha, "verdict": finding.verdict.value})
        if finding.verdict == "ok":
            last_supported, first_drift = sha, newer
            break
        newer = sha
    return {
        "schema_version": 1,
        "claim_id": claim_id,
        "last_statically_supported": last_supported,
        "first_observed_drift": first_drift,
        "observations": observations,
        "max_commits": max_commits,
        "limits": "First-parent, bounded static scans. Missing claims or unavailable coverage stop attribution; "
        "null means unknown. No runtime or complete-history claim.",
    }


def fix_branch(  # noqa: PLR0912, PLR0915 - explicit fail-closed proposal stages.
    repo: Path, config: Config, claim_id: str, branch: str, destination: Path
) -> dict[str, Any]:
    status = _run(repo, [*checkout_options(repo), "status", "--porcelain"])
    if status is None:
        raise ValueError("Cannot determine source checkout status; Git inspection failed.")
    if status.stdout.strip():
        raise ValueError("A clean source checkout is required; preserve or commit your current work first.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,120}", branch) or ".." in branch:
        raise ValueError("Use a valid local branch name without options or '..'.")
    if destination.exists() or destination.resolve().is_relative_to(repo.resolve()):
        raise ValueError("Use a new worktree directory outside the source checkout.")
    result = static_scan(repo, config)
    selected = [f for f in result.findings if f.claim.claim_id == claim_id and f.is_drift]
    if not selected:
        raise ValueError("Finding is absent or no longer drift; scan again.")
    token = CACHE_DISABLED.set(True)
    try:
        plan = plan_rule_fixes(result.model_copy(update={"findings": selected}))
    finally:
        CACHE_DISABLED.reset(token)
    if not plan.edits:
        raise ValueError("No certain rule edit is available; review suggestions manually. No branch was created.")
    created = _run(repo, ["worktree", "add", "--no-checkout", "-b", branch, str(destination.resolve()), "HEAD"])
    if created is None:
        raise ValueError("Cannot create the isolated fix worktree; check branch/destination availability.")
    materialize_snapshot(repo, "HEAD", destination)
    if _run(destination, ["read-tree", "HEAD"]) is None:
        raise ValueError("Cannot initialize proposal index; nothing was pushed.")
    fresh = static_scan(destination, config)
    token = CACHE_DISABLED.set(True)
    try:
        plan = plan_rule_fixes(
            fresh.model_copy(
                update={"findings": [f for f in fresh.findings if f.claim.claim_id == claim_id and f.is_drift]}
            )
        )
    finally:
        CACHE_DISABLED.reset(token)
    if not plan.edits:
        raise ValueError("Proposal snapshot has no certain edit; inspect the worktree. Nothing was pushed.")
    for name in sorted({edit.file for edit in plan.edits}):
        apply_file(destination, plan, name)
    after = static_scan(destination, config)
    for edit in plan.edits:
        candidates = [f for f in after.findings if f.claim.doc_path == edit.file and f.claim.line == edit.line]
        if not candidates or any(f.verdict != "ok" for f in candidates):
            raise ValueError(
                f"Verification failed; inspect the uncommitted worktree at {destination}. Nothing was pushed."
            )
    files = sorted({edit.file for edit in plan.edits})
    for name in files:
        blob = _run(destination, ["hash-object", "-w", "--no-filters", "--", name])
        mode = _run(destination, ["ls-files", "--stage", "--", name])
        if blob is None or mode is None or not mode.stdout.strip():
            raise ValueError("Cannot stage the local proposal; nothing was pushed.")
        staged = _run(destination, ["update-index", "--cacheinfo", mode.stdout.split()[0], blob.stdout.strip(), name])
        if staged is None:
            raise ValueError("Cannot stage the local proposal; nothing was pushed.")
    committed = _run(
        destination,
        [
            *checkout_options(destination),
            "-c",
            "user.name=docs-drift",
            "-c",
            "user.email=docs-drift@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            f"Review documentation fix {claim_id[:16]}",
        ],
    )
    if committed is None:
        raise ValueError(f"Cannot save the local proposal; inspect {destination}. Nothing was pushed.")
    return {
        "schema_version": 1,
        "branch": branch,
        "worktree": str(destination.resolve()),
        "commit": resolve_revision(destination, "HEAD"),
        "files": files,
        "verified": True,
        "pushed": False,
        "approval": "Human review required; this tool-authored local proposal was not merged.",
    }
