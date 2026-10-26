"""The `docs-drift deps` report: documentation checks, code usage, PR-diff awareness and baselines."""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docs_drift.config import Config
from docs_drift.deps import DEPENDENCY_KINDS, DependencyScan, scan_dependencies
from docs_drift.deps.manifests import Declared, Ecosystem, ManifestSet
from docs_drift.deps.usage import make_issue
from docs_drift.models import DependencyIssue, DependencyIssueKind, Evidence, Finding, PipelineModel, RepoPath
from docs_drift.report.markdown import escape
from docs_drift.safe_files import metadata_text

MIN_MENTION_CHARS = 3
MAX_EXAMPLES = 3
_INLINE = re.compile(r"`([^`\n]+)`")


class ManifestSummary(PipelineModel):
    path: RepoPath
    ecosystem: Literal["python", "node"]
    project: str | None = None
    runtime: str | None = None
    dependencies: int = Field(ge=0)


class DepsReport(PipelineModel):
    """Stable JSON contract for `docs-drift deps --format json`."""

    schema_version: Literal[1] = 1
    head_sha: str
    base_sha: str | None = None
    manifests: list[ManifestSummary] = Field(default_factory=list)
    claims_checked: int = Field(default=0, ge=0)
    findings: list[Finding] = Field(default_factory=list)
    issues: list[DependencyIssue] = Field(default_factory=list)
    baselined: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class DepsBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    findings: list[tuple[str, str]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def _summaries(manifests: ManifestSet) -> list[ManifestSummary]:
    rows = []
    for manifest in manifests.manifests:
        runtime = next((r for r in manifest.runtime if r.authoritative), None) or next(iter(manifest.runtime), None)
        rows.append(
            ManifestSummary(
                path=manifest.path,
                ecosystem=manifest.ecosystem,
                project=manifest.project_name,
                runtime=runtime.spec if runtime else None,
                dependencies=len(manifest.dependencies),
            )
        )
    return rows


def _code_text(root: Path, docs: list[str]) -> list[tuple[str, int, str]]:
    """(doc, line, text) for inline code spans and fenced lines, where package names are meaningful."""
    rows: list[tuple[str, int, str]] = []
    for doc in docs:
        try:
            lines = metadata_text(root / doc).splitlines()
        except (OSError, UnicodeError):
            continue
        fenced = False
        for number, line in enumerate(lines, 1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                rows.append((doc, number, line))
            else:
                rows.extend((doc, number, span) for span in _INLINE.findall(line))
    return rows


def _first_mention(rows: list[tuple[str, int, str]], declaration: Declared) -> tuple[str, int] | None:
    variants = {declaration.name, declaration.key}
    if declaration.ecosystem == "python":
        variants.add(declaration.key.replace("-", "_"))
    patterns = [
        re.compile(r"(?<![\w./@-])" + re.escape(v) + r"(?![\w/-])", re.I)
        for v in variants
        if len(v) >= MIN_MENTION_CHARS
    ]
    for doc, line, text in rows:
        if any(pattern.search(text) for pattern in patterns):
            return doc, line
    return None


def _runtime_declarations(manifests: ManifestSet, ecosystem: Ecosystem) -> dict[str, Declared]:
    selected: dict[str, Declared] = {}
    projects = manifests.project_names(ecosystem)
    for declaration in manifests.declared(ecosystem):
        if declaration.group == "runtime" and declaration.key not in projects:
            selected.setdefault(declaration.key, declaration)
    return selected


def manifest_diff_issues(
    base: ManifestSet, head: ManifestSet, rows: list[tuple[str, int, str]], base_sha: str
) -> list[DependencyIssue]:
    """New runtime dependencies the docs never mention, and removed ones the docs still name."""
    issues: list[DependencyIssue] = []
    short = base_sha[:12]
    ecosystems: tuple[Ecosystem, ...] = ("python", "node")
    for ecosystem in ecosystems:
        before = _runtime_declarations(base, ecosystem)
        after = _runtime_declarations(head, ecosystem)
        head_any = {d.key for d in head.declared(ecosystem)}
        # Only docs that already name dependencies are expected to name a new one.
        mentioned_existing = sorted(
            item.name for key, item in after.items() if key in before and _first_mention(rows, item)
        )
        for key, item in sorted(after.items()):
            if key in before or not mentioned_existing or _first_mention(rows, item):
                continue
            examples = ", ".join(mentioned_existing[:MAX_EXAMPLES])
            issues.append(
                make_issue(
                    DependencyIssueKind.added_undocumented,
                    ecosystem,
                    item.name,
                    key,
                    item.manifest,
                    f"{item.name} became a runtime dependency after {short}, but the documentation never names it "
                    f"while it does name other dependencies ({examples}). Update the installation docs.",
                    [Evidence(file=item.manifest, line=item.line, text=item.name)],
                    0.6,
                )
            )
        for key, item in sorted(before.items()):
            if key in head_any:
                continue
            mention = _first_mention(rows, item)
            if mention is None:
                continue
            doc, line = mention
            issues.append(
                make_issue(
                    DependencyIssueKind.removed_documented,
                    ecosystem,
                    item.name,
                    key,
                    item.manifest,
                    f"{item.name} was removed from {item.manifest} after {short}, but {doc}:{line} still names it.",
                    [Evidence(file=doc, line=line, text=item.name)],
                    0.65,
                )
            )
    return issues


def _scan(root: Path, config: Config) -> tuple[DependencyScan, int, list[str], list[str]]:
    from docs_drift.docs.pipeline import scan_claims  # noqa: PLC0415 - avoids an import cycle at load time.
    from docs_drift.index.base import source_files  # noqa: PLC0415

    warnings: list[str] = []
    paths = [p.relative_to(root).as_posix() for p in source_files(root, config.exclude, warnings_out=warnings)]
    enabled = config.model_copy(update={"deps": config.deps.model_copy(update={"enabled": True})})
    docs = scan_claims(root, enabled)
    scan = scan_dependencies(root, enabled, docs.claims, paths, usage=True)
    claims = sum(c.kind in DEPENDENCY_KINDS and c.claim_id not in scan.unchecked_claims for c in docs.claims)
    lines: dict[str, list[str]] = {}
    for finding in scan.findings:
        path = finding.claim.doc_path
        if path not in lines:
            try:
                lines[path] = metadata_text(root / path).splitlines()
            except (OSError, UnicodeError):
                lines[path] = []
        if finding.claim.line <= len(lines[path]):
            finding.doc_text = lines[path][finding.claim.line - 1]
    # Extraction notes about unrelated claim kinds belong to `scan`, not to this report.
    doc_warnings = [w for w in docs.warnings if w.startswith("Could not process")]
    return scan, claims, [d.path for d in docs.docs], [*doc_warnings, *warnings, *scan.warnings]


def load_deps_baseline(path: Path) -> DepsBaseline:
    try:
        return DepsBaseline.model_validate_json(metadata_text(path))
    except ValidationError:
        raise ValueError(
            f"Invalid dependency baseline at {path}. Regenerate it with docs-drift deps --write-baseline <path>."
        ) from None
    except OSError as exc:
        raise ValueError(f"Cannot read dependency baseline {path}: {exc}.") from exc


def save_deps_baseline(path: Path, report: DepsReport) -> None:
    baseline = DepsBaseline(
        findings=sorted({(f.claim.claim_id, f.verdict.value) for f in report.findings if f.is_drift}),
        issues=sorted({issue.issue_id for issue in report.issues}),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")


def apply_baseline(report: DepsReport, baseline: DepsBaseline) -> DepsReport:
    known_findings = {tuple(item) for item in baseline.findings}
    known_issues = set(baseline.issues)
    findings = [
        f for f in report.findings if not (f.is_drift and (f.claim.claim_id, f.verdict.value) in known_findings)
    ]
    issues = [issue for issue in report.issues if issue.issue_id not in known_issues]
    hidden = sum(f.is_drift for f in report.findings) - sum(f.is_drift for f in findings)
    return report.model_copy(
        update={
            "findings": findings,
            "issues": issues,
            "baselined": hidden + len(report.issues) - len(issues),
        }
    )


def run_deps(repo: Path, config: Config, *, base: str | None = None) -> DepsReport:
    """Build the dependency report; with `base`, keep only drift and issues absent at that revision."""
    from docs_drift.docs.pipeline import _head  # noqa: PLC0415

    root = repo.resolve()
    scan, claims, docs, warnings = _scan(root, config)
    report = DepsReport(
        head_sha=_head(root),
        manifests=_summaries(scan.manifests),
        claims_checked=claims,
        findings=sorted(scan.findings, key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id)),
        issues=scan.issues,
        warnings=list(dict.fromkeys(warnings)),
    )
    if base is None:
        return report
    from docs_drift.snapshots import materialize_snapshot  # noqa: PLC0415 - Git archive only for PR mode.

    with tempfile.TemporaryDirectory(prefix="docs-drift-deps-") as directory:
        base_root = Path(directory) / "source"
        sha = materialize_snapshot(root, base, base_root)
        before, _, _, base_warnings = _scan(base_root.resolve(), config)
    known_findings = {(f.claim.claim_id, f.verdict) for f in before.findings if f.is_drift}
    known_issues = {issue.issue_id for issue in before.issues}
    findings = [f for f in report.findings if f.is_drift and (f.claim.claim_id, f.verdict) not in known_findings]
    issues = [issue for issue in report.issues if issue.issue_id not in known_issues]
    issues.extend(manifest_diff_issues(before.manifests, scan.manifests, _code_text(root, docs), sha))
    issues.sort(key=lambda i: (i.ecosystem, i.manifest or "", i.kind.value, i.name.casefold()))
    base_warnings = [w.replace(str(base_root), "<base>") for w in base_warnings]
    return report.model_copy(
        update={
            "base_sha": sha,
            "findings": findings,
            "issues": issues,
            "warnings": list(dict.fromkeys([*report.warnings, *(f"base: {w}" for w in base_warnings)])),
        }
    )


def visible(report: DepsReport, min_confidence: float, show_all: bool) -> DepsReport:
    return report.model_copy(
        update={
            "findings": [
                f for f in report.findings if (show_all or f.is_drift) and (show_all or f.confidence >= min_confidence)
            ],
            "issues": [i for i in report.issues if show_all or i.confidence >= min_confidence],
        }
    )


def render_json(report: DepsReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=True, indent=2, sort_keys=True) + "\n"


_LABELS = {
    "version_mismatch": "VERSION",
    "unknown_extra": "EXTRA",
    "missing": "MISSING",
    "ok": "OK",
    "unverifiable": "UNVERIFIABLE",
}


def _counts(report: DepsReport) -> str:
    drift = sum(f.is_drift for f in report.findings)
    kinds = Counter(issue.kind.value for issue in report.issues)
    detail = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    return f"{drift} documentation drift · {len(report.issues)} code issue(s)" + (f" ({detail})" if detail else "")


def render_terminal(report: DepsReport) -> str:
    lines = [
        f"Dependency drift · {report.head_sha[:8]}"
        + (f" vs {report.base_sha[:8]}" if report.base_sha else "")
        + f" · {len(report.manifests)} manifest(s) · {report.claims_checked} documentation claim(s) checked",
        "",
    ]
    if report.manifests:
        lines.append("Manifests")
        for manifest in report.manifests:
            runtime = f", {manifest.ecosystem} {manifest.runtime}" if manifest.runtime else ""
            lines.append(f"  {manifest.path} ({manifest.dependencies} dependencies{runtime})")
        lines.append("")
    if report.findings:
        lines.append("Documentation vs manifests")
        for f in report.findings:
            label = _LABELS.get(f.verdict.value, f.verdict.value.upper())
            lines.append(
                f"  {label} {f.claim.doc_path}:{f.claim.line}  {f.doc_text.strip() if f.doc_text else f.claim.raw_text}"
            )
            lines.append(f"    → {f.suggestion + ': ' if f.suggestion else ''}{f.reasons[0]}")
            lines.extend(f"    evidence {e.file}:{e.line}  {e.text}" for e in f.evidence[:2])
        lines.append("")
    if report.issues:
        lines.append("Code vs manifests")
        for issue in report.issues:
            lines.append(
                f"  {issue.kind.value.upper()} {issue.ecosystem} {issue.name}"
                + (f" ({issue.manifest})" if issue.manifest else "")
                + f" · {issue.confidence:.0%}"
            )
            lines.append(f"    {issue.message}")
            lines.extend(f"    {e.file}:{e.line}  {e.text}" for e in issue.evidence)
        lines.append("")
    lines.append(_counts(report) + (f" · baselined {report.baselined}" if report.baselined else ""))
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"


def _cell(text: object) -> str:
    """Untrusted names and document text: HTML, links, images and @mentions are neutralised."""
    return escape(text)


def render_markdown(report: DepsReport) -> str:
    out = [f"# Dependency drift\n\n{_counts(report)}\n"]
    if report.findings:
        out.append("\n## Documentation vs manifests\n\n| Verdict | Location | Claim | Evidence | Suggestion |\n")
        out.append("| --- | --- | --- | --- | --- |\n")
        for f in report.findings:
            evidence = "; ".join(f"{e.file}:{e.line}" for e in f.evidence[:2])
            out.append(
                f"| {f.verdict.value} | {_cell(f.claim.doc_path)}:{f.claim.line} | {_cell(f.claim.raw_text)} | "
                f"{_cell(evidence)} | {_cell(f.suggestion or f.reasons[0])} |\n"
            )
    if report.issues:
        out.append("\n## Code vs manifests\n\n| Kind | Package | Manifest | Evidence | Detail |\n")
        out.append("| --- | --- | --- | --- | --- |\n")
        for issue in report.issues:
            evidence = "; ".join(f"{e.file}:{e.line}" for e in issue.evidence)
            out.append(
                f"| {issue.kind.value} | {_cell(issue.name)} | {_cell(issue.manifest or '')} | {_cell(evidence)} | "
                f"{_cell(issue.message)} |\n"
            )
    return "".join(out)
