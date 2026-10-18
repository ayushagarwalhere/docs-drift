"""Trusted scheduled health monitor; alerts only after two consecutive failed probes."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime


def probe(base: str) -> bool:
    try:
        for path in ("/healthz", "/api/scans/example-py-cli"):
            with urllib.request.urlopen(base + path, timeout=25) as response:
                data = json.load(response)
                if data.get("status") not in {"ok", "done"}:
                    return False
        return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def main() -> None:
    today = datetime.now(UTC).date().isoformat()
    if os.getenv("MANUAL") != "true" and not "2026-10-18" <= today <= "2026-11-06":
        print("Outside judging window; skipped.")
        return
    base = os.environ["BASE_URL"].rstrip("/")
    if probe(base):
        print("PASS service and cached example")
        return
    time.sleep(10)
    if probe(base):
        print("Recovered on second probe")
        return
    existing = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", '"Demo uptime alert" in:title', "--json", "number"],
        capture_output=True,
        text=True,
        check=True,
    )
    if not json.loads(existing.stdout):
        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                "Demo uptime alert",
                "--body",
                "The judging monitor could not load /healthz or the baked py-cli example on two consecutive probes. "
                "Check the hosting logs and rollback instructions in docs/deploy.md.",
            ],
            check=True,
        )
    raise SystemExit("Health check failed twice; issue exists or was opened.")


if __name__ == "__main__":
    main()
