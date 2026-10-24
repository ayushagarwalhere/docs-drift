"""Bounded Git archives for historical static scans; target code is never executed."""

import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile

from docs_drift.config import Config
from docs_drift.git_safety import disabled_filters
from docs_drift.history import _run
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan

MAX_ARCHIVE = 64_000_000
MAX_EXPANDED = 128_000_000
ARCHIVE_TIMEOUT = 15


def resolve_revision(repo: Path, revision: str) -> str:
    if revision.startswith("-") or any(c.isspace() for c in revision) or not revision:
        raise ValueError("Use a single Git revision, not command options or whitespace.")
    resolved = _run(repo, ["rev-parse", "--verify", revision + "^{commit}"])
    if not resolved:
        raise ValueError("Git revision unavailable; fetch the base/history before comparing.")
    return resolved.stdout.strip()


def materialize_snapshot(repo: Path, revision: str, root: Path) -> str:
    sha = resolve_revision(repo, revision)
    with tempfile.TemporaryDirectory(prefix="docs-drift-snapshot-") as directory:
        parent = Path(directory)
        archive = parent / "source.zip"
        root.mkdir(parents=True, exist_ok=True)
        # Compare canonical paths on both sides (Windows TEMP may use an 8.3 alias).
        root = root.resolve()
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1"
        )
        with archive.open("wb") as output:
            process = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(repo),
                    *disabled_filters(repo),
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "archive",
                    "--format=zip",
                    sha,
                ],
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            try:
                deadline = time.monotonic() + ARCHIVE_TIMEOUT
                while process.poll() is None:
                    if time.monotonic() > deadline or archive.stat().st_size > MAX_ARCHIVE:
                        raise ValueError("Historical archive exceeded its size/time budget; comparison is incomplete.")
                    time.sleep(0.02)
                if process.returncode or archive.stat().st_size > MAX_ARCHIVE:
                    raise ValueError("Cannot acquire historical source archive within the comparison budget.")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        with ZipFile(archive) as snapshot:
            if sum(info.file_size for info in snapshot.infolist()) > MAX_EXPANDED:
                raise ValueError("Historical archive expands beyond the static scan budget.")
            for info in snapshot.infolist():
                target = root / info.filename
                if (
                    not target.resolve().is_relative_to(root)
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or any(part.rstrip(". ").casefold() == ".git" for part in Path(info.filename).parts)
                    or ":" in info.filename
                ):
                    raise ValueError("Historical archive contains an unsafe path or symbolic link.")
            snapshot.extractall(root)
    return sha


def scan_snapshot(repo: Path, revision: str, config: Config) -> ScanResult:
    with tempfile.TemporaryDirectory(prefix="docs-drift-scan-") as directory:
        root = Path(directory) / "source"
        sha = materialize_snapshot(repo, revision, root)
        token = CACHE_DISABLED.set(True)
        try:
            result = run_scan(root, config, history=False, no_cache=True)
        finally:
            CACHE_DISABLED.reset(token)
        return result.model_copy(update={"head_sha": sha, "repo_path": repo})
