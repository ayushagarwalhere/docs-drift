"""Shared ordering, badges and summaries."""

from collections.abc import Sequence

from docs_drift.models import Finding, ScanResult

RULES = {
    "missing": ("DD001", "MISSING", "Documented name is absent from the source index."),
    "renamed": ("DD002", "RENAMED", "Update the documentation to use the current name."),
    "default_changed": ("DD003", "DEFAULT", "The documented default differs from the code."),
    "invalid_command": ("DD004", "INVALID CMD", "The documented command is invalid."),
    "unverifiable": ("DD005", "UNVERIFIABLE", "Available static evidence is insufficient."),
    "version_mismatch": ("DD006", "VERSION", "The documented version disagrees with the package manifest."),
    "unknown_extra": ("DD007", "EXTRA", "The documented install extra is not defined by the manifest."),
    "ok": ("DD000", "OK", "The documented claim matches the code."),
}


# Dependency verdicts appear in summaries only when present, keeping existing report layouts stable.
_OPTIONAL_VERDICTS = {"version_mismatch", "unknown_extra"}


def verdict_counts(result: ScanResult) -> dict[str, int]:
    return {
        key: value for key, value in result.summary()["by_verdict"].items() if value or key not in _OPTIONAL_VERDICTS
    }


def ordered(result: ScanResult) -> list[Finding]:
    return sorted(result.findings, key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id))


def health(findings: Sequence[Finding]) -> int:
    checked = [f for f in findings if f.verdict != "unverifiable"]
    return round(100 * sum(not f.is_drift for f in checked) / len(checked)) if checked else 100


def message(f: Finding) -> str:
    text = f"{f.claim.value}: {f.verdict.value}. {f.suggestion or f.reasons[0]}"
    if f.introduced_by:
        commit = f.introduced_by
        text += f" Introduced by {commit.short_sha}: {commit.summary} ({commit.date.isoformat()})."
    return text
