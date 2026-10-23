"""Declared CODEOWNERS routing and explicit documentation coverage, without identity lookups."""

import re
from pathlib import Path
from typing import Any

from pathspec import GitIgnoreSpec

from docs_drift.models import ScanResult
from docs_drift.safe_files import metadata_text


def ownership(repo: Path, paths: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    result: dict[str, list[str]] = {path: [] for path in paths}
    warnings: list[str] = []
    selected = next(
        (repo / p for p in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS") if (repo / p).is_file()), None
    )
    if selected is None:
        return result, ["No CODEOWNERS file; review owners are unassigned."]
    for number, line in enumerate(metadata_text(selected).splitlines(), 1):
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        pattern, *owners = parts
        if (
            pattern.startswith("!")
            or any(c in pattern for c in "[]\\")
            or any(not (owner.startswith("@") or re.fullmatch(r"[^\s@]+@[^\s@]+", owner)) for owner in owners)
        ):
            warnings.append(f"Unsupported CODEOWNERS rule at line {number}; routing may be incomplete.")
            continue
        spec = GitIgnoreSpec.from_lines([pattern])
        for path in paths:
            if spec.match_file(path):
                result[path] = owners
    return result, warnings


def coverage_report(result: ScanResult, important: list[str] | None = None) -> dict[str, Any]:
    expected = important or []
    paths = sorted({doc.path for doc in result.docs} | set(expected))
    owners, warnings = ownership(result.repo_path, paths)
    rows = []
    for path in paths:
        findings = [finding for finding in result.findings if finding.claim.doc_path == path]
        claims = [claim for claim in result.claims if claim.doc_path == path]
        scanned = path in {doc.path for doc in result.docs}
        rows.append(
            {
                "path": path,
                "important": path in expected,
                "owners": owners[path],
                "status": "checked" if claims else "no-claims" if scanned else "not-checked",
                "claims": len(claims),
                "drift": sum(f.is_drift for f in findings),
                "unverifiable": sum(f.verdict == "unverifiable" for f in findings),
                "without_source_match": sum(
                    not f.matched_symbol and not any(e.file != f.claim.doc_path for e in f.evidence) for f in findings
                ),
            }
        )
    return {
        "schema_version": 1,
        "head_sha": result.head_sha,
        "documents": rows,
        "warnings": warnings,
        "limits": "Declared owners are not verified identities. Checked means claims were extracted, "
        "not that documentation is correct. Unsupported CODEOWNERS patterns are reported.",
    }
