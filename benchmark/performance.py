"""Measure cache-cold and warm static scans, retaining every unrounded observation."""

import argparse
import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path

from benchmark.run import ROOT, source_digest
from docs_drift.analysis import impact
from docs_drift.config import Config, HistoryConfig
from docs_drift.report import render
from docs_drift.scanning import run_scan
from docs_drift.web.cleanup import remove_workspace


def measure(repo: Path, config: Config, *, cold: bool = False, base: str | None = None) -> dict[str, object]:
    cache = repo / ".docs-drift/cache"
    if cold and cache.exists():
        remove_workspace(cache.parent, cache)
    start = time.perf_counter()
    result = impact(repo, config, base) if base else run_scan(repo, config)
    elapsed = time.perf_counter() - start
    encoded = render(result, "json").encode()
    return {
        "seconds": elapsed,
        "stages": result.timings,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "head_sha": result.head_sha,
        "claims": len(result.claims),
        "symbols": len(result.symbols),
        "warnings": result.warnings,
        "cache_cold": cold,
        "base": base,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=["small", "large"], default="small")
    parser.add_argument("--large-repo", choices=["fastapi", "next"], default="next")
    args = parser.parse_args()
    rows: dict[str, object] = {}
    config = Config(history=HistoryConfig(max_commits=2000))
    if args.part == "small":
        for name in ("py-cli", "node-tool", "fastapi-svc", "clean-repo"):
            repo = ROOT / "tests/fixtures/repos" / name
            measure(repo, config)
            rows["fixture:" + name] = [measure(repo, config) for _ in range(3)]
        flask = ROOT / ".docs-drift/repositories/pallets--flask/repository"
        rows["flask:cold"] = measure(flask, config, cold=True)
        rows["flask:warm"] = [measure(flask, config) for _ in range(3)]
        rows["flask:impact"] = [measure(flask, config, base="HEAD~1") for _ in range(3)]
    else:
        name = "fastapi--fastapi" if args.large_repo == "fastapi" else "vercel--next.js"
        repo = ROOT / ".docs-drift/repositories" / name / "repository"
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
        if args.large_repo == "next":
            config.exclude.append("examples/**")
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True).stdout
        rows["tracked_files"] = len([p for p in tracked.split(b"\0") if p])
        rows["large:cold"] = measure(repo, config, cold=True)
    suffix = "small" if args.part == "small" else "large-" + args.large_repo
    output = ROOT / ".docs-drift/performance" / (suffix + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "source_sha256": source_digest(),
                "method": "run_scan wall time, includes history; excludes imports/rendering; "
                "cold clears application caches, not the OS page cache; warm = three subsequent scans; "
                "all observations retained, targets use the slowest warm observation",
                "rows": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(output.as_posix(), flush=True)


if __name__ == "__main__":
    main()
