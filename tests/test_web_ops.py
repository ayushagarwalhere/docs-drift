"""Monitoring decisions are tested without contacting a service or opening issues."""

import json
import subprocess
import time
from typing import Any

import pytest

from scripts import monitor_web


def test_monitor_success_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANUAL", "true")
    monkeypatch.setenv("BASE_URL", "https://example.test")
    monkeypatch.setattr(monitor_web, "probe", lambda base: True)
    monitor_web.main()
    results = iter([False, True])
    monkeypatch.setattr(monitor_web, "probe", lambda base: next(results))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monitor_web.main()


@pytest.mark.parametrize("existing", [[], [{"number": 42}]])
def test_monitor_two_failures_deduplicates_alert(
    existing: list[dict[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANUAL", "true")
    monkeypatch.setenv("BASE_URL", "https://example.test")
    monkeypatch.setattr(monitor_web, "probe", lambda base: False)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(existing), "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match="failed twice"):
        monitor_web.main()
    assert len(calls) == (1 if existing else 2)
    assert calls[0][:3] == ["gh", "issue", "list"]
