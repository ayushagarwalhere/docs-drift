"""One repository index, with package-local matching and separate docs roots."""

import os
import subprocess
import time
from collections.abc import Callable
from contextlib import nullcontext, suppress
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from docs_drift.config import Config
from docs_drift.docs.pipeline import _head, scan_claims
from docs_drift.drift_age import age_findings
from docs_drift.errors import DocsDriftError
from docs_drift.index import build_index
from docs_drift.index.base import CodeIndex
from docs_drift.match import match_claims
from docs_drift.models import Claim, DependencyIssue, Finding, Package, ScanResult, Symbol
from docs_drift.workspaces import detect_packages, package_for


def remote_url(repo: Path) -> str | None:
    # Reuse the same hardened Git invocation as history inspection.
    try:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        value = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
            check=False,
        )
        return value.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def package_indexes(index: CodeIndex, packages: list[Package]) -> dict[str, CodeIndex]:
    symbols: dict[str, list[Symbol]] = {}
    for symbol in index.symbols:
        symbols.setdefault(package_for(symbol.file, packages).root, []).append(symbol)
    return {
        p.root: index
        if p.root == "."
        else CodeIndex(
            symbols=symbols.get(p.root, []),
            coverage=index.coverage,
            files_by_language=index.files_by_language,
            source_paths=index.source_paths,
            shared_path_index=(index.path_lookup, index.folded_paths),
        )
        for p in packages
    }


def scoped_findings(  # noqa: PLR0913 - matching context.
    claims: list[Claim],
    index: CodeIndex,
    repo: Path,
    packages: list[Package],
    *,
    history: bool = True,
    indexes: dict[str, CodeIndex] | None = None,
) -> tuple[list[Finding], list[str]]:
    from docs_drift.cli import _full_findings  # noqa: PLC0415 - optional dependency or shared pipeline import.

    grouped: dict[str, tuple[Package, list[Claim]]] = {}
    for claim in claims:
        package = package_for(claim.doc_path, packages)
        grouped.setdefault(package.root, (package, []))[1].append(claim)
    output: list[Finding] = []
    warnings: list[str] = []
    indexes = indexes or package_indexes(index, packages)
    for root, (package, group) in grouped.items():
        local = indexes[root]
        findings = match_claims(group, local, repo)
        retry = [f.claim for f in findings if f.verdict in {"missing", "unverifiable"}]
        fallback = (
            {f.claim.claim_id: f for f in match_claims(retry, index, repo) if f.verdict == "ok"} if root != "." else {}
        )
        findings = [
            fallback[f.claim.claim_id].model_copy(
                update={
                    "confidence": max(0, fallback[f.claim.claim_id].confidence - 0.08),
                    "reasons": [
                        *fallback[f.claim.claim_id].reasons,
                        "Matched the repository-wide index outside this package.",
                    ],
                }
            )
            if f.claim.claim_id in fallback
            else f
            for f in findings
        ]
        if history:
            findings, notes = _full_findings(group, local, repo, initial=findings)
            warnings.extend(notes)
        output.extend(f.model_copy(update={"package": package.name}) for f in findings)
    return output, warnings


