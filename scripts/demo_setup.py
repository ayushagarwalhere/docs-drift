"""Build an explicitly synthetic, reproducible recording project without overwriting existing work."""

import argparse
import json
from pathlib import Path

from docs_drift.config import Config
from docs_drift.fix.edits import render_file
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.report import render
from docs_drift.scanning import run_scan
from tests.fixtures.build_fixtures import Commit, Repository, _build_repo

README = """# Parcel: a small preview server

Synthetic recording fixture, not a real customer incident.

## Local preview

The `serve` command accepts `--port`.
The `--workers` default is `4`.
"""
SOURCE = '''"""Serve a local preview (synthetic demonstration only)."""
import argparse

def main():
    parser = argparse.ArgumentParser(prog="parcel")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(f"Preview available on {args.port}")

if __name__ == "__main__":
    main()
'''


def prepare(output: Path) -> dict[str, object]:
    """Create fixed authored history, warm scanner caches and emit ordered commands."""
    if output.exists():
        raise FileExistsError("Choose a fresh --output; existing demo repositories are never reset or deleted.")
    output.mkdir(parents=True)
    original = Commit(
        "Add Parcel preview command and usage",
        "Demo Author",
        "demo@example.invalid",
        "2026-10-01T09:00:00+00:00",
        (("README.md", README), ("parcel.py", SOURCE)),
    )
    renamed = Commit(
        "Rename preview port option and increase worker default",
        "Demo Author",
        "demo@example.invalid",
        "2026-10-02T09:00:00+00:00",
        (
            (
                "parcel.py",
                SOURCE.replace("--port", "--listen")
                .replace("args.port", "args.listen")
                .replace("default=4", "default=8"),
            ),
            ("README.md", README.replace("## Local preview", "## Preview workflow")),
        ),
    )
    history = _build_repo(output, Repository("parcel", (original, renamed)))
    repo = output / "parcel"
    first = run_scan(repo, Config())
    warm = run_scan(repo, Config())
    claim = next(f.claim.claim_id for f in warm.findings if f.is_drift)
    (output / "report.html").write_text(render(warm, "html"), encoding="utf-8")
    (output / "scan.json").write_text(render(warm, "json"), encoding="utf-8")
    plan = plan_rule_fixes(warm)
    comments = []
    for path in sorted(plan.hashes):
        before, after = render_file(repo, plan, path)
        for line, (old, new) in enumerate(
            zip(before.decode().splitlines(), after.decode().splitlines(), strict=True), 1
        ):
            if old != new:
                comments.append(
                    {"path": path, "line": line, "side": "RIGHT", "body": "```suggestion\n" + new + "\n```"}
                )
    (output / "review-preview.json").write_text(
        json.dumps(
            {
                "commit_id": history[-1],
                "event": "COMMENT",
                "body": f"This PR makes {len(comments)} doc statements outdated. "
                "Synthetic integration test; inspect the evidence before applying.",
                "comments": comments,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assert render(first, "json") == render(warm, "json")
    commands = [
        "python parcel.py serve --port 8000",
        "docs-drift scan",
        f"docs-drift explain {claim}",
        "docs-drift fix --patch ../fix.patch",
        "docs-drift watch",
    ]
    result: dict[str, object] = {
        "synthetic": True,
        "head": history[-1],
        "claim": claim,
        "commands": commands,
        "expected_exit_codes": [2, 0, 0, 0, None],
        "findings": len(warm.findings),
        "working_directory": repo.as_posix(),
    }
    (output / "recording.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("SYNTHETIC BACKUP: never describe this project as the personal real-work hook.")
    print("Expected first command exit: 2 (stale flag); scan/explain/fix: 0. Fix is preview only.")
    print(f"Before recording, change directory to {repo.as_posix()}; keep that path off camera.")
    print("\n".join(commands))
    print("For the editor shot, change --port to --listen and the documented worker default from 4 to 8.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".docs-drift/demo-take-01"))
    args = parser.parse_args()
    prepare(args.output.resolve())


if __name__ == "__main__":
    main()
