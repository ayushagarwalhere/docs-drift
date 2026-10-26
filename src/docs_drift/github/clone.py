"""Bounded, credential-scoped fetch of a pull request's head and base commits.

Follows the public demo's hardening (``web/git.py``): no user/system Git config,
no hooks, no LFS smudge, no submodules, no redirects, HTTPS only, disk and time
limits, sparse checkout of text sources only, and symlinks materialized as plain
files. The installation token reaches Git only through ``GIT_CONFIG_*``
environment entries of the fetch process: it never appears in argv, ``.git/config``,
the scan subprocess or logs. Full blobs are fetched so later Git reads need no
network access or credentials.
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from docs_drift.limits import CONTROL_START, DELETE_CHARACTER, MAX_FILE_BYTES, MAX_GIT_OUTPUT, MAX_PATH_CHARS
from docs_drift.web.git import SOURCE_NAMES, SOURCE_SUFFIXES, JobError, disk_bytes, git_environment, kill_process

_SHA = re.compile(r"^[0-9a-f]{40}$")
SKIPPED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock"}


def _command(args: list[str], allow_local: bool) -> list[str]:
    options = [
        f"core.hooksPath={os.devnull}",
        "core.fsmonitor=false",
        "core.symlinks=false",
        "protocol.allow=never",
        "protocol.ext.allow=never",
        f"protocol.file.allow={'always' if allow_local else 'never'}",
        "protocol.https.allow=always",
        "http.followRedirects=false",
        "credential.helper=",
        "filter.lfs.smudge=",
        "filter.lfs.process=",
        "filter.lfs.required=false",
        "submodule.recurse=false",
    ]
    if os.name == "nt":
        options.append("http.sslBackend=openssl")
    return ["git", *[part for option in options for part in ("-c", option)], *args]


def run_git(  # noqa: PLR0913 - explicit trusted limits.
    args: list[str],
    root: Path,
    deadline: float,
    max_bytes: int,
    *,
    token: str | None = None,
    allow_local: bool = False,
    stdin: bytes | None = None,
) -> bytes:
    env = git_environment(root)
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {credential}",
        )
    options: dict[str, int] = {}
    if sys.platform == "win32":
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            _command(args, allow_local),
            cwd=root,
            env=env,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            **options,  # type: ignore[call-overload]
        )
        try:
            if stdin and process.stdin:
                process.stdin.write(stdin)
                process.stdin.close()
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise JobError(
                        "timeout", "Fetching the pull request took too long.", "Retry with /docs-drift recheck."
                    )
                if disk_bytes(root) > max_bytes:
                    raise JobError("too_large", "The repository exceeded the download limit.", "Scan locally.")
                time.sleep(0.05)
            if process.returncode:
                raise JobError("clone", "The pull request commits could not be fetched.", "Check the App installation.")
            out.seek(0)
            output = out.read(MAX_GIT_OUTPUT + 1)
            if len(output) > MAX_GIT_OUTPUT:
                raise JobError("too_large", "The repository listing is too large.", "Scan locally.")
            return output
        finally:
            kill_process(process)


def _selected(listing: bytes, max_files: int) -> list[str]:
    """Validate every tree path; keep regular text-source files under the per-file size limit."""
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) > max_files:
        raise JobError("too_large", "The repository exceeds the file-count limit.", "Scan locally.")
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
            raise JobError("filename", "The repository contains unsupported filenames.", "Scan locally.")
        mode, _kind, _sha, size = meta.decode("ascii").split()
        item = PurePosixPath(path)
        if (
            mode.startswith("100")
            and size.isdigit()
            and int(size) <= MAX_FILE_BYTES
            and item.name not in SKIPPED_NAMES
            and not any(p.startswith(".env") or p in {"node_modules", "vendor", "dist", "build"} for p in parts)
            and (item.suffix.lower() in SOURCE_SUFFIXES or item.name in SOURCE_NAMES)
        ):
            selected.append("/" + "".join("\\" + c if c in "*?[]!#\\" else c for c in path))
    if not selected:
        raise JobError("no_docs", "No supported documentation or source files were found.", "Nothing to check.")
    return selected


def fetch_pull_request(  # noqa: PLR0913 - trusted job configuration, never request-controlled options.
    remote: str,
    head_sha: str,
    base_sha: str,
    root: Path,
    *,
    token: str | None,
    deadline: float,
    max_bytes: int = 200_000_000,
    max_files: int = 20_000,
    depth: int = 200,
    allow_local: bool = False,
) -> Path:
    """Fetch both commits by SHA into ``root/repository`` and check out the head (sparse, detached)."""
    if not (_SHA.fullmatch(head_sha) and _SHA.fullmatch(base_sha)):
        raise JobError("clone", "GitHub sent an unexpected commit identifier.", "Retry later.")
    if not allow_local and not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+\.git", remote):
        raise JobError("clone", "Only github.com repositories are supported.", "Check the installation.")
    repo = root / "repository"
    repo.mkdir()

    def git(args: list[str], *, credentials: bool = False, stdin: bytes | None = None) -> bytes:
        return run_git(
            ["-C", str(repo), *args],
            root,
            deadline,
            max_bytes,
            token=token if credentials else None,
            allow_local=allow_local,
            stdin=stdin,
        )

    run_git(["init", "--quiet", "--template=", str(repo)], root, deadline, max_bytes)
    git(["remote", "add", "origin", remote])
    git(["config", "core.symlinks", "false"])
    git(["config", "core.hooksPath", os.devnull])
    git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            f"--depth={depth}",
            "origin",
            head_sha,
            base_sha,
        ],
        credentials=True,
    )
    listing = git(["ls-tree", "-r", "-z", "-l", head_sha])
    git(["sparse-checkout", "init", "--no-cone"])
    git(
        ["sparse-checkout", "set", "--no-cone", "--stdin"],
        stdin=("\n".join(_selected(listing, max_files)) + "\n").encode(),
    )
    git(["checkout", "--quiet", "--detach", head_sha])
    if disk_bytes(root) > max_bytes:
        raise JobError("too_large", "The repository exceeded the download limit.", "Scan locally.")
    return repo