def run_scan(  # noqa: PLR0913, PLR0912, PLR0915 - staged pipeline with optional progress events.
    repo: Path,
    config: Config,
    *,
    docs_repo: Path | None = None,
    package: str | None = None,
    progress: bool = False,
    history: bool = True,
    no_cache: bool = False,
    stage_callback: Callable[[str, dict[str, int]], None] | None = None,
    on_index: Callable[[CodeIndex, list[Package]], None] | None = None,
) -> ScanResult:
    """Scan ``repo``; ``on_index`` receives the static index, e.g. to keep it after the checkout is gone."""
    repo = repo.resolve()
    docs_root = (docs_repo or repo).resolve()
    if not docs_root.is_dir():
        raise DocsDriftError(
            f"Documentation directory does not exist: {docs_root}", "Choose an existing --docs-repo directory."
        )
    display = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        console=Console(stderr=True),
        transient=True,
        disable=not progress,
    )
    with display if progress else nullcontext():
        task = display.add_task("docs", total=None) if progress else None
        start = time.perf_counter()
        result = scan_claims(docs_root, config, source_repo=repo)
        if stage_callback:
            stage_callback("indexing", {"docs": len(result.docs), "claims": len(result.claims)})
        timings = {"docs": time.perf_counter() - start}
        if task is not None:
            display.update(task, description="index")
        start = time.perf_counter()
        index = build_index(repo, config, no_cache=no_cache)
        packages = detect_packages(repo, [repo / p for p in index.source_paths])
        timings["index"] = time.perf_counter() - start
        if on_index is not None:
            on_index(index, packages)
        if stage_callback:
            stage_callback(
                "matching", {"docs": len(result.docs), "claims": len(result.claims), "symbols": len(index.symbols)}
            )
        if package and package not in {p.name for p in packages}:
            raise DocsDriftError(f"Unknown package {package!r}.", "Choose from: " + ", ".join(p.name for p in packages))
        if package:
            selected = {c.claim_id for c in result.claims if package_for(c.doc_path, packages).name == package}
            result.claims = [c for c in result.claims if c.claim_id in selected]
            result.docs = [d for d in result.docs if package_for(d.path, packages).name == package]
        if task is not None:
            display.update(task, description="match")
        start = time.perf_counter()
        indexes = package_indexes(index, packages)
        findings, warnings = scoped_findings(result.claims, index, repo, packages, history=False, indexes=indexes)
        timings["match"] = time.perf_counter() - start
        if stage_callback:
            stage_callback(
                "history",
                {"docs": len(result.docs), "claims": len(result.claims), "findings": sum(f.is_drift for f in findings)},
            )
        if task is not None:
            display.update(task, description="history")
        start = time.perf_counter()
        if history:
            # Enrich local results without rebuilding the source index.
            from docs_drift.cli import _full_findings  # noqa: PLC0415 - optional dependency or shared pipeline import.

            enriched = []
            for p in packages:
                group = [f for f in findings if package_for(f.claim.doc_path, packages).root == p.root]
                if not group:
                    continue
                local = indexes[p.root]
                items, notes = _full_findings(
                    [f.claim for f in group], local, repo, initial=group, history_config=config.history
                )
                enriched.extend(items)
                warnings.extend(notes)
            findings = enriched
        timings["history"] = time.perf_counter() - start
        issues: list[DependencyIssue] = []
        if config.deps.enabled or config.deps.usage:
            from docs_drift.deps import scan_dependencies  # noqa: PLC0415 - optional dependency checks.

            start = time.perf_counter()
            dependencies = scan_dependencies(repo, config, result.claims, index.source_paths)
            findings.extend(
                f.model_copy(update={"package": package_for(f.claim.doc_path, packages).name})
                for f in dependencies.findings
            )
            result.claims = [c for c in result.claims if c.claim_id not in dependencies.unchecked_claims]
            issues = dependencies.issues
            warnings.extend(dependencies.warnings)
            timings["deps"] = time.perf_counter() - start
    doc_lines = {}
    for doc in result.docs:
        with suppress(OSError, UnicodeError):
            doc_lines[doc.path] = (docs_root / doc.path).read_text(encoding="utf-8").splitlines()
    for finding in findings:
        lines = doc_lines.get(finding.claim.doc_path, [])
        if finding.claim.line <= len(lines):
            finding.doc_text = lines[finding.claim.line - 1]
    head_sha = _head(repo)
    # scan_time is the source HEAD commit time, so drift ages are reproducible for a commit.
    findings = age_findings(findings, result.scan_time, head_sha, config.policy)
    return result.model_copy(
        update={
            "repo_path": repo,
            "docs_repo_path": docs_root if docs_repo else None,
            "head_sha": head_sha,
            "remote_url": remote_url(repo),
            "symbols": index.symbols,
            "findings": findings,
            "packages": packages,
            "timings": timings,
            "warnings": list(dict.fromkeys([*result.warnings, *index.warnings, *warnings])),
            "dependency_issues": issues,
        }
    )
