"""Bounded partial clone with no user Git config, credentials, hooks or smudge filters."""

import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from docs_drift.limits import CONTROL_START, DELETE_CHARACTER, MAX_GIT_OUTPUT, MAX_PATH_CHARS
from docs_drift.web.settings import Settings
from docs_drift.web.validation import Repository

SOURCE_SUFFIXES = {
    ".md",
    ".mdx",
    ".markdown",
    ".rst",
    ".txt",
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".mk",
}
SOURCE_NAMES = {"Makefile", "GNUmakefile", "justfile", "Dockerfile", "Taskfile.yml"}


class JobError(Exception):
    def __init__(self, kind: str, message: str, hint: str):
        super().__init__(message)
        self.kind, self.hint = kind, hint


def git_environment(home: Path) -> dict[str, str]:
    # Inherit only execution essentials, never tokens, proxies, SSH agents or model keys.
    env = {
        k: os.environ[k]
        for k in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG")
        if k in os.environ
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
        GIT_OPTIONAL_LOCKS="0",
        PYTHONUTF8="1",
    )
    return env


def disk_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.kill()
    process.wait(timeout=10)


def git(args: list[str], root: Path, settings: Settings, deadline: float, *, stdin: bytes | None = None) -> bytes:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    command = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.required=false",
        *args,
    ]
    if os.name == "nt":
        command[1:1] = ["-c", "http.sslBackend=openssl"]
    # Temporary files prevent a full pipe from deadlocking our disk/timeout monitor.

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=git_environment(root),
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
        )
        try:
            if stdin and process.stdin:
                process.stdin.write(stdin)
                process.stdin.close()
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise JobError(
                        "timeout",
                        "Git took too long to download this repository.",
                        "Try a smaller repository or a pre-scanned example.",
                    )
                if disk_bytes(root) > settings.max_bytes:
                    raise JobError(
                        "too_large", "The repository exceeded the 200 MB download limit.", "Try a smaller repository."
                    )
                time.sleep(0.1)
            if process.returncode:
                raise JobError(
                    "clone",
                    "The repository or ref could not be fetched; it may be private, missing or redirected.",
                    "Use the canonical public GitHub URL and check the branch or tag.",
                )
            out.seek(0)
            output = out.read(8_000_001)
            if len(output) > MAX_GIT_OUTPUT:
                raise JobError("too_large", "The repository listing is too large.", "Try a smaller repository.")
            return output
        finally:
            kill_process(process)


def resolve(repository: Repository, root: Path, settings: Settings, deadline: float) -> str:
    ref = repository.ref or "HEAD"
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    patterns = ["HEAD"] if ref == "HEAD" else [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    rows = git(["ls-remote", "--exit-code", repository.url, *patterns], root, settings, deadline).decode().splitlines()
    matches = [row.split() for row in rows if row.strip()]
    if not matches:
        raise JobError(
            "clone", "This branch or tag was not found.", "Check the ref or leave it empty for the default branch."
        )
    annotated = [row[0] for row in matches if row[1].endswith("^{}")]
    sha = (annotated or [matches[0][0]])[0]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise JobError("clone", "GitHub returned an unexpected commit identifier.", "Try again later.")
    return sha


def checkout(  # noqa: PLR0913 - trusted clone configuration, not request-controlled options.
    repository: Repository,
    sha: str,
    root: Path,
    settings: Settings,
    deadline: float,
    *,
    blob_filter: str = "blob:none",
    depth_only: bool = False,
) -> Path:
    repo = root / "repository"
    repo.mkdir()
    git(["init", "--quiet", "--template=", str(repo)], root, settings, deadline)
    prefix = ["-C", str(repo)]
    git([*prefix, "remote", "add", "origin", repository.url], root, settings, deadline)
    for key, value in (
        ("http.followRedirects", "false"),
        ("protocol.file.allow", "never"),
        ("protocol.ext.allow", "never"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("core.hooksPath", os.devnull),
    ):
        git([*prefix, "config", key, value], root, settings, deadline)
    if os.name == "nt":
        git([*prefix, "config", "http.sslBackend", "openssl"], root, settings, deadline)
    git([*prefix, "config", "remote.origin.promisor", "true"], root, settings, deadline)
    git([*prefix, "config", "remote.origin.partialclonefilter", blob_filter], root, settings, deadline)
    since = (datetime.now(UTC) - timedelta(days=settings.history_days)).date().isoformat()
    fetch = [
        *prefix,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        f"--filter={blob_filter}",
        f"--depth={settings.history_commits}" if depth_only else f"--shallow-since={since}",
        "origin",
        sha,
    ]
    try:
        git(fetch, root, settings, deadline)
    except JobError as exc:
        if exc.kind != "clone":
            raise
        # Old inactive projects may have no commits within the date window.
        fetch = [arg for arg in fetch if not arg.startswith("--shallow-since=")]
        fetch.insert(-2, f"--depth={settings.history_commits}")
        git(fetch, root, settings, deadline)
    listing = git([*prefix, "ls-tree", "-r", "-z", sha], root, settings, deadline).split(b"\0")
    entries = [entry for entry in listing if entry]
    if len(entries) > settings.max_files:
        raise JobError(
            "too_large", "The repository exceeds the 20,000 file limit.", "Try a smaller repository or an example."
        )
    selected = []
    for entry in entries:
        meta, raw = entry.split(b"\t", 1)
        path = raw.decode("utf-8", errors="strict")
        parts = PurePosixPath(path).parts
        if (
            len(path) > MAX_PATH_CHARS
            or any(ord(c) < CONTROL_START or ord(c) == DELETE_CHARACTER for c in path)
            or "\\" in path
            or path.startswith(("/", "-"))
            or ".." in parts
            or any(p.lower() == ".git" for p in parts)
        ):
            raise JobError(
                "filename",
                "The repository contains unsupported filenames.",
                "Use an example, or scan this repository locally after reviewing its paths.",
            )
        if meta.startswith(b"100") and not any(
            p.startswith(".env") or p in {"node_modules", "vendor", "dist", "build"} for p in parts
        ):
            item = PurePosixPath(path)
            if item.suffix.lower() in SOURCE_SUFFIXES or item.name in SOURCE_NAMES:
                # Non-cone sparse patterns are anchored and escaped; never inject patterns through paths.
                selected.append("/" + "".join("\\" + c if c in "*?[]!#\\" else c for c in path))
    if not selected:
        raise JobError(
            "no_docs",
            "No supported documentation or source files were found.",
            "Try a repository with Markdown docs and Python or JavaScript source.",
        )
    git([*prefix, "sparse-checkout", "init", "--no-cone"], root, settings, deadline)
    git(
        [*prefix, "sparse-checkout", "set", "--no-cone", "--stdin"],
        root,
        settings,
        deadline,
        stdin=("\n".join(selected) + "\n").encode(),
    )
    git([*prefix, "checkout", "--quiet", "--detach", sha], root, settings, deadline)
    if disk_bytes(root) > settings.max_bytes:
        raise JobError("too_large", "The repository exceeded the 200 MB limit.", "Try a smaller repository.")
    return repo
