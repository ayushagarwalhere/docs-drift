"""Compute human-labeled precision/Wilson intervals and a qualified weighted recall audit."""

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmark.run import ROOT, freeze
from docs_drift.fix.edits import unified_patch
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.models import ScanResult

LABEL_COLUMNS = 9
Z = 1.959963984540054


def wilson(successes: int, total: int) -> tuple[float, float] | None:
    if not 0 <= successes <= total:
        raise ValueError("Success count must be between zero and the sample size.")
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + Z * Z / total
    center = (p + Z * Z / (2 * total)) / denominator
    half = Z * math.sqrt(p * (1 - p) / total + Z * Z / (4 * total * total)) / denominator
    return max(0, center - half), min(1, center + half)


def read_labels(path: Path, rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    known = {row["row_id"]: row for row in rows}
    labels: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or cells[0] not in known:
            continue
        if len(cells) != LABEL_COLUMNS or cells[0] in labels:
            raise ValueError("Malformed or duplicate human row: " + cells[0])
        value = cells[7].casefold()
        allowed = (
            {"", "true", "false"}
            if known[cells[0]]["sample"] == "precision"
            else {"", "stale", "current", "not-a-claim"}
        )
        if value not in allowed:
            raise ValueError(f"Invalid human verdict {value!r} in {cells[0]}")
        labels[cells[0]] = {"verdict": value, "note": cells[8]}
    if set(labels) != set(known):
        raise ValueError("Label sheet is missing sampled rows; do not delete unlabelled rows.")
    return labels


def summarize(
    rows: list[dict[str, Any]], labels: dict[str, dict[str, str]], populations: dict[str, Any]
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pending = sum(not labels[row["row_id"]]["verdict"] for row in rows)
    for row in rows:
        if row["sample"] == "precision":
            groups["repo:" + row["repo"]].append(row)
            groups["verdict:" + row["tool_verdict"]].append(row)
    for repo in populations:
        groups.setdefault("repo:" + repo, [])
    for verdict in ("missing", "renamed", "default_changed", "invalid_command"):
        groups.setdefault("verdict:" + verdict, [])
    precision: dict[str, Any] = {}
    for name, sample in sorted(groups.items()):
        labeled = [labels[r["row_id"]]["verdict"] for r in sample if labels[r["row_id"]]["verdict"]]
        count, true = len(labeled), labeled.count("true")
        precision[name] = {
            "true": true,
            "labeled": count,
            "selected": len(sample),
            "precision": true / count if count and count == len(sample) else None,
            "wilson95": wilson(true, count) if count and count == len(sample) else None,
        }
    estimated_tp, estimated_missed = 0.0, 0.0
    for repo, population in populations.items():
        positive = [r for r in rows if r["repo"] == repo and r["sample"] == "precision"]
        if positive:
            estimated_tp += (
                population["precision"]
                * sum(labels[r["row_id"]]["verdict"] == "true" for r in positive)
                / len(positive)
            )
        for stratum, details in population["recall"].items():
            audit = [r for r in rows if r["repo"] == repo and r["sample"] == "recall" and r["stratum"] == stratum]
            if audit:
                estimated_missed += (
                    details["population"] * sum(labels[r["row_id"]]["verdict"] == "stale" for r in audit) / len(audit)
                )
    denominator = estimated_tp + estimated_missed
    return {
        "pending_labels": pending,
        "precision": precision,
        "estimated_true_positives": estimated_tp if not pending else None,
        "estimated_missed_stale_statements": estimated_missed if not pending else None,
        "weighted_audit_recall": estimated_tp / denominator if not pending and denominator else None,
        "recall_interval": None,
        "recall_scope": "ok claims and unextracted doc lines only; excludes unverifiable/low-confidence",
    }


def draft_upstream(run: Path, rows: list[dict[str, Any]], labels: dict[str, dict[str, str]], labeler: str) -> None:

    repos = {repo["name"]: repo for repo in json.loads((run / "repos.json").read_text(encoding="utf-8"))}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["sample"] == "precision" and labels[row["row_id"]]["verdict"] == "true":
            grouped[row["repo"]].append(row)
    target = run / "upstream-drafts"
    target.mkdir(exist_ok=True)
    for name, selected in list(sorted(grouped.items()))[:3]:
        result = ScanResult.model_validate_json((run / (name.replace("/", "--") + ".json")).read_text(encoding="utf-8"))
        result.repo_path = ROOT / repos[name]["local_path"]
        chosen = {row["id"] for row in selected}
        result.findings = [f for f in result.findings if f.claim.claim_id in chosen]
        plan = plan_rule_fixes(result)
        patch = unified_patch(result.repo_path, plan)
        slug = name.replace("/", "--")
        (target / f"{slug}.patch").write_text(patch, encoding="utf-8")
        body = [
            f"# Documentation correction for {name}",
            "",
            "Draft only — not submitted.",
            "",
            f"Found using docs-drift; {labeler} manually checked these statements at `{repos[name]['sha']}`.",
            "",
            *[
                f"- [Documentation]({r['doc_link']}): {r['statement']} — {labels[r['row_id']]['note']}"
                for r in selected
            ],
            "",
            "Could we update the documentation to match the current behavior? "
            "A generated patch is attached where the tool could propose a safe edit.",
            "Thank you for maintaining the project.",
        ]
        (target / f"{slug}.md").write_text("\n".join(body) + "\n", encoding="utf-8")


def read_measurement(run: Path, name: str) -> dict[str, Any]:
    """Read local raw data or the portable committed measurement summary."""
    measurement_path = run / (name.replace("/", "--") + ".measurement.json")
    if measurement_path.exists():
        result: dict[str, Any] = json.loads(measurement_path.read_text(encoding="utf-8"))
    else:
        result = json.loads((run / "measurements.json").read_text(encoding="utf-8"))[name]
    return result


def report(run: Path, sheet: Path, labeler: str | None, *, final: bool = False, drafts: bool = False) -> str:
    stamp = freeze(run)
    samples = json.loads((run / "samples.json").read_text(encoding="utf-8"))
    rows = samples["rows"]
    labels = read_labels(sheet, rows)
    summary = summarize(rows, labels, samples["populations"])
    if any(label["verdict"] for label in labels.values()) and not labeler:
        raise ValueError("Supply the human labeler's name when reporting any completed labels.")
    if (final or drafts) and (summary["pending_labels"] or not labeler):
        raise ValueError(
            "Human labels and the human labeler's name are required; no AI or blank labels can finalize accuracy."
        )
    label_hash = hashlib.sha256(sheet.read_bytes()).hexdigest()
    lock = run / "final-labels.sha256"
    if lock.exists() and lock.read_text(encoding="utf-8").strip() != label_hash:
        raise ValueError("Published labels changed; retain this result and create a new dated section.")
    lines = [
        f"# Results — {stamp['date']} / {run.name}",
        "",
        f"Scanner: docs-drift {stamp['version']}; commit `{stamp['scanner_commit']}`; dirty={stamp['dirty']}.",
        f"Actual scanner source SHA-256: `{stamp['source_sha256']}` (source archive retained).",
        "",
        f"Human labeler: **{labeler or 'not yet supplied'}**. Pending human labels: **{summary['pending_labels']}**.",
        "No AI labels were used. Blank labels are never counted as correct or incorrect.",
        "",
        "Commands: `python -m benchmark.acquire --clone`; `python -m benchmark.run scan --run "
        + run.name
        + f" --finding-seed {stamp['finding_seed']} --recall-seed {stamp['recall_seed']} --timeout-seconds 1200`; "
        "`python -m benchmark.run labels --run "
        + run.name
        + "`; `python -m benchmark.compute --run "
        + run.name
        + "`.",
        "",
        "## Precision (human labels; Wilson 95% intervals)",
        "",
        "| Group | True / labeled | Selected | Precision | Wilson 95% |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name, data in summary["precision"].items():
        interval = data["wilson95"]
        lines.append(
            f"| {name} | {data['true']}/{data['labeled']} | {data['selected']} | "
            + (f"{data['precision']:.6f}" if data["precision"] is not None else "pending")
            + " | "
            + (f"[{interval[0]:.6f}, {interval[1]:.6f}]" if interval else "pending")
            + " |"
        )
    recall = summary["weighted_audit_recall"]
    lines.extend(
        [
            "",
            "## Recall audit",
            "",
            "Weighted audit recall: " + (str(recall) if recall is not None else "pending / not estimable") + ".",
            "Stratified population weights are recorded in samples.json. This is not exhaustive recall; "
            "no binomial interval is assigned to the weighted ratio.",
            "The audit excludes unverifiable and low-confidence predicted findings.",
            "",
            "## Coverage and runtime",
            "",
            "| Repo | Status | Claims | Unverifiable / findings | Share | Scan seconds |",
            "| --- | --- | ---: | --- | ---: | ---: |",
        ]
    )
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    for repo in json.loads((run / "repos.json").read_text(encoding="utf-8")):
        name = repo["name"]
        if status.get(name, {}).get("status") != "done":
            lines.append(f"| {name} | {status.get(name, {}).get('status', 'not run')} | — | — | — | — |")
            continue
        measurement = read_measurement(run, name)
        counts = measurement["findings"]["by_verdict"]
        total = sum(counts.values())
        fraction = counts["unverifiable"] / total if total else 0.0
        lines.append(
            f"| {name} | done | {measurement['claims']} | {counts['unverifiable']}/{total} | "
            f"{fraction:.6f} | {measurement['seconds']!r} |"
        )
    false = [r for r in rows if r["sample"] == "precision" and labels[r["row_id"]]["verdict"] == "false"]
    patterns = Counter(labels[r["row_id"]]["note"].partition(":")[0].strip() or "uncategorized" for r in false)
    lines.extend(["", "## What went wrong", ""])
    if not false:
        lines.append(
            "Human false-positive patterns are pending labeling; this does not mean there were no false positives."
            if summary["pending_labels"]
            else "No false positives were found in the human-labeled sample; this does not establish perfect precision."
        )
    else:
        lines.extend(f"- {pattern}: {count}" for pattern, count in sorted(patterns.items()))
        lines.extend(f"- [Example]({r['doc_link']}): {labels[r['row_id']]['note']}" for r in false)
    lines.extend(
        [
            "",
            "Infrastructure/parser failures and raw warnings remain in status.json, "
            "per-repository logs and measurements.",
            "Repository-level sampling is uniform within each repo; pooled verdict precision "
            "is descriptive and can be composition-biased.",
            "A dirty source archive is required to reproduce this run; the scanner commit alone is insufficient.",
            "",
        ]
    )
    text = "\n".join(lines)
    (run / "RESULTS.md").write_text(text, encoding="utf-8")
    (ROOT / "docs/benchmark.md").write_text(
        text + "\nHuman labeling instructions and blank columns are in `benchmark/labels_todo.md` "
        "in the source checkout. Raw local scan files are reproducible with the commands above.\n",
        encoding="utf-8",
    )
    (run / "metrics.json").write_text(json.dumps(summary, indent=2) + "\n")
    if final:
        lock.write_text(label_hash + "\n")
        update_readme(run, stamp, summary, labeler)
    if drafts:
        draft_upstream(run, rows, labels, labeler or "")
    append_results_index(run, stamp)
    return text


def append_results_index(run: Path, stamp: dict[str, Any]) -> None:
    index = ROOT / "benchmark/RESULTS.md"
    previous = (
        index.read_text(encoding="utf-8")
        if index.exists()
        else "# Real-repository benchmark results\n\nAccuracy is pending human labeling. "
        "Fixture scores are not real-world accuracy.\n"
    )
    marker = f"runs/{run.name}/RESULTS.md"
    if marker not in previous:
        index.write_text(
            previous + f"\n## {stamp['date']} — {run.name}\n\n[Run tables and provenance]({marker}). "
            "Human labels are never AI-prefilled.\n",
            encoding="utf-8",
        )


def update_readme(run: Path, stamp: dict[str, Any], summary: dict[str, Any], labeler: str | None) -> None:
    readme = ROOT / "README.md"
    source = readme.read_text(encoding="utf-8")
    source = source.replace(
        "Real-world\nprecision and recall are **pending human labeling**, not inferred from fixtures.",
        "Human-labeled accuracy is reported below, including uncertainty and coverage limits.",
    )
    start, end = "<!-- accuracy:start -->", "<!-- accuracy:end -->"
    if start in source and end in source:
        table = [
            f"Human review by **{labeler}**, {stamp['date']}, run `{run.name}`.",
            "Static matching is partial. These results include false positives; inspect the intervals.",
            "",
            "| Verdict | True / labeled | Precision | Wilson 95% |",
            "| --- | ---: | ---: | --- |",
        ]
        for key, data in summary["precision"].items():
            if not key.startswith("verdict:"):
                continue
            interval = data["wilson95"]
            value = f"{data['precision']:.6f}" if data["precision"] is not None else "not estimable"
            bounds = f"[{interval[0]:.6f}, {interval[1]:.6f}]" if interval else "not estimable"
            table.append(f"| {key.removeprefix('verdict:')} | {data['true']}/{data['labeled']} | {value} | {bounds} |")
        table.append(f"\n[Per-repository results and audit limits](benchmark/runs/{run.name}/RESULTS.md).")
        before, rest = source.split(start, 1)
        _, after = rest.split(end, 1)
        readme.write_text(before + start + "\n" + "\n".join(table) + "\n" + end + after, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="2026-10-18-r4")
    parser.add_argument("--labels", type=Path, default=ROOT / "benchmark/labels_todo.md")
    parser.add_argument("--labeler")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--drafts", action="store_true")
    args = parser.parse_args()
    print(report(ROOT / "benchmark/runs" / args.run, args.labels, args.labeler, final=args.final, drafts=args.drafts))


if __name__ == "__main__":
    main()
