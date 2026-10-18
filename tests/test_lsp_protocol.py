"""Exercise pygls over its real stdio JSON-RPC transport, including unsaved edits."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

import pytest

from tests.fixtures.build_fixtures import build_fixtures


def test_stdio_quickfix_and_didchange(tmp_path: Path) -> None:  # noqa: PLR0915 - one transport lifecycle.
    pytest.importorskip("pygls")
    build_fixtures(tmp_path)
    repo = tmp_path / "py-cli"
    uri = (repo / "README.md").as_uri()
    process = subprocess.Popen(
        [sys.executable, "-c", "from docs_drift.cli import app; app()", "lsp", "--repo", str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    messages: Queue[dict[str, Any]] = Queue()

    def read() -> None:
        assert process.stdout is not None
        while line := process.stdout.readline():
            if not line.lower().startswith(b"content-length:"):
                continue
            length = int(line.split(b":")[1])
            while process.stdout.readline().strip():
                pass
            messages.put(json.loads(process.stdout.read(length)))

    Thread(target=read, daemon=True).start()

    def send(method: str, params: dict[str, Any], ident: int | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if ident is not None:
            message["id"] = ident
        data = json.dumps(message).encode()
        assert process.stdin is not None
        process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        process.stdin.flush()

    def receive(key: str, value: object) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = messages.get(timeout=30)
            if message.get(key) == value:
                assert "error" not in message, message
                return message
        raise AssertionError("LSP response timed out.")

    try:
        send("initialize", {"rootUri": repo.as_uri(), "capabilities": {}}, 1)
        assert receive("id", 1)["result"]["capabilities"]["definitionProvider"]
        send("initialized", {})
        source = (repo / "README.md").read_text(encoding="utf-8")
        send(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "markdown", "version": 1, "text": source}},
        )
        diagnostics = receive("method", "textDocument/publishDiagnostics")["params"]["diagnostics"]
        stale = next(d for d in diagnostics if d["code"] == "DD002" and "--port" in d["message"])
        send(
            "textDocument/codeAction",
            {"textDocument": {"uri": uri}, "range": stale["range"], "context": {"diagnostics": [stale]}},
            2,
        )
        actions = receive("id", 2)["result"]
        assert any(a["title"] == "Replace --port with --listen" for a in actions)
        send("textDocument/definition", {"textDocument": {"uri": uri}, "position": stale["range"]["start"]}, 3)
        assert receive("id", 3)["result"]["uri"].endswith("cli.py")
        send(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": source.replace("--port", "--listen")}],
            },
        )
        updated = receive("method", "textDocument/publishDiagnostics")["params"]["diagnostics"]
        assert not any("--port" in d["message"] for d in updated)
    finally:
        process.terminate()
        process.wait(timeout=10)
