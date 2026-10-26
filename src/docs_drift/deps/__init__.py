"""Dependency drift: documented dependency/runtime claims and code-versus-manifest usage.

Everything here reads manifests, documentation and source files as data. No package manager, build
backend, setup.py or package script is invoked, and no registry is contacted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from docs_drift.config import Config
from docs_drift.deps.check import DEPENDENCY_KINDS, check_dependency_claims
from docs_drift.deps.manifests import ManifestSet, load_manifests
from docs_drift.deps.usage import analyse_usage
from docs_drift.models import Claim, DependencyIssue, Finding

__all__ = ["DEPENDENCY_KINDS", "DependencyScan", "load_manifests", "scan_dependencies"]


@dataclass
class DependencyScan:
    findings: list[Finding] = field(default_factory=list)
    issues: list[DependencyIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifests: ManifestSet = field(default_factory=ManifestSet)
    unchecked_claims: set[str] = field(default_factory=set)


def scan_dependencies(
    repo: Path, config: Config, claims: Sequence[Claim], source_paths: Sequence[str], *, usage: bool | None = None
) -> DependencyScan:
    """Check documented dependency claims and, when requested, source imports against manifests."""
    root = repo.resolve()
    manifests = load_manifests(root, source_paths)
    scan = DependencyScan(manifests=manifests, warnings=list(manifests.warnings))
    dependency_claims = [claim for claim in claims if claim.kind in DEPENDENCY_KINDS]
    if config.deps.enabled or usage:
        scan.findings = check_dependency_claims(dependency_claims, manifests, root, config.deps)
    checked = {finding.claim.claim_id for finding in scan.findings}
    scan.unchecked_claims = {claim.claim_id for claim in dependency_claims if claim.claim_id not in checked}
    if config.deps.usage if usage is None else usage:
        result = analyse_usage(root, source_paths, manifests, config.deps)
        scan.issues = result.issues
        scan.warnings.extend(result.warnings)
    return scan
