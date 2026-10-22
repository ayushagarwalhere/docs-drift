"""Bounded, text-only Git history queries for change attribution and rename evidence."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from docs_drift.config import HistoryConfig
from docs_drift.history_cache import QueryCache
from docs_drift.limits import MAX_GIT_OUTPUT, MAX_LINE_CHARS
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


def _rename_shape(line: str, name: re.Pattern[str]) -> str:
    normalized = name.sub("__DOCS_DRIFT_RENAMED__", line[1:].strip())
    if len(normalized) > MAX_LINE_CHARS:
        return normalized
    try:
        statements = ast.parse(normalized).body
    except (SyntaxError, ValueError, RecursionError):
        return normalized
    if len(statements) != 1:
        return normalized
    statement = statements[0]
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and targets[0].id == "__DOCS_DRIFT_RENAMED__"
            and isinstance(statement.value, ast.Constant)
        ):
            # A field can be renamed and have its literal default changed in
            # the same edit. Preserve target/type structure, not the value.
            statement.value = ast.Constant(value="__DOCS_DRIFT_DEFAULT__")
            return ast.dump(statement, include_attributes=False)
    return normalized


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
    partial = args[:1] == ["__bounded_history__"]
    if partial:
        args = args[1:]
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    try:
        # File-backed output avoids waiting forever on a pipe inherited by a lazy
        # partial-clone fetch after the parent git process times out.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args],
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            try:
                deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
                while process.poll() is None:
                    if time.monotonic() >= deadline or output.seek(0, 2) > MAX_GIT_OUTPUT:
                        raise subprocess.TimeoutExpired(process.args, _COMMAND_TIMEOUT_SECONDS)
                    time.sleep(0.02)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                if partial:
                    output.seek(0)
                    prefix = output.read(MAX_GIT_OUTPUT).decode("utf-8", "replace")
                    prefix = prefix.rpartition("\n@@@")[0]
                    return subprocess.CompletedProcess(process.args, 0, "@@TRUNCATED_HISTORY@@\n" + prefix, "")
                return None
            if process.returncode:
                return None
            output.seek(0)
            data = output.read(8_000_001)
            if len(data) > MAX_GIT_OUTPUT:
                return None
            return subprocess.CompletedProcess(process.args, process.returncode, data.decode("utf-8", "replace"), "")
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
        self._revision_range: str | None = None
        self.warnings: list[str] = []
        self._queries = QueryCache(self.repo, _run)
        self._prepared: list[tuple[re.Match[str], str]] | None = None

    def prepare(self) -> None:
        """Read one bounded newest-first patch stream, avoiding a Git process per claim."""
        if self.shallow() is None or self.config.max_commits == 0:
            self._prepared = []
            return
        listing = self._queries.query(["rev-list", f"--max-count={self.config.max_commits + 1}", "HEAD"])
        revisions = listing.stdout.splitlines() if listing else []
        self._revision_range = f"{revisions[-1]}..HEAD" if len(revisions) > self.config.max_commits else "HEAD"
        result = self._queries.query(
            [
                "__bounded_history__",
                "log",
                "--no-ext-diff",
                "--no-textconv",
                f"--max-count={self.config.max_commits}",
                "-p",
                "--format=@@@%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s",
                self._revision_range,
                "--",
                *_SOURCE_GLOBS,
            ]
        )
        self._prepared = _chunks(result.stdout) if result else []
        if result is None or result.stdout.startswith("@@TRUNCATED_HISTORY@@"):
            self.warnings.append(
                "History patch stream exceeded the 8 MB/12 second budget; older attribution is unavailable."
            )

    def shallow(self) -> bool | None:
        if self._shallow is None:
            result = self._queries.query(["rev-parse", "--is-shallow-repository"])
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

    def search(self, old_name: str, new_names: tuple[str, ...] = ()) -> HistoryMatch:  # noqa: PLR0912 - evidence guards.
        """Find the newest commit that removes an old token from source text."""
        if self.shallow() is None:
            return HistoryMatch(None, warning=self.warnings[0])
        if self.config.max_commits == 0:
            return HistoryMatch(None)
        if self._revision_range is None:
            listing = self._queries.query(["rev-list", f"--max-count={self.config.max_commits + 1}", "HEAD"])
            revisions = listing.stdout.splitlines() if listing else []
            self._revision_range = f"{revisions[-1]}..HEAD" if len(revisions) > self.config.max_commits else "HEAD"
        result = (
            self._queries.query(
                [
                    "log",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"--max-count={self.config.max_commits}",
                    "-p",
                    "--format=@@@%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s",
                    *(["-G", f"[\"']{re.escape(old_name)}[\"']"] if old_name.startswith("/") else [f"-S{old_name}"]),
                    self._revision_range,
                    "--",
                    *_SOURCE_GLOBS,
                ],
            )
            if self._prepared is None
            else None
        )
        if result is None and self._prepared is None:
            warning = "Git history lookup failed or timed out; no introducing commit was attached."
            self.warnings.append(warning)
            return HistoryMatch(None, warning=warning)
        old_pattern = re.compile(rf"(?<![\w]){re.escape(old_name)}(?![\w])")
        chunks = self._prepared if self._prepared is not None else _chunks(result.stdout if result else "")
        for meta, patch in chunks:
            if old_name not in patch:
                continue
            if not old_name.startswith("/"):
                removed_occurrences = sum(
                    line[1:].count(old_name)
                    for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                )
                added_occurrences = sum(
                    line[1:].count(old_name)
                    for line in patch.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                )
                if removed_occurrences == added_occurrences:
                    continue
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
        old_pattern = re.compile(rf"(?<![\w]){re.escape(old_name)}(?![\w])")
        removed_shapes = {
            _rename_shape(line, old_pattern)
            for line in lines
            if line.startswith("-") and not line.startswith("---") and old_pattern.search(line)
        }
        added_text = "\n".join(added)
        for candidate in new_names:
            if candidate == old_name or candidate not in added_text:
                continue
            candidate_pattern = re.compile(rf"(?<![\w]){re.escape(candidate)}(?![\w])")
            # Sharing a large diff hunk does not establish a replacement. Require
            # matching text or a matching assignment with a changed literal
            # default; otherwise retain only removal attribution.
            if any(
                candidate_pattern.search(line) and _rename_shape(line, candidate_pattern) in removed_shapes
                for line in added
            ):
                excerpt = "\n".join([line for line in lines if line.startswith(("-", "+"))][:8])
                return HistoryEvidence(old_name, candidate, commit, file_name, excerpt)
        return None

    def introduced_for(self, finding: Finding) -> HistoryMatch:
        return self.search(finding.claim.value)

    def default_change(self, finding: Finding) -> tuple[CommitRef | None, str]:
        """Find a declaration changing the documented default to its current value."""
        symbol = finding.matched_symbol
        expected = finding.claim.expected_default
        if symbol is None or expected is None or symbol.default is None or self.shallow() is None:
            return None, ""
        result = (
            self._queries.query(
                [
                    "log",
                    "--no-ext-diff",
                    f"--max-count={self.config.max_commits}",
                    "-p",
                    "--format=@@@%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s",
                    "-G",
                    re.escape(symbol.name),
                    "--",
                    symbol.file,
                ],
            )
            if self._prepared is None
            else None
        )
        if result is None and self._prepared is None:
            return None, ""
        old = re.compile(rf"(?<![\w]){re.escape(expected.strip(chr(96) + chr(34) + chr(39)))}(?![\w])")
        new = re.compile(rf"(?<![\w]){re.escape(symbol.default.strip(chr(96) + chr(34) + chr(39)))}(?![\w])")
        chunks = self._prepared if self._prepared is not None else _chunks(result.stdout if result else "")
        for metadata, patch in chunks:
            selected_patch = patch
            if self._prepared is not None:
                selected_patch = "\n".join(
                    block
                    for block in re.split(r"(?m)(?=^diff --git )", patch)
                    if re.search(rf"(?m)^\+\+\+ b/{re.escape(symbol.file)}$", block)
                )
            if symbol.name not in selected_patch:
                continue
            # Check each hunk independently, avoiding unrelated defaults in the same commit.
            for hunk in re.split(r"(?m)^@@.*@@.*$", selected_patch):
                removed = [line for line in hunk.splitlines() if line.startswith("-") and not line.startswith("---")]
                added = [line for line in hunk.splitlines() if line.startswith("+") and not line.startswith("+++")]
                if any(symbol.name in line and old.search(line) for line in removed) and any(
                    symbol.name in line and new.search(line) for line in added
                ):
                    return _commit(metadata, self.config.show_emails), "\n".join([*removed, *added])
        return None, ""

    def last_touched(self, doc_path: str, line: int) -> CommitRef | None:
        """Blame the committed snapshot, independent of checkout newline conversion or uncommitted edits."""
        key = doc_path, line
        if key in self._blame:
            return self._blame[key]
        if self.shallow() is None:
            return None
        result = self._queries.query(["blame", "--line-porcelain", f"-L{line},{line}", "HEAD", "--", doc_path])
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
        date = datetime.fromtimestamp(int(timestamp), tz=UTC)
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
