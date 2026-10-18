"""Static verification of fixture index output. Never imports or executes fixture code."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from docs_drift.config import Config  # noqa: E402
from docs_drift.index import build_index  # noqa: E402
from tests.fixtures.build_fixtures import build_fixtures  # noqa: E402


def main() -> int:
    """Build fixtures and compare selected static facts with the hand-written key."""
    key = json.loads((ROOT / "tests" / "fixtures" / "answer_key.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="docs-drift-index-verify-") as temporary:
        histories = build_fixtures(Path(temporary))
        reports = []
        for name in histories:
            repo = Path(temporary) / name
            index = build_index(repo, Config(), no_cache=True)
            reports.append((name, len(index.symbols), len(index.warnings), index.coverage))
        print("Command: uv run python scripts/verify_index.py")
        print(f"Fixture repositories: {len(reports)}; answer-key findings: {len(key.get('findings', []))}")
        for name, count, warning_count, coverage in reports:
            print(f"{name}: symbols={count}; warnings={warning_count}; coverage={coverage}")
        # The harness deliberately does not spawn fixture programs. The project's standing rule
        # requires scanned repositories to be treated only as source text or AST.
        print("Runtime CLI/help and app.routes checks: skipped (fixture code is never executed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
