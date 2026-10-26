"""Contracts and query helpers for static source indexers."""

import fnmatch
import os
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from docs_drift.limits import (
    CONTROL_START,
    DELETE_CHARACTER,
    MAX_FILE_BYTES,
    MAX_LINE_CHARS,
    MAX_NESTING,
    MAX_PATH_CHARS,
)
from docs_drift.models import ClaimKind, Symbol, SymbolKind
from docs_drift.support import COVERAGE_MAP


class Indexer(Protocol):
    """One read-only language/framework indexer."""

    name: str
    languages: frozenset[str]

    def applies_to(self, repo: Path) -> bool: ...

    def index(self, repo: Path, files: Sequence[Path]) -> list[Symbol]: ...


@dataclass
class CodeIndex:
    """Stable, sorted symbols plus exact normalized lookup indexes and coverage metadata."""

    symbols: list[Symbol] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_by_language: dict[str, int] = field(default_factory=dict)
    source_paths: list[str] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    by_kind: dict[SymbolKind, list[Symbol]] = field(default_factory=dict)
    by_name: dict[tuple[SymbolKind, str], list[Symbol]] = field(default_factory=dict)
    by_alias: dict[tuple[SymbolKind, str], list[Symbol]] = field(default_factory=dict)
    by_file: dict[str, list[Symbol]] = field(default_factory=dict)
    path_lookup: set[str] = field(default_factory=set, init=False)
    folded_paths: dict[str, str] = field(default_factory=dict, init=False)
    shared_path_index: tuple[set[str], dict[str, str]] | None = field(default=None, repr=False)
    match_cache: dict[tuple[ClaimKind, str, str | None], tuple[list[Symbol], bool]] = field(
        default_factory=dict, init=False
    )
    rename_cache: dict[tuple[ClaimKind, str], list[tuple[int, Symbol]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.symbols.sort(key=lambda item: (item.file, item.line, item.kind.value, item.qualified_name))
        kinds: dict[SymbolKind, list[Symbol]] = defaultdict(list)
        names: dict[tuple[SymbolKind, str], list[Symbol]] = defaultdict(list)
        aliases: dict[tuple[SymbolKind, str], list[Symbol]] = defaultdict(list)
        files: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in self.symbols:
            kinds[symbol.kind].append(symbol)
            for name in {symbol.name, symbol.qualified_name}:
                names[(symbol.kind, normalize_name(name))].append(symbol)
            for alias in symbol.aliases:
                aliases[(symbol.kind, normalize_name(alias))].append(symbol)
            files[symbol.file].append(symbol)
        self.by_kind = dict(kinds)
        self.by_name = dict(names)
        self.by_alias = dict(aliases)
        self.by_file = dict(files)
        if self.shared_path_index is not None:
            self.path_lookup, self.folded_paths = self.shared_path_index
            return
        paths = set(self.source_paths) | set(files)
        directories = {
            parent.as_posix() for value in paths for parent in PurePosixPath(value).parents if parent.as_posix() != "."
        }
        self.path_lookup = paths | directories
        self.folded_paths = {path.casefold(): path for path in sorted(self.path_lookup)}

    def candidates(self, kind: ClaimKind | SymbolKind | str, name: str) -> list[Symbol]:
        """Return exact matches only; fuzzy suggestions belong in diagnostics, never verdicts."""
        value = kind.value if isinstance(kind, (ClaimKind, SymbolKind)) else kind
        try:
            symbol_kind = SymbolKind(value)
        except ValueError:
            return []
        normalized = normalize_name(name)
        return list(
            dict.fromkeys(
                (*self.by_name.get((symbol_kind, normalized), []), *self.by_alias.get((symbol_kind, normalized), []))
            )
        )


def normalize_name(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum() or char in "-_./:{}")


def source_files(  # noqa: PLR0912, PLR0915 - bounded file validation before parsing.
    repo: Path, exclude: Sequence[str], *, warnings_out: list[str] | None = None
) -> list[Path]:
    """List tracked, non-generated text candidates without opening or executing them."""
    root = repo.resolve()
    if (root / ".git").exists():
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        command = [
            "git",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "ls-files",
            "-z",
        ]
        try:
            raw = subprocess.run(command, capture_output=True, check=True, env=env, timeout=5).stdout
            paths = [Path(os.fsdecode(item)) for item in raw.split(b"\0") if item]
        except (OSError, subprocess.SubprocessError) as exc:
            # Losing Git's tracked-file boundary must not turn a checkout scan
            # into an unbounded traversal of ignored build/cache/test trees.
            raise OSError(
                "Git could not list tracked source files. Check Git availability, repository ownership and "
                "permissions, then retry. The scan stopped instead of indexing ignored or generated files."
            ) from exc
    else:
        paths = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]
    skip = {
        ".git",
        ".venv",
        ".docs-drift",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".next",
        "coverage",
        "__pycache__",
    }
    result = []
    for relative in paths:
        posix = relative.as_posix()
        parts = set(relative.parts)
        if parts & skip or any(fnmatch.fnmatch(posix, pattern) for pattern in exclude):
            continue
        path = root / relative
        if len(posix) > MAX_PATH_CHARS or any(ord(c) < CONTROL_START or ord(c) == DELETE_CHARACTER for c in posix):
            if warnings_out is not None:
                warnings_out.append(f"Source skipped: unsupported or overlong path: {posix!r}")
            continue
        private_env = path.name == ".env" or (
            path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample", ".env.template"}
        )
        try:
            allowed = (
                path.is_file()
                and not path.is_symlink()
                and path.resolve().is_relative_to(root)
                and not private_env
                and path.stat().st_size <= MAX_FILE_BYTES
                and len(posix) <= MAX_PATH_CHARS
                and not any(ord(c) < CONTROL_START or ord(c) == DELETE_CHARACTER for c in posix)
            )
        except OSError:
            if warnings_out is not None:
                warnings_out.append(f"Source skipped: unreadable file: {posix}")
            continue
        if not allowed and warnings_out is not None and language_for(path) is not None and not private_env:
            warnings_out.append(f"Source skipped: non-regular, external, missing or over 1 MB: {posix}")
        if allowed:
            if path.suffix in {".json", ".yaml", ".yml", ".toml"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    if warnings_out is not None:
                        warnings_out.append(f"Source skipped: unreadable file: {posix}")
                    continue
                depth = 0
                unsafe = False
                for char in text:
                    depth += (char in "[{") - (char in "]}")
                    if depth > MAX_NESTING:
                        unsafe = True
                        break
                if unsafe or any(len(line) > MAX_LINE_CHARS for line in text.splitlines()):
                    if warnings_out is not None:
                        warnings_out.append(f"Source skipped: configuration nesting/line parser limit: {posix}")
                    continue
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


# Exact file names the indexers read; keep `.pre-commit-hooks.yaml` `files` in step (a test enforces it).
NAMED_LANGUAGES = {
    "Makefile": "make",
    "GNUmakefile": "make",
    "justfile": "just",
    "Taskfile.yml": "yaml",
    "tox.ini": "ini",
    "setup.cfg": "ini",
    "Dockerfile": "dockerfile",
    ".env.example": "dotenv",
    ".env.sample": "dotenv",
    ".env.template": "dotenv",
}
# Case-insensitive suffixes the indexers read.
SUFFIX_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".ini": "ini",
    ".cfg": "ini",
    ".mk": "make",
    ".sh": "shell",
    ".dockerfile": "dockerfile",
}


def language_for(path: Path) -> str | None:
    named = NAMED_LANGUAGES.get(path.name)
    if named is not None:
        return named
    if path.name.casefold().endswith(".dockerfile"):  # also a bare ".dockerfile", which has no suffix
        return "dockerfile"
    return SUFFIX_LANGUAGES.get(path.suffix.casefold())


def ensure_coverage(code_index: CodeIndex) -> Mapping[str, str]:
    """Known unindexed ecosystems stay unverifiable instead of being reported absent."""
    for key, level in COVERAGE_MAP.items():
        code_index.coverage.setdefault(key, level)
    return code_index.coverage
