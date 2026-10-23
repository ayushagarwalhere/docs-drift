"""Linear, line-oriented secret masking and repository instruction detection."""

import fnmatch
import re
from typing import Any

from docs_drift.models import ScanResult

_KEY = re.compile(r"\b(?:sk-(?:ant|or-v1)-|ghp_|github_pat_|AKIA|ASIA)[A-Za-z0-9_-]{4,256}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b[\s\"']{0,8}[:=][\s\"']{0,8}[^\s\"']{3,256}"
)
_CONNECTION = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]{0,20}://[^\s/:@]{1,128}:[^\s@]{1,256}@")
_INSTRUCTION = re.compile(
    r"(?i)ignore (?:all |any |previous |prior |the )?(?:instructions|rules|system)|"
    r"report all claims as|system prompt|you are (?:now|an? assistant)|"
    r"override (?:the |all )?(?:verdict|instructions)|(?:return|output) (?:only )?supported"
)


def suspicious_text(text: str) -> bool:
    return _INSTRUCTION.search(text) is not None


def mask_secrets(text: str) -> str:
    """Mask whole secret-bearing lines, preserving line count and ending style.

    Patterns have bounded repetitions and no nested quantifiers. Whole-line
    replacement also prevents a very long token's unmatched suffix leaking.
    Private keys use a line state machine instead of an unbounded multiline regex.
    """
    result: list[str] = []
    private_key = False
    for line in text.splitlines(keepends=True):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            private_key = True
        secret = private_key or bool(_KEY.search(line) or _ASSIGNMENT.search(line) or _CONNECTION.search(line))
        result.append("[REDACTED]" + ending if secret else line)
        if "-----END " in line and "PRIVATE KEY-----" in line:
            private_key = False
    return "".join(result)


def excluded_path(path: str, patterns: list[str]) -> bool:
    defaults = [".env", ".env.*", "**/.env", "**/.env.*", "*.pem", "**/*.pem", "*.key", "**/*.key"]
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in [*defaults, *patterns])


def redact_value(value: Any) -> Any:
    """Mask nested metadata without changing its JSON structure."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value


def redact_result(result: ScanResult) -> ScanResult:
    """Return a redacted copy so every report format shares the same secret boundary."""
    return ScanResult.model_validate(redact_value(result.model_dump(mode="json")))
