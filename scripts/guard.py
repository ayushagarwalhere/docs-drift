"""Check the exact staged blobs without printing secret values or running their code."""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 1_000_000  # One decimal MB; do not raise to accommodate generated artifacts.


@dataclass(frozen=True)
class StagedFile:
    """Git paths are decoded losslessly, including spaces and non-ASCII names."""

    path: Path
    object_id: str


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def staged_files(repo: Path) -> list[StagedFile]:
    """Read NUL-separated raw changes so quoting and platform separators cannot split paths."""
    raw = _git(repo, "diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "--diff-filter=ACM", "-z")
    fields = raw.split(b"\0")
    files = []
    for header, name in zip(fields[0:-1:2], fields[1::2], strict=True):
        metadata = header.split()
        if metadata[1] == b"160000":  # Submodule commits are not file blobs.
            continue
        files.append(StagedFile(Path(os.fsdecode(name)), metadata[3].decode("ascii")))
    return files


def secret_findings(path: Path, content: bytes) -> list[str]:
    """Recognize token-shaped strings, including binary files; never echo matched bytes."""
    patterns = (
        ("OpenRouter API key", rb"\bsk-or-v1-[A-Za-z0-9_-]{8,}"),
        ("Anthropic API key", rb"\bsk-ant-[A-Za-z0-9_-]{8,}"),
        ("GitHub token", rb"\bghp_[A-Za-z0-9]{20,}"),
        ("GitHub fine-grained token", rb"\bgithub_pat_[A-Za-z0-9_]{20,}"),
        ("AWS access key ID", rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    )
    findings = []
    for label, pattern in patterns:
        for match in re.finditer(pattern, content):
            line = content.count(b"\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: possible {label}; remove it and use an environment variable.")
    return findings


def inspect_staged(repo: Path) -> list[str]:
    """Inspect index contents rather than potentially different working-tree files."""
    findings = []
    for file in staged_files(repo):
        basename = file.path.name.casefold()
        if basename == ".env" or basename.startswith(".env."):
            findings.append(f"{file.path}: .env files must not be committed; unstage it and use local configuration.")
        size = int(_git(repo, "cat-file", "-s", file.object_id))
        if size > MAX_FILE_BYTES:
            findings.append(f"{file.path}: staged file exceeds 1 MB; unstage it and store the artifact elsewhere.")
            continue
        findings.extend(secret_findings(file.path, _git(repo, "cat-file", "blob", file.object_id)))
    return findings


def main(repo: Path | None = None) -> int:
    """Fail closed when Git cannot read the index, with an actionable error."""
    try:
        findings = inspect_staged(Path.cwd() if repo is None else repo)
    except (OSError, subprocess.CalledProcessError):
        print("Guard could not read the Git index. Run it inside a Git repository with Git installed.", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
