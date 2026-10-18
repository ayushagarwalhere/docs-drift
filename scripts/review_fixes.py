"""Print fixture patches, check git applicability, apply to temp copies and re-scan."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docs_drift.cli import _full_findings
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.fix.edits import apply_file, unified_patch
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.index import build_index
from docs_drift.models import ScanResult, Verdict
from tests.fixtures.build_fixtures import build_fixtures


def scan(repo: Path) -> ScanResult:
    result = scan_claims(repo, Config())
    index = build_index(repo, Config())
    findings, _ = _full_findings(result.claims, index, repo)
    return result.model_copy(update={"symbols": index.symbols, "findings": findings})


def main() -> int:
    fixed_total, suggestion_total = 0, 0
    with tempfile.TemporaryDirectory(prefix="docs-drift-fixes-") as directory:
        root = Path(directory)
        build_fixtures(root / "original")
        for name in ("py-cli", "fastapi-svc", "node-tool", "clean-repo"):
            repo = root / "fixed" / name
            shutil.copytree(root / "original" / name, repo)
            before = scan(repo)
            plan = plan_rule_fixes(before)
            patch = unified_patch(repo, plan)
            print(f"Repository: {name}\n{patch}")
            if patch:
                subprocess.run(["git", "-C", str(repo), "apply", "--check", "-"], input=patch.encode(), check=True)
            for relative in {edit.file for edit in plan.edits}:
                apply_file(repo, plan, relative)
            after = scan(repo)
            fixed_ids = {edit.claim_id for edit in plan.edits}
            for original in (item for item in before.findings if item.claim.claim_id in fixed_ids):
                same_location = [
                    item
                    for item in after.findings
                    if item.claim.doc_path == original.claim.doc_path
                    and item.claim.line == original.claim.line
                    and item.claim.kind == original.claim.kind
                ]
                if not same_location or not all(item.verdict == Verdict.ok for item in same_location):
                    print(f"NOT RESOLVED: {name}:{original.claim.line} {original.claim.value}")
                    return 1
            fixed = len(fixed_ids)
            remaining = sum(item.is_drift for item in before.findings) - fixed
            fixed_total += fixed
            suggestion_total += remaining
            print(
                f"{name}: {fixed} auto-fixable, {remaining} suggestion-only findings; all proposed fixes verified OK."
            )
    print(f"Total: {fixed_total} auto-fixable, {suggestion_total} suggestion-only findings.")
    print("Command: uv run python scripts/review_fixes.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
