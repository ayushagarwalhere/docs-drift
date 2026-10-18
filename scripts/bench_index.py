"""Benchmark cold and warm indexing of two public repos without executing their code."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docs_drift.config import Config  # noqa: E402
from docs_drift.index import build_index, clear_cache  # noqa: E402

REPOSITORIES = {
    "flask": "https://github.com/pallets/flask.git",
    "hono": "https://github.com/honojs/hono.git",
}


def _timed_index(repo: Path, *, cold: bool) -> tuple[float, int]:
    if cold:
        clear_cache(repo)
    started = time.perf_counter()
    result = build_index(repo, Config(), no_cache=False)
    return time.perf_counter() - started, len(result.symbols)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", choices=tuple(REPOSITORIES), action="append")
    args = parser.parse_args(argv)
    selected = args.repo or list(REPOSITORIES)
    with tempfile.TemporaryDirectory(prefix="docs-drift-bench-") as temporary:
        for name in selected:
            target = Path(temporary) / name
            clone = ["git", "clone", "--depth", "1", REPOSITORIES[name], str(target)]
            print("Command: " + " ".join(clone))
            subprocess.run(clone, check=True, capture_output=True, text=True)
            cold_seconds, cold_count = _timed_index(target, cold=True)
            warm_seconds, warm_count = _timed_index(target, cold=False)
            print(
                f"{name}: cold={cold_seconds:.3f}s/{cold_count} symbols; warm={warm_seconds:.3f}s/{warm_count} symbols"
            )
            print(f"Index command: docs-drift --repo {target} index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
