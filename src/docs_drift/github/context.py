"""Read the GitHub Actions environment and event payload as untrusted, validated data."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docs_drift.github.render import valid_slug

_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_EVENT_BYTES = 30_000_000


@dataclass(frozen=True)
class ActionsContext:
    repository: str | None = None
    event_name: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    fork: bool = False
    api_url: str = "https://api.github.com"
    server_url: str = "https://github.com"
    run_url: str | None = None
    token: str | None = None
    in_actions: bool = False

    def __repr__(self) -> str:
        return (
            f"ActionsContext(repository={self.repository!r}, event={self.event_name!r}, pr={self.pr_number!r}, "
            f"fork={self.fork}, token={'set' if self.token else 'unset'})"
        )

    @property
    def read_only_fork(self) -> bool:
        """`pull_request` runs from forks get a read-only token and no secrets, by GitHub design."""
        return self.fork and self.event_name == "pull_request"


def _event(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > MAX_EVENT_BYTES:
            return {}
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def process_environment() -> dict[str, str]:
    """Read the Actions variables by literal name (greppable, and indexed by docs-drift itself)."""
    values = {
        "GITHUB_EVENT_PATH": os.environ.get("GITHUB_EVENT_PATH"),
        "GITHUB_EVENT_NAME": os.environ.get("GITHUB_EVENT_NAME"),
        "GITHUB_REPOSITORY": os.environ.get("GITHUB_REPOSITORY"),
        "GITHUB_SERVER_URL": os.environ.get("GITHUB_SERVER_URL"),
        "GITHUB_API_URL": os.environ.get("GITHUB_API_URL"),
        "GITHUB_RUN_ID": os.environ.get("GITHUB_RUN_ID"),
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        "GH_TOKEN": os.environ.get("GH_TOKEN"),
        "GITHUB_ACTIONS": os.environ.get("GITHUB_ACTIONS"),
    }
    return {name: value for name, value in values.items() if value is not None}


def actions_context(env: Mapping[str, str] | None = None) -> ActionsContext:
    env = process_environment() if env is None else env
    event = _event(env.get("GITHUB_EVENT_PATH"))
    pr = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else None
    repository = env.get("GITHUB_REPOSITORY") or None
    if repository and not valid_slug(repository):
        repository = None
    number = head = base = None
    fork = False
    if pr:
        number = pr.get("number") if isinstance(pr.get("number"), int) else None
        head_info = pr.get("head") or {}
        base_info = pr.get("base") or {}
        head = head_info.get("sha") if _SHA.fullmatch(str(head_info.get("sha", ""))) else None
        base = base_info.get("sha") if _SHA.fullmatch(str(base_info.get("sha", ""))) else None
        head_repo = (head_info.get("repo") or {}).get("full_name")
        base_repo = (base_info.get("repo") or {}).get("full_name") or repository
        # A deleted fork has a null head repo: treat it as a fork, never as trusted.
        fork = head_repo is None or head_repo != base_repo
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = env.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id.isdigit() else None
    return ActionsContext(
        repository=repository,
        event_name=env.get("GITHUB_EVENT_NAME") or None,
        pr_number=number,
        head_sha=head,
        base_sha=base,
        fork=fork,
        api_url=(env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/"),
        server_url=server.rstrip("/"),
        run_url=run_url,
        token=env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or None,
        in_actions=env.get("GITHUB_ACTIONS") == "true",
    )
