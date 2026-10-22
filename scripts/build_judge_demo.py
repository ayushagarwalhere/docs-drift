"""Build one synthetic example through the existing static worker, without network access."""

import argparse
import json
from pathlib import Path

from docs_drift.models import Evidence, ScanResult
from docs_drift.web.settings import Settings
from docs_drift.web.worker import execute
from tests.fixtures.build_fixtures import Repository, _build_repo, _commit

NAME = "judge-fixture"
NOTICE = (
    "SYNTHETIC FIXTURE / precomputed static scan. One intentionally stale claim; "
    "not a real-world accuracy result. Review the proposed replacement before editing. "
    "Rebuild and inspect the two commits with scripts/build_judge_demo.py."
)
README = "# Synthetic CLI fixture\n\nUse `--port` to select the listening port.\n"
SOURCE = 'import argparse\n\nparser = argparse.ArgumentParser()\nparser.add_argument("--port", type=int)\n'


def build_demo(output: Path) -> ScanResult:
    """Create a fresh, reproducible two-commit repo; never overwrite a checkout."""
    output.mkdir(parents=True, exist_ok=True)
    _build_repo(
        output,
        Repository(
            NAME,
            (
                _commit(1, "Document the original port option", ("README.md", README), ("cli.py", SOURCE)),
                _commit(
                    2, "Rename port to listen without updating README", ("cli.py", SOURCE.replace("--port", "--listen"))
                ),
            ),
        ),
    )
    result = execute(output / NAME, Settings(), lambda *args: None)
    result.repo_path = Path(NAME)
    result.remote_url = None  # This fixture has no upstream repository or GitHub commit URL.
    result.warnings = [NOTICE]
    for finding in result.findings:
        finding.evidence.insert(1, Evidence(file="cli.py", line=1, text=SOURCE.replace("--port", "--listen")))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh parent directory for the fixture")
    parser.add_argument("--bake", action="store_true", help="Update the bundled example and its manifest")
    args = parser.parse_args()
    result = build_demo(args.output)
    report = args.output / "scan.json"
    report.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if args.bake:
        examples = Path(__file__).resolve().parents[1] / "src/docs_drift/web/examples"
        (examples / f"{NAME}.json").write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
        path = examples / "manifest.json"
        manifest = [item for item in json.loads(path.read_text(encoding="utf-8")) if item["scan_id"] != "example-judge"]
        manifest.insert(
            0,
            {
                "scan_id": "example-judge",
                "name": "One stale claim (synthetic fixture)",
                "repo_url": "synthetic:judge-fixture",
                "file": f"{NAME}.json",
                "kind": "fixture",
                "verified": False,
                "note": NOTICE,
            },
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Synthetic fixture: {args.output / NAME}\nStatic report: {report}")


if __name__ == "__main__":
    main()
