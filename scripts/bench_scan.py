"""Measure full cold/warm scans of a fresh 20-package, 2,000-source-file workspace."""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from docs_drift.config import Config
from docs_drift.scanning import run_scan
from tests.fixtures.build_fixtures import build_monorepo


def main() -> None:
    with TemporaryDirectory(prefix="docs-drift-bench-", dir=Path.cwd()) as directory:
        repo = Path(directory) / "monorepo"
        build_monorepo(repo)
        values: dict[str, object] = {}
        for label in ("cold", "warm"):
            start = time.perf_counter()
            result = run_scan(repo, Config())
            values[label] = {"seconds": round(time.perf_counter() - start, 3), "stages": result.timings}
        values["source_files"] = 2000
        values["packages"] = 20
        text = json.dumps(values, indent=2) + "\n"
        Path("examples/monorepo-benchmark.json").write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
