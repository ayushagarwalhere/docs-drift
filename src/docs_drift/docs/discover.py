"""Discover bounded documentation files without importing or executing repository code."""

import hashlib
import os
import subprocess
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pathspec import GitIgnoreSpec

from docs_drift.config import Config
from docs_drift.models import DocFile, DocFormat

MAX_DOC_BYTES = 2_000_000


class DiscoveryWarning(UserWarning):
    """A documentation candidate or discovery mechanism could not be used."""


def _warn(message: str) -> None:
    warnings.warn(message, DiscoveryWarning, stacklevel=3)


def _globs(patterns: Sequence[str]) -> GitIgnoreSpec:
    # Anchor configured globs to the scan root; **/ may match zero directories.
    return GitIgnoreSpec.from_lines("/" + pattern.replace("\\", "/").lstrip("/") for pattern in patterns)


def _tracked_paths(repo: Path) -> list[str] | None:
    # A directory inside some unrelated parent checkout is not itself a repo root.
    if not (repo / ".git").exists():
        return None
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Repository-configured fsmonitor commands/hooks must never run during a scan.
    command = [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(repo),
        "ls-files",
        "-z",
        "--cached",
        "--exclude-standard",
    ]
    try:
        tracked = subprocess.run(command, check=True, capture_output=True, env=env, timeout=5).stdout
        ignored = subprocess.run([*command, "--ignored"], check=True, capture_output=True, env=env, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        _warn("Cannot list tracked documentation with Git; walking the repository and its .gitignore files instead.")
        return None
    ignored_paths = set(ignored.split(b"\0"))
    return [os.fsdecode(path) for path in tracked.split(b"\0") if path and path not in ignored_paths]


@dataclass(frozen=True)
class _IgnoreLayer:
    directory: PurePosixPath
    spec: GitIgnoreSpec


def _ignored(path: str, is_directory: bool, layers: tuple[_IgnoreLayer, ...]) -> bool:
    ignored = False
    for layer in layers:
        relative = PurePosixPath(path).relative_to(layer.directory).as_posix()
        result = layer.spec.check_file(relative + ("/" if is_directory else ""))
        if result.include is not None:
            ignored = result.include
    return ignored


def _inside(repo: Path, path: Path) -> bool:
    return not path.is_symlink() and path.resolve().is_relative_to(repo)


def _walk(repo: Path, directory: Path, layers: tuple[_IgnoreLayer, ...] = ()) -> Iterator[str]:
    ignore_file = directory / ".gitignore"
    try:
        if ignore_file.is_file() and _inside(repo, ignore_file):
            spec = GitIgnoreSpec.from_lines(ignore_file.read_text(encoding="utf-8", errors="replace").splitlines())
            layers = (*layers, _IgnoreLayer(PurePosixPath(directory.relative_to(repo).as_posix()), spec))
        children = sorted(directory.iterdir())
    except OSError:
        _warn(f"Cannot read directory {directory}; check its permissions.")
        return
    vendors = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor", "__pycache__", ".docs-drift"}
    for child in children:
        relative = child.relative_to(repo).as_posix()
        if not _inside(repo, child):
            continue
        is_directory = child.is_dir()
        if _ignored(relative, is_directory, layers):
            continue
        if is_directory:
            if child.name not in vendors:
                yield from _walk(repo, child, layers)
        elif child.is_file():
            yield relative


def _format(path: str) -> DocFormat | None:
    return {
        ".md": DocFormat.markdown,
        ".markdown": DocFormat.markdown,
        ".mdx": DocFormat.mdx,
        ".rst": DocFormat.rst,
        ".txt": DocFormat.text,
        ".text": DocFormat.text,
    }.get(PurePosixPath(path).suffix.casefold())


def _read_bytes(repo: Path, relative: str) -> bytes:
    path = repo / relative
    if not _inside(repo, path) or not path.is_file():
        raise ValueError(f"{relative} is not a regular file inside the repository. Rediscover documentation.")
    if path.stat().st_size > MAX_DOC_BYTES:
        raise ValueError(f"{relative} exceeds 2 MB and was skipped. Narrow the configured documentation globs.")
    with path.open("rb") as stream:
        content = stream.read(MAX_DOC_BYTES + 1)
    if len(content) > MAX_DOC_BYTES:
        raise ValueError(f"{relative} exceeds 2 MB and was skipped. Narrow the configured documentation globs.")
    return content


def discover_docs(repo: Path, config: Config) -> list[DocFile]:
    """Select tracked files in Git roots, otherwise walk with layered .gitignore rules."""
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError("Choose an existing repository directory before discovering documentation.")
    patterns = config.docs
    if patterns == Config().docs:
        from docs_drift.workspaces import docs_patterns  # noqa: PLC0415 - optional dependency or shared pipeline import.

        patterns = [*patterns, *docs_patterns(root)]
    includes, excludes = _globs(patterns), _globs(config.exclude)
    paths = _tracked_paths(root)
    candidates = _walk(root, root) if paths is None else iter(paths)
    docs = []
    for relative in sorted(set(candidates)):
        format_ = _format(relative)
        if format_ is None or not includes.match_file(relative) or excludes.match_file(relative):
            continue
        try:
            content = _read_bytes(root, relative)
        except (OSError, ValueError) as exc:
            _warn(f"Cannot read documentation {relative}: {exc}")
            continue
        try:
            docs.append(DocFile(path=relative, content_hash=hashlib.sha256(content).hexdigest(), format=format_))
        except ValueError:
            _warn(f"Cannot read documentation {relative!r}: unsupported path; skipped.")
    return docs


def read_doc(repo: Path, doc: DocFile) -> str:
    """Read the discovered byte snapshot as UTF-8 with replacement, detecting intervening edits."""
    content = _read_bytes(repo.resolve(), doc.path)
    if hashlib.sha256(content).hexdigest() != doc.content_hash:
        raise ValueError(f"{doc.path} changed since discovery. Rediscover documentation before parsing it.")
    return content.decode("utf-8", errors="replace")
