"""Bake the static indexes that let /write check text against the bundled web examples.

Each index is produced by the production worker path (``execute(..., keep_index=...)``), so it
is scrubbed and size-capped exactly like a live scan's. Fixture repositories are rebuilt
deterministically; public examples are fetched at their pinned commit (network required).

    uv run python scripts/build_write_indexes.py            # everything
    uv run python scripts/build_write_indexes.py --offline  # fixtures only
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from docs_drift.web.settings import Settings
from docs_drift.web.worker import execute
from docs_drift.web.write import WriteIndex
from scripts.build_judge_demo import NAME as JUDGE_NAME
from scripts.build_judge_demo import build_demo
from tests.fixtures.build_fixtures import build_fixtures

EXAMPLES = Path(__file__).resolve().parents[1] / "src/docs_drift/web/examples"
FIXTURES = ("py-cli", "fastapi-svc", "node-tool", "clean-repo")


def _git(repo: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=env,
        timeout=300,
    )


def fetch_pinned(url: str, sha: str, target: Path) -> Path:
    """Fetch exactly one public commit without running repository hooks or filters."""
    target.mkdir(parents=True)
    _git(target, "init", "-q")
    _git(target, "remote", "add", "origin", url)
    _git(target, "fetch", "-q", "--depth", "1", "origin", sha)
    _git(target, "checkout", "-q", "FETCH_HEAD")
    return target


def bake(repo: Path, file: str, output: Path) -> None:
    target = output / file
    execute(repo, Settings(), lambda *_: None, keep_index=target)
    index = WriteIndex.model_validate_json(target.read_text(encoding="utf-8"))
    state = index.unavailable or f"{len(index.symbols)} symbols, {len(index.source_paths)} paths"
    print(f"{file}: {state} ({target.stat().st_size:,} bytes, {index.head_sha[:8]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="Skip public examples that need a network fetch.")
    args = parser.parse_args()
    output = EXAMPLES / "indexes"
    output.mkdir(exist_ok=True)
    manifest = json.loads((EXAMPLES / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="docs-drift-write-") as temporary:
        root = Path(temporary)
        build_fixtures(root / "repos")
        for name in FIXTURES:
            bake(root / "repos" / name, f"{name}.json", output)
        build_demo(root / "judge")
        bake(root / "judge" / JUDGE_NAME, f"{JUDGE_NAME}.json", output)
        if args.offline:
            return
        fetched: dict[tuple[str, str], Path] = {}
        for item in manifest:
            if item.get("kind") != "public" or not item.get("sha"):
                continue
            key = (item["repo_url"].casefold(), item["sha"])
            if key not in fetched:
                fetched[key] = fetch_pinned(item["repo_url"], item["sha"], root / "public" / str(len(fetched)))
            bake(fetched[key], item["file"], output)


if __name__ == "__main__":
    main()
