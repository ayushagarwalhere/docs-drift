"""Run the documentation-only extraction pipeline, isolating every input file."""

import fnmatch
import hashlib
import os
import re
import subprocess
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from docs_drift.config import Config
from docs_drift.docs.defaults import extract_defaults
from docs_drift.docs.discover import DiscoveryWarning, discover_docs, read_doc
from docs_drift.docs.extract_blocks import extract_block_claims, project_command_names
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.sections import ParsedDoc, is_historical_span, parse_document
from docs_drift.models import Claim, ClaimLocation, ScanResult, SuppressedClaim

MIN_EXTRACTION_CONFIDENCE = 0.3


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + time.perf_counter() - start


def _head(root: Path) -> str:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        value = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
            env=env,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        value = ""
    return value if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) else "0" * 40


def snapshot_time(root: Path) -> datetime:
    """Use the commit timestamp for reproducible snapshots; unversioned input uses the epoch."""
    try:
        value = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), "show", "-s", "--format=%ct", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return datetime.fromtimestamp(int(value.stdout.strip()), UTC)
    except (OSError, ValueError, subprocess.SubprocessError):
        return datetime(1970, 1, 1, tzinfo=UTC)


def _suppression(parsed: ParsedDoc, claim: Claim) -> str | None:  # noqa: PLR0911, PLR0912
    if "docs-drift-ignore" not in parsed.text:
        return None
    lines = parsed.lines
    if any("docs-drift-ignore-file" in line for line in lines[: min(len(lines), 100)]):
        return "docs-drift-ignore-file"
    if claim.line > 1 and "docs-drift-ignore-next-line" in lines[claim.line - 2]:
        return "docs-drift-ignore-next-line"
    if claim.line > 1 and "docs-drift-ignore" in lines[claim.line - 2]:
        return "docs-drift-ignore"
    # A paragraph/table/fence directive suppresses the following block through its closing boundary.
    for index, line in enumerate(lines[: claim.line - 1]):
        if "docs-drift-ignore" not in line or "next-line" in line or "file" in line:
            continue
        next_nonblank = next((n for n in range(index + 1, len(lines)) if lines[n].strip()), None)
        if next_nonblank is None or claim.line - 1 < next_nonblank:
            continue
        opening = lines[next_nonblank].lstrip()
        if opening.startswith("```") or opening.startswith("~~~"):
            marker = opening[:3]
            close = next(
                (n for n in range(next_nonblank + 1, len(lines)) if lines[n].lstrip().startswith(marker)), len(lines)
            )
            if next_nonblank <= claim.line - 1 <= close:
                return "docs-drift-ignore"
        if opening.startswith("|"):
            end = next_nonblank
            while end + 1 < len(lines) and lines[end + 1].strip() and lines[end + 1].lstrip().startswith("|"):
                end += 1
            if next_nonblank <= claim.line - 1 <= end:
                return "docs-drift-ignore"
        if not opening.startswith(("#", "|")):
            end = next_nonblank
            while (
                end + 1 < len(lines)
                and lines[end + 1].strip()
                and not lines[end + 1].lstrip().startswith(("#", "```", "~~~", "|"))
            ):
                end += 1
            if next_nonblank <= claim.line - 1 <= end:
                return "docs-drift-ignore"
    return None


def _ignored(claim: Claim, patterns: list[str]) -> bool:
    if claim.claim_id in patterns:
        return True
    return any(
        ":" in pattern
        and pattern.split(":", 1)[0] == claim.kind.value
        and fnmatch.fnmatchcase(claim.value, pattern.split(":", 1)[1])
        for pattern in patterns
    )


