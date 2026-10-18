"""Contracts and query helpers for static source indexers."""

import fnmatch
import os
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from docs_drift.models import ClaimKind, Symbol, SymbolKind


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
    coverage: dict[str, str] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    by_kind: dict[SymbolKind, list[Symbol]] = field(default_factory=dict)
    by_name: dict[tuple[SymbolKind, str], list[Symbol]] = field(default_factory=dict)
    by_alias: dict[tuple[SymbolKind, str], list[Symbol]] = field(default_factory=dict)
    by_file: dict[str, list[Symbol]] = field(default_factory=dict)

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


def source_files(repo: Path, exclude: Sequence[str]) -> list[Path]:
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
        except (OSError, subprocess.SubprocessError):
            paths = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]
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
        if path.is_file() and not path.is_symlink():
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def language_for(path: Path) -> str | None:  # noqa: PLR0911
    if path.name in {"Makefile", "GNUmakefile"}:
        return "make"
    if path.name == "justfile":
        return "just"
    if path.name == "Taskfile.yml":
        return "yaml"
    if path.name in {"tox.ini", "setup.cfg"}:
        return "ini"
    if path.name == "Dockerfile" or path.name.casefold().endswith(".dockerfile"):
        return "dockerfile"
    if path.name in {".env.example", ".env.sample", ".env.template"}:
        return "dotenv"
    return {
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
    }.get(path.suffix.casefold())


def ensure_coverage(code_index: CodeIndex) -> Mapping[str, str]:
    """Known unindexed ecosystems stay unverifiable instead of being reported absent."""
    code_index.coverage.setdefault("routes:python", "partial")
    code_index.coverage.setdefault("routes:javascript", "partial")
    code_index.coverage.setdefault("cli_command:python", "partial")
    code_index.coverage.setdefault("cli_command:javascript", "partial")
    for language in ("java", "go", "rust", "ruby", "php", "csharp"):
        code_index.coverage.setdefault(f"all:{language}", "none")
    return code_index.coverage
