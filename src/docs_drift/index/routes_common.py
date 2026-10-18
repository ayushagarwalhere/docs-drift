"""Shared deterministic route matching helpers."""

import re

_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_]\w*\??|<[^>/]+>")


def normalize_route(method: str, path: str) -> tuple[str, str]:
    """Normalize parameter spellings and trailing slashes for matching, preserving raw_path separately."""
    normalized = re.sub(r"/{2,}", "/", path.strip())
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = _PARAM.sub("{param}", normalized)
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return method.casefold(), normalized
