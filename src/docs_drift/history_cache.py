"""Bounded snapshot-keyed Git text cache; public workers disable repository caches."""

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

MAX_ENTRIES = 128
MAX_CACHE_BYTES = 128_000_000
MAX_ENTRY_BYTES = 8_000_000
MAX_MEMORY_ENTRY = 100_000
CACHE_DISABLED: ContextVar[bool] = ContextVar("history_cache_disabled", default=False)


class QueryCache:
    def __init__(self, repo: Path, run: Callable[[Path, list[str]], subprocess.CompletedProcess[str] | None]):
        self.repo, self.run = repo, run
        self.identity: str | None = None
        self.memory: dict[str, subprocess.CompletedProcess[str] | None] = {}
        self.directory = repo / ".docs-drift/cache/history"
        self.enabled = (
            not CACHE_DISABLED.get()
            and os.getenv("DOCS_DRIFT_NO_HISTORY_CACHE") != "1"
            and self.directory.resolve().is_relative_to(repo)
        )

    def query(self, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        key = json.dumps(args)
        if key in self.memory:
            return self.memory[key]
        if self.identity is None:
            head = self.run(self.repo, ["rev-parse", "HEAD"])
            shallow = self.repo / ".git/shallow"
            self.identity = (head.stdout.strip() if head else "unversioned") + (
                shallow.read_text() if shallow.is_file() and not shallow.is_symlink() else "full"
            )
        digest = hashlib.sha256(("history-v2" + self.identity + key).encode()).hexdigest()
        path = self.directory / f"{digest}.txt"
        result = None
        if self.enabled and path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ENTRY_BYTES:
            result = subprocess.CompletedProcess(args, 0, path.read_text(encoding="utf-8"), "")
        else:
            result = self.run(self.repo, args)
            if result and self.enabled:
                try:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    path.write_text(result.stdout, encoding="utf-8")
                    used = 0
                    for index, entry in enumerate(
                        sorted(self.directory.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
                    ):
                        used += entry.stat().st_size
                        if index >= MAX_ENTRIES or used > MAX_CACHE_BYTES:
                            entry.unlink(missing_ok=True)
                except OSError:
                    pass  # Read-only checkouts still scan correctly.
        if len(self.memory) < MAX_ENTRIES and (result is None or len(result.stdout) < MAX_MEMORY_ENTRY):
            self.memory[key] = result
        return result
