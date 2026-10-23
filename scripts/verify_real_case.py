"""Verify the reviewed rich-cli correction on an archived pin without executing target code."""

import argparse
import hashlib
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

from docs_drift.config import Config
from docs_drift.fix.edits import FixPlan, TextEdit, apply_file, unified_patch
from docs_drift.models import ClaimKind, ScanResult, Verdict
from docs_drift.scanning import run_scan

PIN = "46f4d2469097be395558768714a5f07ebccf1412"
CORRECTIONS = {61: ("--line-number", "--line-numbers"), 180: ("--p", "-p")}


def correction_plan(result: ScanResult, readme: str) -> FixPlan:
    """These two edits are explicitly reviewed proposals, never inferred rename evidence."""
    plan = FixPlan(hashes={"README.md": hashlib.sha256(readme.encode()).hexdigest()})
    for line, (old, new) in CORRECTIONS.items():
        finding = next(
            (
                f
                for f in result.findings
                if f.claim.doc_path == "README.md" and f.claim.line == line and f.claim.value == old
            ),
            None,
        )
        if finding is None or finding.verdict != Verdict.missing or finding.claim.kind != ClaimKind.cli_flag:
            raise ValueError(f"Expected missing-option finding absent at README.md:{line}; review the input.")
        text = readme.splitlines()[line - 1]
        matches = list(re.finditer(rf"(?<![\w-]){re.escape(old)}(?![\w-])", text))
        if len(matches) != 1:
            raise ValueError(f"Expected unique reviewed option at README.md:{line}.")
        start, end = matches[0].span()
        plan.edits.append(
            TextEdit(
                file="README.md",
                line=line,
                column=start + 1,
                end_line=line,
                end_column=end + 1,
                old_text=old,
                new_text=new,
                claim_id=finding.claim.claim_id,
                reason="Explicit agent-reviewed real-case proposal; not an automatic rename inference.",
            )
        )
    return plan


def verify(repo: Path, output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("Use a new output directory; existing evidence is never overwritten.")
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=zip", PIN])
    with tempfile.TemporaryDirectory(prefix="docs-drift-real-case-") as directory:
        root = Path(directory).resolve()
        with ZipFile(io.BytesIO(archive)) as snapshot:
            if any(not (root / name).resolve().is_relative_to(root) for name in snapshot.namelist()):
                raise ValueError("Archive contains a path outside the snapshot.")
            snapshot.extractall(root)
        readme = root / "README.md"
        before_bytes = readme.read_bytes()
        before = run_scan(root, Config(), history=False, no_cache=True)
        plan = correction_plan(before, before_bytes.decode("utf-8"))
        patch = unified_patch(root, plan)
        subprocess.run(
            ["git", "-C", str(root), "apply", "--check", "-"], input=patch.encode(), check=True, capture_output=True
        )
        apply_file(root, plan, "README.md")
        after = run_scan(root, Config(), history=False, no_cache=True)
        rows = []
        for line, (old, new) in CORRECTIONS.items():
            resolved = [
                f
                for f in after.findings
                if f.claim.doc_path == "README.md"
                and f.claim.line == line
                and f.claim.kind == ClaimKind.cli_flag
                and f.claim.value == new
            ]
            if not resolved or any(f.verdict != Verdict.ok or f.matched_symbol is None for f in resolved):
                raise ValueError(f"Corrected option at README.md:{line} was not verified against source.")
            rows.append({"line": line, "old": old, "new": new, "before": "missing", "after": "ok"})
        summary: dict[str, object] = {
            "source_revision": PIN,
            "snapshot_archive_sha256": hashlib.sha256(archive).hexdigest(),
            "original_readme_sha256": hashlib.sha256(before_bytes).hexdigest(),
            "corrected_readme_sha256": hashlib.sha256(readme.read_bytes()).hexdigest(),
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "git_apply_check": "passed",
            "review": "Two agent-reviewed edits; not automatic fixes, human labels, or an upstream submission.",
            "target_code_executed": False,
            "history_used": False,
            "rows": rows,
            "limits": "Only these two option claims are verified; no overall accuracy, recall or runtime claim.",
        }
        output.mkdir(parents=True)
        (output / "rich-cli-correction.patch").write_bytes(patch.encode())
        (output / "correction-verification.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.repo, args.out), indent=2))


if __name__ == "__main__":
    main()
