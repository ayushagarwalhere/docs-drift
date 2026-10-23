"""Human decisions bound to a particular claim, verdict and source snapshot."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from docs_drift.index.base import source_files
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import ScanResult
from docs_drift.safe_files import metadata_text


class ReviewKind(StrEnum):
    stale = "stale"
    intentional = "intentional"
    false_positive = "false-positive"


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: ReviewKind
    reason: str = Field(min_length=1, max_length=2000)
    reviewer: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewBook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    decisions: list[ReviewDecision] = Field(default_factory=list, max_length=10000)
    _path: Path | None = PrivateAttr(default=None)


def fingerprints(result: ScanResult, review_path: Path | None = None) -> dict[str, str]:
    """Absence claims depend on the complete index; positive evidence is scoped to its source files."""
    repo = result.repo_path.resolve()
    hashes = {}
    for path in source_files(repo, result.config_used.exclude):
        if review_path is not None and path.resolve() == review_path.resolve():
            continue
        hashes[path.relative_to(repo).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    index_digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    values = {}
    for finding in result.findings:
        sources = {e.file for e in finding.evidence if e.file != finding.claim.doc_path}
        if finding.matched_symbol:
            sources.add(finding.matched_symbol.file)
        signature = {
            "claim_id": finding.claim.claim_id,
            "value": finding.claim.value,
            "qualifier": finding.claim.qualifier,
            "verdict": finding.verdict,
            "doc_text": finding.doc_text or finding.claim.raw_text,
            "sources": {name: hashes.get(name, "unavailable") for name in sorted(sources)},
            "index": index_digest if not finding.matched_symbol else None,
            "symbol": finding.matched_symbol.model_dump(mode="json") if finding.matched_symbol else None,
        }
        values[finding.claim.claim_id] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return values


def load_reviews(path: Path) -> ReviewBook:
    try:
        book = ReviewBook.model_validate_json(metadata_text(path))
        book._path = path
        return book
    except (OSError, ValidationError):
        raise ValueError(
            "Cannot read review decisions; use a bounded regular JSON file with schema_version 1."
        ) from None


def record_review(  # noqa: PLR0913, PLR0917 - explicit, required audit fields.
    result: ScanResult, path: Path, claim_id: str, decision: ReviewKind, reason: str, reviewer: str
) -> ReviewDecision:
    if not reason.strip() or not reviewer.strip():
        raise ValueError("A nonempty reason and reviewer are required.")
    current = fingerprints(result, path)
    if claim_id not in current:
        raise ValueError("Finding not present in this scan; scan again before reviewing.")
    entry = ReviewDecision(
        claim_id=claim_id,
        fingerprint=current[claim_id],
        decision=decision,
        reason=mask_secrets(reason.strip()),
        reviewer=mask_secrets(reviewer.strip()),
    )
    book = load_reviews(path) if path.exists() else ReviewBook()
    book.decisions = [d for d in book.decisions if (d.claim_id, d.fingerprint) != (claim_id, entry.fingerprint)] + [
        entry
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Review file must not be a symlink.")
    path.write_text(book.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return entry


def review_status(result: ScanResult, book: ReviewBook) -> dict[str, ReviewDecision]:
    current = fingerprints(result, book._path)
    return {entry.claim_id: entry for entry in book.decisions if current.get(entry.claim_id) == entry.fingerprint}


def suppressed_ids(result: ScanResult, book: ReviewBook) -> set[str]:
    return {key for key, entry in review_status(result, book).items() if entry.decision != "stale"}
