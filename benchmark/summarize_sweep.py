"""Publish exact robustness outcomes and warning groups without treating timeouts as successes."""

import argparse
import json
import re
from collections import Counter, defaultdict

from benchmark.run import ROOT

EXAMPLE_LIMIT = 3


def category(message: str) -> str:
    for prefix, group in {
        "Tree-sitter found syntax errors in ": "Tree-sitter syntax errors: recognizable declarations only",
        "Skipped Python source ": "Skipped Python source: syntax or decoding error",
        "Cannot read documentation ": "Cannot read documentation",
        "Could not process ": "Could not process individual file",
    }.items():
        if message.startswith(prefix):
            return group
    for prefix in (
        "Source skipped: configuration nesting/line parser limit:",
        "Source skipped: non-regular, external, missing or over 1 MB:",
        "Source skipped: unreadable file:",
        "Source skipped: unsupported or overlong path:",
        "Skipped minified source ",
        "Cannot list tracked documentation with Git;",
    ):
        if message.startswith(prefix):
            return prefix.rstrip(": ;")
    if message.startswith("Skipped documentation "):
        return "Skipped documentation: " + message.partition(": ")[2]
    return re.sub(r"^.*?:\d+:", "<location>:", message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="2026-10-18-robustness-r2")
    args = parser.parse_args()
    run = ROOT / "benchmark/runs" / args.run
    statuses = json.loads((run / "status.json").read_text(encoding="utf-8"))
    repos = json.loads((run / "repos.json").read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    measurements = {}
    rows = []
    for repo in repos:
        name = repo["name"]
        status = statuses.get(name, {}).get("status", "not_run")
        measurement = None
        if status == "done":
            path = run / (name.replace("/", "--") + ".measurement.json")
            measurement = json.loads(path.read_text(encoding="utf-8"))
            measurements[name] = {
                **{key: value for key, value in measurement.items() if key not in {"warnings", "repo"}},
                "repo": ".docs-drift/repositories/" + name.replace("/", "--") + "/repository",
                "warning_count": len(measurement["warnings"]),
            }
            for warning in measurement["warnings"]:
                group = category(warning)
                counts[group] += 1
                if len(examples[group]) < EXAMPLE_LIMIT:
                    examples[group].append({"repo": name, "message": warning})
        rows.append(
            {
                "repo": name,
                "sha": repo["sha"],
                "status": status,
                "seconds": measurement["seconds"] if measurement else None,
                "warnings": len(measurement["warnings"]) if measurement else None,
            }
        )
    summary = {
        "warning_scope": "Completed scans only; partial warnings from timed-out scans are unavailable.",
        "repositories": rows,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "warning_groups": [
            {"message": message, "count": count, "examples": examples[message]}
            for message, count in counts.most_common()
        ],
    }
    (run / "robustness.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (run / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Robustness sweep — {run.name}",
        "",
        "Random selection and exact pins: repos.json; selection seed 20261021.",
        "Static extraction/index/matching only; no history, models, target execution or correctness assertions.",
        "Acquisition failures, timeouts and crashes are separate outcomes. A non-completed scan is never a pass.",
        "Warning totals cover completed scans only; partial warnings from timed-out scans are unavailable.",
        "",
        "| Repo | Status | Seconds | Warnings |",
        "| --- | --- | ---: | ---: |",
    ]
    lines.extend(f"| {row['repo']} | {row['status']} | {row['seconds']!r} | {row['warnings']} |" for row in rows)
    lines.extend(["", "## Warning groups", "", "| Message | Count |", "| --- | ---: |"])
    lines.extend(f"| {message.replace('|', '&#124;')} | {count} |" for message, count in counts.most_common())
    lines.extend(
        [
            "",
            "Exact warning examples and source hashes are retained in robustness.json and measurements.json.",
            "Sparse acquisition can omit binary/Go/Rust paths. Missing-file warnings can reflect acquisition limits; "
            "do not describe every such warning as a parser bug.",
            "",
        ]
    )
    (run / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary["status_counts"]))


if __name__ == "__main__":
    main()
