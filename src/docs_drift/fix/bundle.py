"""A reviewable fix bundle: the certain rule edits for one scanned revision, as a patch.

The bundle reuses `plan_rule_fixes` and `unified_patch`, so it contains exactly the
edits `docs-drift fix` would preview for the same scan. Model rewrites are never
included. A file's edits are withheld, not partially applied, when its patch looks
secret-bearing, cannot be expressed as a UTF-8 unified diff, or would exceed the
size limit.
"""

from collections import defaultdict
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from docs_drift.fix.edits import FixPlan, StaleDocumentError, TextEdit, unified_patch
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.limits import MAX_FIX_BUNDLE_BYTES, MAX_PATCH_BYTES
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import GitSHA, PipelineModel, RepoPath, ScanResult


class FixStatus(StrEnum):
    patched = "patched"
    manual = "manual"
    withheld = "withheld"


class FileFix(PipelineModel):
    path: RepoPath
    edits: int = Field(ge=0)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class FindingFix(PipelineModel):
    claim_id: str
    status: FixStatus
    note: str


class FixBundle(PipelineModel):
    """Certain documentation edits for one revision; `patch` applies with `git apply`."""

    head_sha: GitSHA
    patch: str = ""
    files: list[FileFix] = Field(default_factory=list)
    findings: list[FindingFix] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.patch)

    def status(self) -> dict[str, FixStatus]:
        return {item.claim_id: item.status for item in self.findings}

    def count(self, status: FixStatus) -> int:
        return sum(item.status == status for item in self.findings)


def _patch_counts(text: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++ "):
            additions += 1
        elif line.startswith("-") and not line.startswith("--- "):
            deletions += 1
    return additions, deletions


def _withhold(statuses: dict[str, FindingFix], edits: list[TextEdit], note: str) -> None:
    for edit in edits:
        statuses[edit.claim_id] = FindingFix(claim_id=edit.claim_id, status=FixStatus.withheld, note=note)


def build_fix_bundle(result: ScanResult, repo: Path, *, max_patch_bytes: int = MAX_PATCH_BYTES) -> FixBundle:
    """Plan certain rule edits against the checkout at `repo` and render a git-applyable patch.

    `repo` must be the checkout the scan read; every document is hash-checked first,
    so a changed file yields an empty bundle rather than a patch for different text.
    """
    scoped = result.model_copy(update={"repo_path": repo, "docs_repo_path": None})
    drift = [finding for finding in result.findings if finding.is_drift]
    bundle = FixBundle(head_sha=result.head_sha)
    manual = "No certain edit; a maintainer needs to decide the correct wording."
    statuses = {
        finding.claim.claim_id: FindingFix(claim_id=finding.claim.claim_id, status=FixStatus.manual, note=manual)
        for finding in drift
    }
    try:
        plan = plan_rule_fixes(scoped)
    except (StaleDocumentError, OSError):
        bundle.notes.append("Documents changed after the scan, so no patch was prepared. Scan again.")
        bundle.findings = list(statuses.values())
        return bundle
    by_file: dict[str, list[TextEdit]] = defaultdict(list)
    for edit in plan.edits:
        by_file[edit.file].append(edit)
    parts: list[str] = []
    used = 0
    for relative in sorted(by_file):
        edits = by_file[relative]
        try:
            text = unified_patch(repo, FixPlan(edits=edits, hashes=plan.hashes))
        except (ValueError, OSError):
            _withhold(statuses, edits, "This file cannot be patched safely as UTF-8 text; edit it by hand.")
            continue
        if not text:
            continue
        if mask_secrets(text) != text:
            _withhold(statuses, edits, "The surrounding lines look secret-bearing, so the edit is not published.")
            continue
        size = len(text.encode("utf-8"))
        if used + size > max_patch_bytes:
            _withhold(statuses, edits, "The patch reached the size limit; run `docs-drift fix` locally.")
            continue
        used += size
        parts.append(text)
        additions, deletions = _patch_counts(text)
        bundle.files.append(FileFix(path=relative, edits=len(edits), additions=additions, deletions=deletions))
        for edit in edits:
            statuses[edit.claim_id] = FindingFix(
                claim_id=edit.claim_id, status=FixStatus.patched, note=f"{edit.reason} Included in fix.patch."
            )
    bundle.patch = "".join(parts)
    # Per-finding "no certain edit" suggestions are already carried by the manual statuses.
    bundle.notes.extend(mask_secrets(note) for note in plan.suggestions if "no certain rule edit" not in note)
    bundle.findings = list(statuses.values())
    if len(bundle.model_dump_json().encode("utf-8")) > MAX_FIX_BUNDLE_BYTES:
        bundle.notes = bundle.notes[:20]
        bundle.findings = [item for item in bundle.findings if item.status != FixStatus.manual]
    return bundle
