"""Write with voice: saved static indexes, the /api/write/check boundary and the editor page."""

import asyncio
import json
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docs_drift.config import Config
from docs_drift.index.base import CodeIndex
from docs_drift.models import Package
from docs_drift.scanning import run_scan
from docs_drift.web import write
from docs_drift.web.app import create_app
from docs_drift.web.jobs import Job, Store
from docs_drift.web.settings import Settings
from docs_drift.web.worker import execute, public_result, scrub
from docs_drift.web.write import (
    MAX_WRITE_TEXT_BYTES,
    WRITE_INDEX_FILE,
    WriteIndex,
    build_write_index,
    dump_write_index,
    load_write_index,
    materialize,
    normalize_dictation,
)

# Read the examples shipped inside the installed package, so this also passes against the built wheel.
EXAMPLES = Path(str(files("docs_drift").joinpath("web/examples")))
ORIGIN = {"origin": "http://127.0.0.1:8080"}

# Assembled at runtime so secret scanners never see a key-shaped literal in the repository.
FAKE_ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "abcdefghijklmnopqrstuvwxyz0123456789"


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Tool\n\nRun `tool serve --listen 9000`.\n", encoding="utf-8")
    (root / "cli.py").write_text(
        "import argparse, os\n"
        'p = argparse.ArgumentParser(prog="tool")\n'
        's = p.add_subparsers().add_parser("serve")\n'
        's.add_argument("--listen", type=int, default=8000)\n'
        's.add_argument("--workers", type=int, default=8)\n'
        'TOKEN = os.environ.get("TOOL_TOKEN", "' + FAKE_ANTHROPIC_KEY + '")\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "tool"\nversion = "1"\n[project.scripts]\ntool = "cli:main"\n', encoding="utf-8"
    )
    return root


def capture(repo: Path) -> tuple[CodeIndex, list[Package], str]:
    found: dict[str, Any] = {}
    result = run_scan(
        repo, Config(), history=False, on_index=lambda index, packages: found.update(index=index, packages=packages)
    )
    return found["index"], found["packages"], result.head_sha


def check(client: TestClient, scan_id: str, text: str, **extra: Any) -> Any:
    return client.post("/api/write/check", json={"scan_id": scan_id, "text": text, **extra}, headers=ORIGIN)


