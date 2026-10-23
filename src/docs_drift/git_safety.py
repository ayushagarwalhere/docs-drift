"""Git conversion options that prevent repository-defined filter execution."""

import os
import subprocess
from pathlib import Path

from docs_drift.history import _run


def disabled_filters(repo: Path) -> list[str]:
    keys = _run(repo, ["config", "--name-only", "--list"])
    if keys is None:
        raise ValueError("Cannot inspect repository filter configuration safely.")
    names = {key.rsplit(".", 1)[0] for key in keys.stdout.splitlines() if key.startswith("filter.")}
    options = []
    for name in sorted(names):
        options.extend(
            ["-c", name + ".clean=", "-c", name + ".smudge=", "-c", name + ".process=", "-c", name + ".required=false"]
        )
    return options


def checkout_options(repo: Path) -> list[str]:
    """Read conversion configuration without executing filters, hooks or target programs."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    value = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.autocrlf"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env=env,
    ).stdout.strip()
    options = ["-c", "core.autocrlf=" + (value if value in {"true", "false", "input"} else "false")]
    return [*options, *disabled_filters(repo)]
