"""Test helpers: ``assert_no_drift`` and the result cache behind the pytest plugin."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import warnings
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from docs_drift import __version__
from docs_drift.api import CheckResult, PathInput, check

CACHE_SCHEMA = 1
# Bound what a fingerprint reads so a misconfigured include cannot hash an unbounded tree.
MAX_FINGERPRINT_FILES = 50_000
MAX_FINGERPRINT_BYTES = 512_000_000


def assert_no_drift(  # noqa: PLR0913 - forwards the keyword-only options of check().
    path: PathInput = ".",
    *,
    docs: str | Sequence[str] | None = None,
    min_confidence: float | None = None,
    history: bool = True,
    baseline: PathInput | None = None,
    config: PathInput | None = None,
    strict: bool = False,
) -> CheckResult:
    """Fail the calling test with a readable report when ``path`` has documentation drift.

    Accepts the same options as ``docs_drift.check()`` and returns its result when
    the check passes, so a test can make further assertions.
    """
    __tracebackhide__ = True
    result = check(
        path,
        docs=docs,
        min_confidence=min_confidence,
        history=history,
        baseline=baseline,
        config=config,
        strict=strict,
    )
    if not result.passed:
        raise AssertionError(result.report())
    return result


def _digest(parts: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", errors="surrogateescape")).hexdigest()


def fingerprint(  # noqa: PLR0913 - every scan input that can change the result is part of the key.
    repo: Path,
    *,
    docs: Sequence[str] | None,
    min_confidence: float | None,
    history: bool,
    baseline: Path | None,
    config: Path | None,
    strict: bool,
) -> str | None:
    """Hash every input of a check: tool version, options, configuration, HEAD, docs and source bytes.

    Returns ``None`` when the inputs cannot be read within the bounds, in which case
    callers must scan without a cache.
    """
    try:
        with warnings.catch_warnings():
            # The scan itself reports discovery and configuration warnings once.
            warnings.simplefilter("ignore")
            return _fingerprint(repo, docs, min_confidence, history, baseline, config, strict)
    except (OSError, ValueError):
        return None


def _fingerprint(  # noqa: PLR0913, PLR0917 - private helper of fingerprint().
    repo: Path,
    docs: Sequence[str] | None,
    min_confidence: float | None,
    history: bool,
    baseline: Path | None,
    config: Path | None,
    strict: bool,
) -> str | None:
    from docs_drift.config import load_config  # noqa: PLC0415 - heavy imports only when the plugin is active.
    from docs_drift.docs.discover import discover_docs  # noqa: PLC0415
    from docs_drift.docs.pipeline import _head  # noqa: PLC0415
    from docs_drift.index.base import source_files  # noqa: PLC0415

    settings = load_config(repo, config)
    if docs is not None:
        settings = settings.model_copy(update={"docs": list(docs)})
    parts = [
        f"docs-drift-check-v{CACHE_SCHEMA}",
        __version__,
        settings.model_dump_json(),
        json.dumps([min_confidence, history, strict, list(docs) if docs is not None else None]),
        _head(repo),
    ]
    if baseline is not None:
        source = baseline if baseline.is_absolute() else repo / baseline
        parts.append("baseline:" + (hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else ""))
    parts.extend(f"doc:{doc.path}:{doc.content_hash}" for doc in discover_docs(repo, settings))
    files = source_files(repo, settings.exclude)
    if len(files) > MAX_FINGERPRINT_FILES:
        return None
    total = 0
    for file in files:
        data = file.read_bytes()
        total += len(data)
        if total > MAX_FINGERPRINT_BYTES:
            return None
        parts.append(f"src:{file.relative_to(repo).as_posix()}:{hashlib.sha256(data).hexdigest()}")
    return _digest(parts)


def read_cached(path: Path, key: str) -> CheckResult | None:
    """Return the stored result for ``key``; a missing, stale or corrupt entry is a miss."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("key") != key:
            return None
        return CheckResult.model_validate(payload["result"])
    except (OSError, ValueError, KeyError, ValidationError):
        return None


def write_cached(path: Path, key: str, result: CheckResult) -> None:
    """Atomically replace the cache entry so concurrent workers never read a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key": key, "result": result.model_dump(mode="json")}, sort_keys=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix="check-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass  # A read-only cache only costs speed.
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


def cached_check(  # noqa: PLR0913 - check() options plus the cache location.
    repo: Path,
    *,
    cache_file: Path | None,
    docs: Sequence[str] | None = None,
    min_confidence: float | None = None,
    history: bool = True,
    baseline: Path | None = None,
    config: Path | None = None,
    strict: bool = False,
) -> tuple[CheckResult, bool]:
    """Run ``check()`` unless an identical-input result is cached; return ``(result, cache_hit)``."""
    key = None
    if cache_file is not None:
        key = fingerprint(
            repo,
            docs=docs,
            min_confidence=min_confidence,
            history=history,
            baseline=baseline,
            config=config,
            strict=strict,
        )
        cached = read_cached(cache_file, key) if key is not None else None
        if cached is not None:
            return cached, True
    result = check(
        repo,
        docs=docs,
        min_confidence=min_confidence,
        history=history,
        baseline=baseline,
        config=config,
        strict=strict,
    )
    if cache_file is not None and key is not None:
        # Only store the result when no input changed while the scan was running.
        after = fingerprint(
            repo,
            docs=docs,
            min_confidence=min_confidence,
            history=history,
            baseline=baseline,
            config=config,
            strict=strict,
        )
        if after == key:
            write_cached(cache_file, key, result)
    return result, False


__all__ = ["assert_no_drift", "cached_check", "fingerprint"]