def by_claim(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # Default claims share their subject's name ("--port" default 8000); key names by the name claims.
    return {finding["claim"]: finding for finding in body["findings"] if finding["kind"] != "default_value"}


def test_index_round_trip_keeps_matching_inputs_and_masks_secrets(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    index, packages, head = capture(repo)
    saved = build_write_index(repo, index, packages, head, scrub=scrub)
    loaded = WriteIndex.model_validate_json(dump_write_index(saved))
    assert loaded == saved
    rebuilt = loaded.code_index()
    assert [s.symbol_id for s in rebuilt.symbols] == [s.symbol_id for s in index.symbols]
    assert rebuilt.source_paths == index.source_paths and rebuilt.files_by_language == index.files_by_language
    assert rebuilt.coverage == index.coverage
    assert "tool" in loaded.metadata["pyproject.toml"]
    assert "sk-ant-api03" not in dump_write_index(saved)  # Same scrubbing as the public result.


def test_oversized_indexes_are_replaced_by_an_explanation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path / "repo")
    index, packages, head = capture(repo)
    monkeypatch.setattr(write, "MAX_WRITE_INDEX_BYTES", 100)
    small = WriteIndex.model_validate_json(dump_write_index(build_write_index(repo, index, packages, head)))
    assert small.unavailable and "write-check limit" in small.unavailable and not small.symbols
    monkeypatch.setattr(write, "MAX_WRITE_PATHS", 1)
    many = build_write_index(repo, index, packages, head)
    assert many.unavailable and "source files" in many.unavailable


def test_skeleton_never_leaves_its_root(tmp_path: Path) -> None:
    hostile = WriteIndex(
        source_paths=["ok/a.py", "../escape.py", "/abs.py", "C:/drive.py", ".git/config", "a/../../b.py", "x\0y"],
        metadata={"pyproject.toml": "[project]\nname='x'\n", "../../evil.toml": "x"},
    )
    root = tmp_path / "tree"
    materialize(hostile, root)
    created = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert created == ["tree/ok/a.py", "tree/pyproject.toml"]
    assert (root / "ok/a.py").read_text() == ""  # Placeholders only; no source text is stored.


def test_examples_ship_loadable_indexes_pinned_to_their_revision() -> None:
    manifest = json.loads((EXAMPLES / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        index = load_write_index(EXAMPLES / "indexes" / item["file"])
        assert index is not None and index.unavailable is None and index.symbols, item["file"]
        if item.get("sha"):
            assert index.head_sha == item["sha"]


def test_check_reports_ok_renamed_missing_and_default_on_the_baked_example(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        text = (
            "# Run it\n\n`serve` accepts `--port` (default 8000) and `--host`.\n"
            "The `--workers` default is `4`.\nSet `APP_DEBUG` or `NOT_A_REAL_VARIABLE` for logs.\n"
        )
        response = check(client, "example-py-cli", text)
        assert response.status_code == 200, response.text
        body = response.json()
        found = by_claim(body)
        port = found["--port"]
        assert port["verdict"] == "renamed" and port["suggestion"] == "--listen"
        assert port["edit"] == {
            "line": 3,
            "column": 18,
            "old_text": "--port",
            "new_text": "--listen",
            "reason": "Replace a confirmed renamed identifier.",
            "certain": True,
        }
        assert any("saved scan" in reason for reason in port["reasons"])
        assert port["evidence"][0]["file"] == "cli.py"
        assert found["--host"]["verdict"] == "ok" and found["--host"]["edit"] is None
        assert found["NOT_A_REAL_VARIABLE"]["verdict"] == "missing" and found["NOT_A_REAL_VARIABLE"]["edit"] is None
        debug = found["APP_DEBUG"]
        assert debug["verdict"] == "missing" and debug["suggestion"] == "APP_LOG_LEVEL"
        assert debug["edit"]["certain"] is False  # The rule fixer is not certain; the writer decides.
        workers = next(f for f in body["findings"] if f["verdict"] == "default_changed")
        assert workers["suggestion"] == "8" and workers["edit"]["new_text"] == "8"
        assert body["path"] == "docs/new.md" and body["head_sha"].startswith("3fb52f1")
        assert "Git history is not available" in body["note"]
        assert body["findings"][0]["is_drift"] and body["findings"][-1]["verdict"] == "ok"

        rich = by_claim(check(client, "example-rich-cli", "Add the `--line-number` switch.\n").json())
        candidate = rich["--line-number"]
        # One near-miss declared flag is strong evidence: the rule now suggests it directly.
        assert candidate["verdict"] == "missing" and candidate["suggestion"] == "--line-numbers"
        assert candidate["edit"]["new_text"] == "--line-numbers"

        assert check(client, "example-py-cli", "Nothing to check here.\n").json()["findings"] == []
        meta = client.get("/api/write/repos/example-py-cli").json()
        assert meta["available"] and "`--port`" in meta["sample"]


def test_check_limits_origin_and_validation(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        assert check(client, "example-py-cli", "x" * (MAX_WRITE_TEXT_BYTES + 1)).status_code == 422
        wide = check(client, "example-py-cli", "é" * (MAX_WRITE_TEXT_BYTES // 2 + 1))
        assert wide.status_code == 413 and "20 KB" in wide.json()["detail"]
        huge = client.post(
            "/api/write/check",
            content=json.dumps({"scan_id": "example-py-cli", "text": "x" * 70_000}),
            headers={"content-type": "application/json", **ORIGIN},
        )
        assert huge.status_code == 413
        foreign = client.post(
            "/api/write/check", json={"scan_id": "example-py-cli", "text": "x"}, headers={"origin": "https://evil.test"}
        )
        assert foreign.status_code == 403
        for path in ("../outside.md", "/etc/passwd.md", "docs/new.py", ".git/x.md", "a/../../b.md"):
            assert check(client, "example-py-cli", "`--port`", path=path).status_code == 422, path
        assert check(client, "example-py-cli", "`--port`", path="README.md").status_code == 200
        assert check(client, "missing-scan", "`--port`").status_code == 404
        assert check(client, "../../etc", "`--port`").status_code == 422
        extra = client.post(
            "/api/write/check", json={"scan_id": "example-py-cli", "text": "x", "repo": "/"}, headers=ORIGIN
        )
        assert extra.status_code == 422
        hostile = check(client, "example-py-cli", "Use `<script>alert(1)</script>` and `--port`.\n").json()
        assert all("<script>" not in f["claim"] or f["verdict"] for f in hostile["findings"])


def test_check_rate_limit_uses_its_own_budget(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", write_rate_limit=3))
    with TestClient(app) as client:
        statuses = [check(client, "example-py-cli", "`--host`").status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        limited = check(client, "example-py-cli", "`--host`")
        assert limited.headers["retry-after"] == "60" and "text is kept" in limited.json()["detail"]
        # Scans keep their separate allowance.
        scan = client.post("/api/scans", json={"repo_url": "https://github.com/a/b"}, headers=ORIGIN)
        assert scan.status_code != 429


def test_live_scan_index_is_kept_after_the_workspace_is_deleted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    index, packages, head = capture(repo)
    result = public_result(run_scan(repo, Config(), history=False), "https://github.com/acme/tool")
    seen: list[Path] = []

    async def runner(job: Job, workspace: Path) -> None:
        seen.append(workspace)
        if job.repo_url.endswith("/tool"):
            index_file = workspace / WRITE_INDEX_FILE
            index_file.write_text(dump_write_index(build_write_index(repo, index, packages, head)), encoding="utf-8")
        await asyncio.sleep(0)
        job.result = result

    app = create_app(Settings(data_dir=tmp_path / "data"), test_runner=runner)
    with TestClient(app) as client:
        ident = client.post("/api/scans", json={"repo_url": "https://github.com/acme/tool"}, headers=ORIGIN).json()[
            "scan_id"
        ]
        old = client.post("/api/scans", json={"repo_url": "https://github.com/acme/old"}, headers=ORIGIN).json()[
            "scan_id"
        ]
        for _ in range(500):
            states = [client.get(f"/api/scans/{i}").json()["status"] for i in (ident, old)]
            if all(state == "done" for state in states) and all(not path.exists() for path in seen):
                break
            time.sleep(0.01)
        assert states == ["done", "done"] and len(seen) == 2
        assert all(not path.exists() for path in seen)  # Workspaces are gone ...
        meta = client.get(f"/api/write/repos/{ident}").json()
        assert meta["available"] and meta["name"] == "acme/tool"  # ... but the index survived.
        body = check(client, ident, "Run `tool serve --listen 9000`; `serve` accepts `--port`.\n").json()
        found = by_claim(body)
        assert found["--port"]["verdict"] == "missing" and found["--port"]["is_drift"]
        assert body["head_sha"] == head
        # A scan without a saved index explains how to enable write checks instead of guessing.
        missing = client.get(f"/api/write/repos/{old}").json()
        assert not missing["available"] and "Scan the repository again" in missing["reason"]
        assert check(client, old, "`--port`").status_code == 409


def test_index_files_expire_with_their_results(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = Store(settings)
    path = store.index_path("abc")
    assert path is not None and store.index_path("../x") is None
    path.write_text("{}", encoding="utf-8")
    store.expire()
    assert not path.exists()


def test_worker_execute_keeps_the_index_beside_the_result(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    target = tmp_path / WRITE_INDEX_FILE
    execute(repo, Settings(), lambda *_: None, keep_index=target)
    saved = load_write_index(target)
    assert saved is not None and any(symbol.name == "--listen" for symbol in saved.symbols)


def test_write_page_navigation_and_csp(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        page = client.get("/write")
        assert page.status_code == 200
        html = page.text
        assert '<label for="write-text"' in html and 'aria-live="polite"' in html
        assert '<script src="/static/write.js" defer></script>' in html and "<script>" not in html
        assert "Wispr Flow" in html and "Ctrl</kbd> + <kbd>Enter" in html
        assert 'value="example-py-cli" selected' in html
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/static/write.js").status_code == 200
        assert 'href="/write"' in client.get("/").text
        voice = client.get("/built-with-voice").text
        assert 'href="/write"' in voice and "how you use it" in voice
        assert "write.js" not in client.get("/case-study").text


@pytest.mark.parametrize(
    ("dictated", "expected"),
    [
        ("Run serve --port 9000.", "Run serve `--port` 9000."),
        ("Set APP_DEBUG to 1.", "Set `APP_DEBUG` to 1."),
        ("Already `--port` and `APP_DEBUG`.", "Already `--port` and `APP_DEBUG`."),
        ("```sh\nserve --port 9000\n```\nUse --port.", "```sh\nserve --port 9000\n```\nUse `--port`."),
        ("See https://example.com/--x and a-b--c.", "See https://example.com/--x and a-b--c."),
        ("NOTE: HTTP and CI are words, A_B is a name.", "NOTE: HTTP and CI are words, `A_B` is a name."),
    ],
)
def test_dictation_marks_bare_flags_and_env_names_as_code(dictated: str, expected: str) -> None:
    assert normalize_dictation(dictated) == expected
    assert normalize_dictation(dictated).count("\n") == dictated.count("\n")
