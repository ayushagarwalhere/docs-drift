"""Measure one local pinned scan; save runtime separately from canonical snapshot JSON."""

import argparse
import cProfile
import json
import time
import tracemalloc
from pathlib import Path

from benchmark.run import source_digest
from docs_drift.config import Config, HistoryConfig
from docs_drift.report import render
from docs_drift.scanning import run_scan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--docs-only", action="store_true")
    parser.add_argument("--exclude-examples", action="store_true")
    args = parser.parse_args()
    config = Config(history=HistoryConfig(max_commits=2000))
    if args.docs_only:
        config.docs = ["docs/**/*.md", "docs/**/*.mdx", "docs/**/*.rst"]
        config.exclude = [
            ".git/**",
            ".docs-drift/**",
            ".venv/**",
            "node_modules/**",
            "packages/**",
            "tests/**",
            "test/**",
        ]
    if args.exclude_examples:
        config.exclude.append("examples/**")
    start = time.perf_counter()
    scanner_source = source_digest()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def stage(name: str, counts: dict[str, int]) -> None:
        print(json.dumps({"stage": name, "seconds": time.perf_counter() - start, **counts}), flush=True)

    if args.memory:
        tracemalloc.start()
    profiler = cProfile.Profile()
    if args.profile:
        profiler.enable()
    result = run_scan(args.repo, config, history=not args.no_history, stage_callback=stage)
    elapsed = time.perf_counter() - start
    if args.profile:
        profiler.disable()
        profiler.dump_stats(str(args.out) + ".prof")
    peak = tracemalloc.get_traced_memory()[1] if args.memory else None
    if args.memory:
        tracemalloc.stop()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(result, "json"), encoding="utf-8")
    measurement = {
        "repo": args.repo.as_posix(),
        "source_sha256": scanner_source,
        "source_unchanged_during_scan": scanner_source == source_digest(),
        "head_sha": result.head_sha,
        "seconds": elapsed,
        "stages": result.timings,
        "peak_tracemalloc_bytes": peak,
        "claims": len(result.claims),
        "symbols": len(result.symbols),
        "findings": result.summary(),
        "warnings": result.warnings,
        "history_enabled": not args.no_history,
        "docs_only": args.docs_only,
    }
    args.out.with_suffix(".measurement.json").write_text(json.dumps(measurement, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(measurement), flush=True)


if __name__ == "__main__":
    main()
