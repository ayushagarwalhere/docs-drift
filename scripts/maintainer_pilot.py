"""Prepare a frozen, report-only maintainer review using the existing benchmark label contract."""

import argparse
import hashlib
import json
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from benchmark.compute import read_labels, wilson
from benchmark.run import freeze, link, sample
from docs_drift.config import load_config
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.models import ScanResult
from docs_drift.report import render
from docs_drift.scanning import run_scan
from docs_drift.web.validation import validate_repository

MAX_REVIEW_FINDINGS = 100


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    reviewer: str = ""
    reviewer_role: Literal["pending", "maintainer", "contributor", "other"] = "pending"
    human_review_attested: bool = False
    review_minutes: float | None = Field(default=None, ge=0)
    would_keep_in_ci: bool | None = None
    decision: Literal["pending", "report-only", "new-drift-gate", "disabled"] = "pending"
    rationale: str = ""
    accepted_correction_urls: list[str] = Field(default_factory=list)


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _cell(value: str) -> str:
    return value.replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;").replace("<", "&lt;")


def prepare(repo: Path, out: Path, repo_url: str, *, seed: int = 20261023, limit: int = 25) -> None:
    """Scan a clean checkout once; never install/execute its code or prefill review labels."""
    repo, out = repo.resolve(), out.resolve()
    if out.exists() or out.is_relative_to(repo):
        raise ValueError(
            "Use a new output directory outside the target checkout; existing reviews are never overwritten."
        )
    if not 1 <= limit <= MAX_REVIEW_FINDINGS:
        raise ValueError("Review limit must be between 1 and 100.")
    repository = validate_repository(repo_url)
    dirty = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise ValueError("Use a clean target checkout so pinned documentation links describe the scanned files.")
    shallow = (
        subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"], text=True).strip()
        == "true"
    )
    start = time.monotonic()
    token = CACHE_DISABLED.set(True)
    try:
        result = run_scan(repo, load_config(repo), no_cache=True)
    finally:
        CACHE_DISABLED.reset(token)
    seconds = time.monotonic() - start
    if not result.head_sha.strip("0"):
        raise ValueError("A pinned Git checkout is required for maintainer review.")
    # Export through the standard privacy/redaction boundary before building the sheet.
    scan_text = render(result, "json")
    result = ScanResult.model_validate_json(scan_text)
    population = sorted(
        (f for f in result.findings if f.is_drift and f.confidence >= result.config_used.min_confidence),
        key=lambda f: (f.claim.claim_id, f.verdict),
    )
    selected = sample(population, limit, seed, repository.url)
    revision = {"repo_url": repository.url, "sha": result.head_sha}
    rows = [
        {
            "row_id": hashlib.sha256((f.claim.claim_id + f.verdict).encode()).hexdigest()[:20],
            "sample": "precision",
            "repo": repository.url,
            "tool_verdict": f.verdict.value,
            "kind": f.claim.kind.value,
            "statement": f.doc_text or f.claim.raw_text,
            "doc_link": link(revision, f.claim.doc_path, f.claim.line),
            "code_links": [link(revision, e.file, e.line) for e in f.evidence if e.file != f.claim.doc_path],
            "suggestion": f.suggestion or "",
        }
        for f in selected
    ]
    freeze(out, finding_seed=seed)
    (out / "scan.json").write_text(scan_text, encoding="utf-8")
    (out / "report.html").write_text(render(result, "html"), encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "repo_url": repository.url,
        "head_sha": result.head_sha,
        "shallow_history": shallow,
        "config": result.config_used.model_dump(mode="json"),
        "scan_seconds_excluding_clone": seconds,
        "selection": "Uniform deterministic sample of qualifying drift in one maintainer-selected repository.",
        "seed": seed,
        "qualifying_population": len(population),
        "selected": len(rows),
        "claims_extracted": len(result.claims),
        "unverifiable": sum(f.verdict == "unverifiable" for f in result.findings),
        "below_threshold_drift": sum(
            f.is_drift and f.confidence < result.config_used.min_confidence for f in result.findings
        ),
        "warnings": result.warnings,
        "rows": rows,
    }
    _write(out / "samples.json", metadata)
    lines = [
        "# Maintainer review: report-only",
        "",
        "Use benchmark/PROTOCOL.md criteria: true / false, with source citations and reasons.",
        "Leave unknown labels blank. No AI labels. Record attribution and CI willingness in feedback.json.",
        "False-positive notes: third-party:, historical:, example:, index-miss:, dynamic:, other:.",
        "",
        "| ID | Repo / sample | Exact documentation | Statement | Code evidence | "
        "Tool verdict | Suggestion | Human verdict | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        code = " ".join(f"[code]({url})" for url in row["code_links"]) or "No declaration evidence; inspect source."
        cells = [
            row["row_id"],
            repository.url,
            f"[doc]({row['doc_link']})",
            _cell(row["statement"]),
            code,
            row["tool_verdict"],
            _cell(row["suggestion"]),
            "",
            "",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    (out / "labels.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(out / "feedback.json", Feedback().model_dump())
    (out / "REVIEW.md").write_text(
        "# Report-only maintainer review\n\n"
        f"Repository: {repository.url}\n\nRevision: `{result.head_sha}`\n\n"
        f"Qualifying findings: {len(population)}; selected: {len(rows)}. "
        "These are predictions, not confirmed defects.\n\n"
        "1. Open report.html and inspect each selected statement in labels.md against the pinned source.\n"
        "2. Fill true/false and a source citation or explanation. Leave uncertain labels blank. "
        "Do not enter AI judgments as human labels.\n"
        "3. Fill feedback.json with your actual name/role, review time, human-review attestation, "
        "CI willingness, decision and reasons. A refusal is useful feedback.\n"
        "4. Return the edited labels.md and feedback.json to the project owner. No upload or message is automatic.\n\n"
        "The owner can run `python -m scripts.maintainer_pilot report <this-folder>` in the scanner source checkout. "
        "No CI gate changes automatically. Partial labels never become a precision number; recall is not measured. "
        "Review low-confidence findings, warnings and unverifiable counts in scan.json as well.\n",
        encoding="utf-8",
    )
    _write(
        out / "integrity.json",
        {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in ("scan.json", "samples.json")},
    )


def report(folder: Path) -> dict[str, Any]:
    """Pending/partial reviews never become an accuracy or adoption claim."""
    integrity = json.loads((folder / "integrity.json").read_text(encoding="utf-8"))
    for name in ("scan.json", "samples.json"):
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != integrity[name]:
            raise ValueError("Frozen pilot input changed; prepare a new run instead of relabeling changed results.")
    data = json.loads((folder / "samples.json").read_text(encoding="utf-8"))
    labels = read_labels(folder / "labels.md", data["rows"])
    feedback = Feedback.model_validate_json((folder / "feedback.json").read_text(encoding="utf-8"))
    reviewed = [value for value in labels.values() if value["verdict"]]
    has_review = reviewed or feedback.decision != "pending" or feedback.would_keep_in_ci is not None
    attributed = feedback.reviewer.strip() and feedback.human_review_attested and feedback.reviewer_role != "pending"
    if has_review and not attributed:
        raise ValueError("Human labels/decisions require a named reviewer, role and explicit human-review attestation.")
    if any(not value["note"].strip() for value in reviewed):
        raise ValueError("Every reviewed finding requires a source citation or explanation in its note.")
    positives = sum(value["verdict"] == "true" for value in reviewed)
    complete = bool(reviewed) and len(reviewed) == len(labels)
    summary = {
        "schema_version": 1,
        "status": "reviewed" if complete else "pending",
        "head_sha": data["head_sha"],
        "shallow_history": data["shallow_history"],
        "qualifying_population": data["qualifying_population"],
        "selected": len(labels),
        "labeled": len(reviewed),
        "pending": len(labels) - len(reviewed),
        "confirmed_defects": positives,
        "false_positives": len(reviewed) - positives,
        "sample_precision": positives / len(reviewed) if complete else None,
        "sample_wilson95": wilson(positives, len(reviewed)) if complete else None,
        "recall": None,
        "unverifiable": data["unverifiable"],
        "below_threshold_drift": data["below_threshold_drift"],
        "false_positive_categories": dict(
            Counter(value["note"].split(":", 1)[0] for value in reviewed if value["verdict"] == "false")
        ),
        "feedback_self_reported": feedback.model_dump(),
        "limits": "One selected repository; no representative product accuracy, recall or verified CI adoption. "
        "Zero findings do not prove coverage. Reviewer attribution and willingness are self-reported. "
        "No CI policy is changed by this report.",
    }
    _write(folder / "review-summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--repo", type=Path, required=True)
    prepare_parser.add_argument("--repo-url", required=True)
    prepare_parser.add_argument("--out", type=Path, required=True)
    prepare_parser.add_argument("--seed", type=int, default=20261023)
    prepare_parser.add_argument("--limit", type=int, default=25)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.repo, args.out, args.repo_url, seed=args.seed, limit=args.limit)
        print(f"Prepared report-only review at {args.out}. Human labels and feedback are pending.")
    else:
        print(json.dumps(report(args.folder), indent=2))


if __name__ == "__main__":
    main()
