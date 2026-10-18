"""Enforce overall and extraction/matching coverage without rounding failures into passes."""

import json
from pathlib import Path


def main() -> None:
    data = json.loads(Path(".docs-drift/coverage.json").read_text())
    failed = False
    for prefix, target in [("", 85), ("src/docs_drift/docs/", 90), ("src/docs_drift/match/", 90)]:
        selected = [v["summary"] for name, v in data["files"].items() if name.replace("\\", "/").startswith(prefix)]
        covered = sum(row["covered_lines"] for row in selected)
        total = sum(row["num_statements"] for row in selected)
        passed = bool(total) and covered * 100 >= target * total
        print(f"{prefix or 'overall'}: {covered}/{total}; target {target}%; {'PASS' if passed else 'FAIL'}")
        failed |= not passed
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
