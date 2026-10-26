"""Environment variables that the OS, language runtime, CI or a dependency reads on a project's behalf.

Documentation often tells readers about ``HTTPS_PROXY`` or ``XDG_CACHE_HOME`` because the
project honours them, but the project's own source never mentions the name: the standard
library, ``platformdirs`` or ``requests`` reads it. Absence from the static index is then
no evidence of drift, so matching reports these as unverifiable instead of missing.
"""

from __future__ import annotations

import re

STANDARD_VARIABLES = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "BROWSER",
        "CI",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "COLORTERM",
        "COLUMNS",
        "COMSPEC",
        "CONDA_PREFIX",
        "CURL_CA_BUNDLE",
        "DISPLAY",
        "EDITOR",
        "FORCE_COLOR",
        "FTP_PROXY",
        "HOME",
        "HOSTNAME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LINES",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_ENV",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NO_COLOR",
        "NO_PROXY",
        "PAGER",
        "PATH",
        "PATHEXT",
        "PWD",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "VISUAL",
        # Hosted CI services set these for every job.
        "BUILDKITE",
        "CIRCLECI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "JENKINS_URL",
        "TF_BUILD",
        "TRAVIS",
    }
)
STANDARD_PREFIXES = ("XDG_", "LC_", "PYTHON", "PIP_", "UV_", "NPM_CONFIG_", "GITHUB_", "RUNNER_")


def _bare(value: str) -> str:
    value = value.strip().lstrip("$").strip("{}")
    for prefix in ("process.env.", "os.environ.", "env."):
        value = value.removeprefix(prefix)
    return value


def standard_variable(value: str, project_prefixes: set[str]) -> str | None:
    """Return why ``value`` is read outside project code, or None when it may be project-owned."""
    name = _bare(value)
    upper = name.upper()
    if upper != name and upper not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}:
        return None
    if upper in STANDARD_VARIABLES:
        return f"{name} is a standard variable read by the operating system, runtime or CI service."
    for prefix in STANDARD_PREFIXES:
        # A project named like the prefix (pip, uv) owns its own variables.
        if upper.startswith(prefix) and prefix.rstrip("_") not in project_prefixes:
            return f"{name} belongs to the {prefix.rstrip('_')}* family read by the runtime or tooling."
    return None


def dependency_variable(value: str, dependencies: set[str], project_prefixes: set[str]) -> str | None:
    """Name the declared dependency whose prefix this variable carries (``WATCHFILES_FORCE_POLLING``)."""
    name = _bare(value)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        return None
    for dependency in sorted(dependencies, key=len, reverse=True):
        if dependency in project_prefixes:
            continue
        if name == dependency or name.startswith(dependency + "_"):
            return dependency
    return None
