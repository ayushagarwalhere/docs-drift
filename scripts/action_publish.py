"""Composite Action publish step: map Action inputs to `docs-drift github ...` CLI calls.

This step receives the write token and the bundle produced by the scan step; it
never scans repository content itself. Publishing problems become workflow
warnings so they never mask or replace the drift gate's own result.
"""

import os
import sys
from collections.abc import Mapping

import typer

from docs_drift.cli import app
from docs_drift.llm.privacy import mask_secrets
from docs_drift.report.github import escape

PR_EVENTS = {"pull_request", "pull_request_target"}
ISSUE_EVENTS = {"schedule", "workflow_dispatch"}
MODES = {"check", "auto", "pr-review", "issue"}


def resolve_mode(env: Mapping[str, str]) -> str | None:
    """Return the CLI subcommand to run, or None to publish nothing."""
    mode = env.get("DOCS_DRIFT_MODE", "check").strip() or "check"
    comment = env.get("DOCS_DRIFT_COMMENT", "false").strip() or "false"
    if mode not in MODES:
        raise ValueError("mode must be check, auto, pr-review or issue.")
    if comment not in {"true", "false"}:
        raise ValueError("comment must be true or false.")
    if mode == "check":
        if comment == "false":
            return None
        mode = "auto"
    if mode == "auto":
        event = env.get("GITHUB_EVENT_NAME", "")
        return "pr-review" if event in PR_EVENTS else "issue" if event in ISSUE_EVENTS else None
    return mode


def command(env: Mapping[str, str]) -> list[str] | None:
    """Build the exact CLI arguments for this step (tested as the Action contract)."""
    mode = resolve_mode(env)
    bundle = env.get("DOCS_DRIFT_BUNDLE", "").strip()
    if mode is None:
        return None
    if not bundle:
        raise ValueError("The scan step produced no bundle; check its log for the failure.")
    argv = ["github", mode, "--bundle", bundle]
    if mode == "pr-review":
        for name, flag in (("DOCS_DRIFT_CHECK_RUN", "--check-run"),):
            value = env.get(name, "false").strip() or "false"
            if value not in {"true", "false"}:
                raise ValueError(f"{flag.removeprefix('--')} must be true or false.")
            if value == "true":
                argv.append(flag)
        suggestions = env.get("DOCS_DRIFT_SUGGESTIONS", "true").strip() or "true"
        if suggestions not in {"true", "false"}:
            raise ValueError("suggestions must be true or false.")
        if suggestions == "false":
            argv.append("--no-suggestions")
    else:
        argv.extend(["--label", env.get("DOCS_DRIFT_ISSUE_LABEL", "docs-drift").strip() or "docs-drift"])
    if env.get("DOCS_DRIFT_DRY_RUN", "false").strip() == "true":
        argv.append("--dry-run")
    return argv


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        argv = command(env)
    except ValueError as exc:
        print(f"::error title=docs-drift::{exc}")
        return 1
    if argv is None:
        print("docs-drift: nothing to publish for this mode and event.")
        return 0
    print("docs-drift: running docs-drift " + " ".join(argv[:2]))
    try:
        typer.main.get_command(app).main(args=argv, prog_name="docs-drift", standalone_mode=False)
    except SystemExit as exc:
        if exc.code not in {0, None}:
            print("::warning title=docs-drift::Publishing to GitHub failed; the drift gate result is unaffected.")
    except Exception as exc:
        reason = escape(mask_secrets(str(exc))[:300]) or type(exc).__name__
        print(
            f"::warning title=docs-drift::Publishing to GitHub failed: {reason}. The drift gate result is unaffected."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
