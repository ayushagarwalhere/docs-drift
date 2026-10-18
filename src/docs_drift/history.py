"""Bounded, text-only Git history queries for change attribution and rename evidence."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docs_drift.config import HistoryConfig
from docs_drift.models import CommitRef, Finding

_META = re.compile(r"^@@@([0-9a-f]{40,64})\x1f([0-9a-f]{4,64})\x1f([^\x1f]*)\x1f([^\x1f]*)\x1f([^\x1f]*)\x1f(.*)$")
_PATH = re.compile(r"^diff --git a/(.*?) b/(.*?)$")
_SOURCE_GLOBS = (
    "*.py",
    "*.js",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.tsx",
    "*.toml",
    "*.json",
    "*.yaml",
    "*.yml",
    "*.ini",
    "*.cfg",
    "Makefile",
    "justfile",
)
_COMMAND_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class HistoryEvidence:
    """A same-hunk textual replacement that can corroborate a rename."""

    old_name: str
    new_name: str
    commit: CommitRef
    file: str
    hunk_excerpt: str


@dataclass(frozen=True)
class HistoryMatch:
    """Attribution plus optional replacement evidence and a non-fatal warning."""

    commit: CommitRef | None
    rename: HistoryEvidence | None = None
    warning: str | None = None


def _run(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _commit(metadata: re.Match[str], show_emails: bool) -> CommitRef:
    sha, short_sha, author, email, date, subject = metadata.groups()
    return CommitRef(
        sha=sha,
        short_sha=short_sha,
        author_name=author or "Unknown author",
        author_email=email if show_emails else None,
        date=datetime.fromisoformat(date),
        summary=subject or "(no subject)",
    )


def _chunks(text: str) -> list[tuple[re.Match[str], str]]:
    result = []
    current: re.Match[str] | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@@"):
            if current is not None:
                result.append((current, "\n".join(buffer)))
            current = _META.match(line)
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        result.append((current, "\n".join(buffer)))
    return result


class GitHistory:
    """Cache shallow-clone checks and document blame while querying only Git text."""

    def __init__(self, repo: Path, config: HistoryConfig | None = None) -> None:
        self.repo = repo.resolve()
        self.config = config or HistoryConfig()
        self._shallow: bool | None = None
        self._blame: dict[tuple[str, int], CommitRef | None] = {}
        self.warnings: list[str] = []

    def shallow(self) -> bool | None:
        if self._shallow is None:
            result = _run(self.repo, ["rev-parse", "--is-shallow-repository"])
            if result is None:
                self._shallow = None
                warning = (
                    "This folder isn't a git repository, so commit history is unavailable; "
                    "findings will not include 'introduced by'."
                )
                if warning not in self.warnings:
                    self.warnings.append(warning)
            else:
                self._shallow = result.stdout.strip().casefold() == "true"
                if self._shallow:
                    self.warnings.append("Git history is shallow and may be truncated; use checkout fetch-depth: 0.")
        return self._shallow

    def search(self, old_name: str, new_names: tuple[str, ...] = ()) -> HistoryMatch:
        """Find the newest commit that removes an old token from source text."""
        if self.shallow() is None:
            return HistoryMatch(None, warning=self.warnings[0])
        if self.config.max_commits == 0:
            return HistoryMatch(None)
        result = _run(
            self.repo,
            [
                "log",
                "--no-ext-diff",
                f"--max-count={self.config.max_commits}",
                "-p",
                "--format=@@@%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s",
                *(["-G", f"[\"']{re.escape(old_name)}[\"']"] if old_name.startswith("/") else ["-S", old_name]),
                "--",
                *_SOURCE_GLOBS,
            ],
        )
        if result is None:
            warning = "Git history lookup failed or timed out; no introducing commit was attached."
            self.warnings.append(warning)
            return HistoryMatch(None, warning=warning)
        old_pattern = re.compile(rf"(?<![\w]){re.escape(old_name)}(?![\w])")
        for meta, patch in _chunks(result.stdout):
            commit = _commit(meta, self.config.show_emails)
            file_name = ""
            hunk: list[str] = []
            removed = False
            for line in [*patch.splitlines(), "@@ END"]:
                path_match = _PATH.match(line)
                if path_match:
                    file_name = path_match.group(2)
                if line.startswith("@@"):
                    if removed:
                        evidence = self._rename_evidence(old_name, new_names, commit, file_name, hunk)
                        if evidence:
                            return HistoryMatch(commit, evidence)
                        return HistoryMatch(commit)
                    hunk = []
                    removed = False
                    continue
                hunk.append(line)
                if line.startswith("-") and not line.startswith("---") and old_pattern.search(line):
                    removed = True
        return HistoryMatch(None)

    @staticmethod
    def _rename_evidence(
        old_name: str, new_names: tuple[str, ...], commit: CommitRef, file_name: str, lines: list[str]
    ) -> HistoryEvidence | None:
        added = [line for line in lines if line.startswith("+") and not line.startswith("+++")]
        for candidate in new_names:
            candidate_pattern = re.compile(rf"(?<![\w]){re.escape(candidate)}(?![\w])")
            if any(candidate_pattern.search(line) for line in added):
                excerpt = "\n".join([line for line in lines if line.startswith(("-", "+"))][:8])
                return HistoryEvidence(old_name, candidate, commit, file_name, excerpt)
        return None

    def introduced_for(self, finding: Finding) -> HistoryMatch:
        return self.search(finding.claim.value)

    def last_touched(self, doc_path: str, line: int) -> CommitRef | None:
        key = doc_path, line
        if key in self._blame:
            return self._blame[key]
        if self.shallow() is None:
            return None
        result = _run(self.repo, ["blame", "--line-porcelain", f"-L{line},{line}", "--", doc_path])
        if result is None:
            self._blame[key] = None
            return None
        lines = result.stdout.splitlines()
        commit_match = re.match(r"^([0-9a-f]{40,64})\s+\d+\s+\d+", lines[0]) if lines else None
        values: dict[str, str] = {}
        for item in lines[1:]:
            if item.startswith("\t"):
                break
            name, _, value = item.partition(" ")
            values[name] = value
        if not commit_match:
            self._blame[key] = None
            return None
        sha = commit_match.group(1)
        author = values.get("author", "Unknown author")
        email = values.get("author-mail", "").strip("<>")
        timestamp = values.get("author-time", "0")
        date = datetime.fromtimestamp(int(timestamp), tz=datetime.now().astimezone().tzinfo)
        commit = CommitRef(
            sha=sha,
            short_sha=sha[:7],
            author_name=author,
            author_email=email if self.config.show_emails else None,
            date=date,
            summary=values.get("summary", "(no subject)"),
        )
        self._blame[key] = commit
        return commit


__all__ = ["GitHistory", "HistoryEvidence", "HistoryMatch"]