def _merge(parsed: ParsedDoc, claims: list[Claim]) -> list[Claim]:
    groups: dict[tuple[str, str, str | None, str], Claim] = {}
    for claim in claims:
        section = parsed.line_to_section.get(claim.line)
        if section is None:
            continue
        key = (claim.kind.value, claim.value, claim.qualifier, section.section_id)
        existing = groups.get(key)
        location = ClaimLocation(
            line=claim.line, column=claim.column, raw_text=claim.raw_text, extraction=claim.extraction
        )
        group_key = hashlib.sha256("\0".join(part or "" for part in key[:3]).encode()).hexdigest()
        if existing is None:
            claim.locations = [*claim.locations, location]
            claim.extraction_sources = list(dict.fromkeys([*claim.extraction_sources, claim.extraction]))
            claim.group_key = group_key
            groups[key] = claim
        else:
            existing.locations.append(location)
            existing.extraction_sources = list(dict.fromkeys([*existing.extraction_sources, claim.extraction]))
            if claim.extraction_confidence > existing.extraction_confidence:
                existing.extraction_confidence = claim.extraction_confidence
                existing.expected_default = claim.expected_default or existing.expected_default
    return list(groups.values())


def scan_claims(
    repo: Path,
    config: Config,
    *,
    doc_paths: set[str] | None = None,
    contents: dict[str, str] | None = None,
    source_repo: Path | None = None,
) -> ScanResult:
    """Extract claims without importing or running any code in the scanned repository."""
    root = repo.resolve()
    timings: dict[str, float] = {}
    docs = []
    sections = []
    claims: list[Claim] = []
    suppressed: list[SuppressedClaim] = []
    warnings_out: list[str] = []
    historical = 0
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DiscoveryWarning)
        with _timed(timings, "discover"):
            docs = discover_docs(root, config)
            if doc_paths is not None:
                docs = [doc for doc in docs if doc.path in doc_paths]
            if contents:
                from docs_drift.models import DocFile, DocFormat  # noqa: PLC0415 - optional dependency or shared pipeline import.

                for path, text in contents.items():
                    docs = [doc for doc in docs if doc.path != path]
                    docs.append(
                        DocFile(
                            path=path,
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),
                            format={".rst": DocFormat.rst, ".mdx": DocFormat.mdx}.get(
                                Path(path).suffix, DocFormat.markdown
                            ),
                        )
                    )
        warnings_out.extend(str(item.message) for item in caught)
    command_context = project_command_names(source_repo or root)
    for doc in docs:
        try:
            with _timed(timings, "parse"):
                parsed = parse_document(
                    doc, contents[doc.path] if contents and doc.path in contents else read_doc(root, doc)
                )
            sections.extend(parsed.sections)
            with _timed(timings, "inline"):
                inline = extract_claims(parsed)
            with _timed(timings, "blocks"):
                blocks = extract_block_claims(parsed, source_repo or root, command_context=command_context)
            historical += inline.historical_skipped + blocks.historical_skipped
            source = [claim for claim in [*inline.claims, *blocks.claims] if claim.kind.value != "default_value"]
            if doc.format.value == "rst":
                from docs_drift.docs.preprocess import rst_claims  # noqa: PLC0415 - optional dependency or shared pipeline import.

                source.extend(rst_claims(parsed))
            with _timed(timings, "defaults"):
                defaults = extract_defaults(parsed, source)
            with _timed(timings, "merge_filter"):
                merged = _merge(parsed, [*source, *defaults.claims])
                for claim in merged:
                    section = parsed.line_to_section.get(claim.line)
                    reason = _suppression(parsed, claim)
                    if reason:
                        suppressed.append(SuppressedClaim(claim=claim, reason=reason))
                    elif section is not None and is_historical_span(section, claim.line):
                        historical += 1
                    elif claim.extraction_confidence >= MIN_EXTRACTION_CONFIDENCE and not _ignored(
                        claim, config.ignore
                    ):
                        claims.append(claim)
                warnings_out.extend(defaults.debug_notes)
        except Exception as exc:  # A malformed document must not prevent useful scans of its neighbors.
            warnings_out.append(f"Could not process {doc.path}: {type(exc).__name__}: {exc}")
    return ScanResult(
        repo_path=root,
        head_sha=_head(root),
        scan_time=snapshot_time(source_repo or root),
        config_used=config,
        docs=docs,
        sections=sections,
        claims=claims,
        timings=timings,
        warnings=warnings_out,
        suppressed=suppressed,
        historical_ignored=historical,
    )
