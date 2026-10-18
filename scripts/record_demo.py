"""Record actual scan, explain and fix output as an asciinema v2 terminal recording."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from docs_drift.config import Config
from docs_drift.scanning import run_scan


def main() -> None:
    repo = Path("tests/fixtures/repos/py-cli")
    result = run_scan(repo, Config())
    finding = next(f for f in result.findings if f.verdict == "renamed" and f.claim.value == "--port")
    commands = [["scan"], ["explain", finding.claim.claim_id[:14]], ["fix"]]
    env = {**os.environ, "PYTHONUTF8": "1", "NO_COLOR": "1", "COLUMNS": "100"}
    events: list[list[object]] = []
    start = time.monotonic()
    for args in commands:
        displayed = "docs-drift " + " ".join(args) + " --repo " + repo.as_posix() + " --no-color"
        events.append([round(time.monotonic() - start, 3), "o", "$ " + displayed + "\r\n"])
        command = [
            sys.executable,
            "-c",
            "from docs_drift.cli import app; app()",
            *args,
            "--repo",
            str(repo),
            "--no-color",
        ]
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                events.append([round(time.monotonic() - start, 3), "o", line.rstrip("\r\n") + "\r\n"])
            if process.wait():
                raise RuntimeError(f"Demo command failed: {displayed}")
    header = {
        "version": 2,
        "width": 100,
        "height": 35,
        "timestamp": int(time.time()),
        "title": "docs-drift · scan → explain → fix",
        "env": {"TERM": "xterm-256color", "SHELL": "powershell"},
    }
    output = Path("examples/py-cli-demo.cast")
    output.write_text(
        "\n".join(json.dumps(event, ensure_ascii=True) for event in [header, *events]) + "\n", encoding="utf-8"
    )
    print(f"Recorded {len(events)} output events in {time.monotonic() - start:.1f}s to {output}.")


if __name__ == "__main__":
    main()
