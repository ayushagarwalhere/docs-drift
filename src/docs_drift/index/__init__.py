"""Static source-index registry and deterministic code-index builder."""

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from docs_drift import __version__
from docs_drift.config import Config
from docs_drift.index.base import CodeIndex, Indexer, ensure_coverage, language_for, source_files
from docs_drift.models import Symbol

_T = TypeVar("_T", bound=Indexer)
_REGISTRY: list[Indexer] = []


def register(indexer_type: type[_T]) -> type[_T]:
    """Register a zero-argument indexer; duplicate class registration is ignored."""
    if not any(type(existing) is indexer_type for existing in _REGISTRY):
        _REGISTRY.append(indexer_type())
    return indexer_type


# Imported last so registration is available without a circular registry dependency.
from docs_drift.index.config import ConfigIndexer as _ConfigIndexer  # noqa: E402, F401
from docs_drift.index.js_cli import JavaScriptCLIIndexer as _JavaScriptCLIIndexer  # noqa: E402, F401
from docs_drift.index.js_routes import JavaScriptRouteIndexer as _JavaScriptRouteIndexer  # noqa: E402, F401
from docs_drift.index.js_symbols import JavaScriptIndexer as _JavaScriptIndexer  # noqa: E402, F401
from docs_drift.index.python_cli import PythonCLIIndexer as _PythonCLIIndexer  # noqa: E402, F401
from docs_drift.index.python_routes import PythonRouteIndexer as _PythonRouteIndexer  # noqa: E402, F401
from docs_drift.index.python_source import clear_python_cache  # noqa: E402
from docs_drift.index.python_symbols import PythonIndexer as _PythonIndexer  # noqa: E402, F401
from docs_drift.index.python_symbols import _module_name  # noqa: E402
from docs_drift.index.tasks import TaskIndexer as _TaskIndexer  # noqa: E402, F401

INDEX_SCHEMA = 1
INDEXER_VERSION = f"{__version__}:5"


def _fingerprints(repo: Path, files: Sequence[Path]) -> dict[str, str]:
    # Git's ls-files -m can invoke repository clean filters while refreshing stat
    # data. Hash bounded source bytes directly; cached parsing still remains useful.
    return {
        path.relative_to(repo).as_posix(): "worktree:" + hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }


def _read_cache(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (
            value
            if isinstance(value, dict)
            and value.get("schema") == INDEX_SCHEMA
            and value.get("indexer_version") == INDEXER_VERSION
            else None
        )
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix="index-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def clear_cache(repo: Path) -> bool:
    """Remove only this repository's index cache; return whether a cache existed."""
    path = repo.resolve() / ".docs-drift" / "cache" / f"index-v{INDEX_SCHEMA}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def build_index(repo: Path, config: Config, *, no_cache: bool = False) -> CodeIndex:  # noqa: PLR0912, PLR0915
    """Index tracked source text with per-file failure isolation and stable ordering."""
    root = repo.resolve()
    _module_name.cache_clear()
    clear_python_cache()
    file_warnings: list[str] = []
    files = source_files(root, config.exclude, warnings_out=file_warnings)
    grouped: dict[str, list[Path]] = {}
    for path in files:
        language = language_for(path)
        if language:
            grouped.setdefault(language, []).append(path)
    result = CodeIndex(
        warnings=file_warnings,
        files_by_language={language: len(paths) for language, paths in sorted(grouped.items())},
        source_paths=[path.relative_to(root).as_posix() for path in files],
    )
    cache_path = root / ".docs-drift" / "cache" / f"index-v{INDEX_SCHEMA}.json"
    if not cache_path.resolve().is_relative_to(root):
        no_cache = True
    fingerprints = _fingerprints(root, files)
    cached = _read_cache(cache_path) if not no_cache else None
    cached_files_obj = cached.get("files", {}) if cached else {}
    cached_files = cached_files_obj if isinstance(cached_files_obj, dict) else {}
    valid = cached is not None
    for relative, fingerprint in fingerprints.items():
        file_cache = cached_files.get(relative)
        if valid and isinstance(file_cache, dict) and file_cache.get("fingerprint") == fingerprint:
            symbol_items = file_cache.get("symbols", [])
            if isinstance(symbol_items, list):
                result.symbols.extend(Symbol.model_validate(item) for item in symbol_items if isinstance(item, dict))
                result.cache_hits += 1
                continue
        result.cache_misses += 1
    changed = {
        relative
        for relative, fingerprint in fingerprints.items()
        if not (
            valid
            and isinstance(cached_files.get(relative), dict)
            and cached_files[relative].get("fingerprint") == fingerprint
        )
    }
    cached_warnings = cached.get("warnings") if cached else None
    if not changed and isinstance(cached_warnings, list):
        result.warnings.extend(str(warning) for warning in cached_warnings)
    indexed_by_file: dict[str, list[Symbol]] = {}
    for symbol in result.symbols:
        indexed_by_file.setdefault(symbol.file, []).append(symbol)
    registered = list(_REGISTRY)
    # Additional built-ins are loaded here after their implementations exist.
    for language, language_paths in grouped.items():
        paths = [path for path in language_paths if path.relative_to(root).as_posix() in changed]
        if not paths:
            continue
        for indexer in registered:
            if language not in indexer.languages or not indexer.applies_to(root):
                continue
            batch = bool(getattr(indexer, "batch_files", False))
            if batch:
                try:
                    new_symbols = indexer.index(root, paths)
                    result.symbols.extend(new_symbols)
                    for symbol in new_symbols:
                        indexed_by_file.setdefault(symbol.file, []).append(symbol)
                except Exception as exc:
                    result.warnings.append(
                        f"Indexer {indexer.name} batch failed on {language}; retrying individual files: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    for path in paths:
                        try:
                            new_symbols = indexer.index(root, [path])
                            result.symbols.extend(new_symbols)
                            for symbol in new_symbols:
                                indexed_by_file.setdefault(symbol.file, []).append(symbol)
                        except Exception as file_exc:
                            relative = path.relative_to(root).as_posix()
                            result.warnings.append(
                                f"Indexer {indexer.name} skipped {relative}: {type(file_exc).__name__}: {file_exc}"
                            )
            else:
                for path in paths:
                    try:
                        new_symbols = indexer.index(root, [path])
                        result.symbols.extend(new_symbols)
                        for symbol in new_symbols:
                            indexed_by_file.setdefault(symbol.file, []).append(symbol)
                    except Exception as exc:
                        result.warnings.append(f"Indexer {indexer.name} failed on {path}: {type(exc).__name__}: {exc}")
            result.warnings.extend(str(note) for note in getattr(indexer, "warnings", []))
    ensure_coverage(result)
    result.__post_init__()
    if not no_cache:
        old_files = {
            name: value for name, value in cached_files.items() if name in fingerprints and name not in changed
        }
        fresh_files = {
            relative: {
                "fingerprint": fingerprints[relative],
                "symbols": [symbol.model_dump(mode="json") for symbol in indexed_by_file.get(relative, [])],
                "import_edges": [],
            }
            for relative in fingerprints
        }
        _write_cache(
            cache_path,
            {
                "schema": INDEX_SCHEMA,
                "indexer_version": INDEXER_VERSION,
                "files": {**old_files, **fresh_files},
                "warnings": list(dict.fromkeys(result.warnings)),
            },
        )
    clear_python_cache()
    _module_name.cache_clear()
    return result


__all__ = ["CodeIndex", "Indexer", "build_index", "clear_cache", "register"]
