"""Disposable scan process for the GitHub App: checkout in, bundle JSON out.

It never receives an installation token or App key (its environment is the Git
allowlist from ``web/git.py``), loads the repository's own ``.docs-drift.toml`` as
validated TOML data, and only writes JSON status lines to stdout.
"""

import json
import sys
from pathlib import Path

from docs_drift.config import ConfigError, load_config
from docs_drift.github.bundle import BUNDLE_NAME, build_bundle
from docs_drift.llm.privacy import mask_secrets
from docs_drift.policy import GatePolicy
from docs_drift.web.git import JobError
from docs_drift.web.worker import preflight

MAX_HISTORY_COMMITS = 2000


def run(repo: Path, base_sha: str, output: Path) -> None:
    preflight(repo)
    try:
        config = load_config(repo)
    except ConfigError:
        raise JobError(
            "config", "The repository's docs-drift configuration is invalid.", "Run docs-drift locally to see why."
        ) from None
    history = config.history.model_copy(update={"max_commits": min(config.history.max_commits, MAX_HISTORY_COMMITS)})
    # Model calls are a local CLI opt-in; the App never sends repository text to a model.
    config = config.model_copy(update={"history": history, "llm": config.llm.model_copy(update={"enabled": False})})
    bundle = build_bundle(repo, config, GatePolicy(min_confidence=config.min_confidence), base=base_sha)
    output.write_text(bundle.model_dump_json(), encoding="utf-8")


def main() -> None:
    request = json.loads(sys.stdin.readline())
    root = Path(request["workspace"]).resolve()
    try:
        if sys.platform != "win32":
            import resource  # noqa: PLC0415 - Unix-only resource limits.

            resource.setrlimit(resource.RLIMIT_AS, (1_073_741_824, 1_073_741_824))
            resource.setrlimit(resource.RLIMIT_CPU, (170, 180))
        run(root / "repository", request["base_sha"], root / BUNDLE_NAME)
        print(json.dumps({"status": "done"}), flush=True)
    except JobError as exc:
        print(json.dumps({"status": "failed", "kind": exc.kind, "message": str(exc)}), flush=True)
    except Exception as exc:
        # Never send repository content, environment values or tracebacks to logs or GitHub. Plain
        # scanner ValueErrors carry fixed, repository-independent guidance (e.g. archive budget limits).
        message = mask_secrets(str(exc))[:300] if type(exc) is ValueError else "The scan could not finish safely."
        print(json.dumps({"status": "failed", "kind": "scan", "message": message}), flush=True)


if __name__ == "__main__":
    main()
