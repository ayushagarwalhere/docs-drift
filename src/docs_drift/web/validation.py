"""Allow only canonical public GitHub repository URLs and inert Git refs."""

import re
from dataclasses import dataclass

from docs_drift.limits import MAX_REPO_CHARS, MAX_URL_CHARS

_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,104})(?:/tree/(.+?))?/?", re.ASCII
)
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", re.ASCII)


@dataclass(frozen=True)
class Repository:
    url: str
    ref: str | None


def validate_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if (
        not _REF.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".", ".lock"))
        or any(p.startswith(".") or p.endswith(".lock") for p in ref.split("/"))
    ):
        raise ValueError("Choose a branch, tag or commit SHA; refs cannot contain options, spaces or traversal syntax.")
    return ref


def validate_repository(url: str, ref: str | None = None) -> Repository:
    match = _URL.fullmatch(url) if len(url) <= MAX_URL_CHARS else None
    if not match:
        raise ValueError(
            "Use https://github.com/owner/repo, optionally followed by .git or /tree/ref. "
            "Other hosts, credentials and query strings are not accepted."
        )
    owner, name, embedded_ref = match.groups()
    name = name.removesuffix(".git")
    if name in {".", "..", ""} or len(name) > MAX_REPO_CHARS or name.endswith("."):
        raise ValueError("Choose a valid GitHub repository name.")
    if ref is not None and embedded_ref is not None and ref != embedded_ref:
        raise ValueError("Specify the same ref in the URL and ref field, or use only one.")
    return Repository(f"https://github.com/{owner.lower()}/{name.lower()}", validate_ref(ref or embedded_ref))
