"""MCP server behavior through the SDK's in-memory client against synthetic fixtures."""

import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

import pytest
from typer.testing import CliRunner

pytest.importorskip("mcp.server.mcpserver")

import anyio
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, TextResourceContents

from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.errors import DocsDriftError
from docs_drift.fix.edits import StaleDocumentError, render_file
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.mcp_server import create_server
from docs_drift.mcp_service import DriftService
from tests.fixtures.build_fixtures import Repository, _build_repo, _commit, _py_cli

_T = TypeVar("_T")
STALE_SNIPPET = "# Serving\n\nStart it with `serve`, which accepts `--port`.\n"
CORRECT_SNIPPET = "# Serving\n\nStart it with `serve`, which accepts `--listen`.\n"


@pytest.fixture(scope="module")
def shared_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("mcp-shared")
    _build_repo(root, _py_cli())
    return root / "py-cli"


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    _build_repo(tmp_path, _py_cli())
    return tmp_path / "py-cli"


def with_client(server: MCPServer[Any], body: Callable[[Client], Awaitable[_T]]) -> _T:
    async def main() -> _T:
        async with Client(server) as client:
            return await body(client)

    return anyio.run(main)


def call(server: MCPServer[Any], tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
    return with_client(server, lambda client: client.call_tool(tool, arguments or {}))


def structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, error_text(result)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


def error_text(result: CallToolResult) -> str:
    return " ".join(item.text for item in result.content if isinstance(item, TextContent))


def by_claim(payload: dict[str, Any], claim: str) -> dict[str, Any]:
    return next(f for f in payload["findings"] if f["claim"] == claim)


def test_tools_are_listed_with_schemas_and_safety_annotations(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    tools = {tool.name: tool for tool in with_client(server, lambda client: client.list_tools()).tools}
    assert set(tools) == {
        "check_docs",
        "check_document",
        "check_text",
        "explain_finding",
        "preview_fix",
        "apply_fix",
        "coverage",
        "supported_surfaces",
    }
    for name, tool in tools.items():
        assert tool.annotations is not None and tool.description and tool.output_schema, name
        if name == "apply_fix":
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is True
        else:
            assert tool.annotations.read_only_hint is True, name
    assert tools["check_text"].input_schema["required"] == ["path", "text"]
    assert tools["check_docs"].input_schema["properties"]["max_findings"]["maximum"] == 200
    prompts = with_client(server, lambda client: client.list_prompts()).prompts
    assert {prompt.name for prompt in prompts} == {"review-docs-after-change", "write-docs-safely"}
    resources = with_client(server, lambda client: client.list_resources()).resources
    assert {str(resource.uri) for resource in resources} == {
        "docs-drift://report/latest",
        "docs-drift://supported-surfaces",
    }


def test_check_docs_reports_history_backed_drift_and_caches(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    events: list[tuple[float, float | None, str | None]] = []

    async def body(client: Client) -> tuple[CallToolResult, CallToolResult]:
        async def on_progress(progress: float, total: float | None, message: str | None) -> None:
            events.append((progress, total, message))

        first = await client.call_tool("check_docs", {}, progress_callback=on_progress)
        second = await client.call_tool("check_docs", {"min_confidence": 0})
        return first, second

    first, second = with_client(server, body)
    payload = structured(first)
    port = by_claim(payload, "--port")
    assert port["verdict"] == "renamed" and port["suggestion"] == "--listen"
    assert port["location"] == "README.md:4" and port["is_drift"]
    assert port["introduced_by"]["summary"] == "Rename port to listen"
    assert port["id"].startswith("claim_") and len(port["id"]) == 16
    assert any(e["file"] == "cli.py" for e in port["evidence"])
    assert by_claim(payload, "migrate")["suggestion"] == "db-upgrade"
    assert by_claim(payload, "--workers")["verdict"] == "default_changed"
    assert all(f["verdict"] != "ok" for f in payload["findings"])
    summary = payload["summary"]
    assert summary["drift"] == 4 and summary["by_verdict"]["ok"] == 5 and summary["docs_scanned"] == 1
    assert summary["hidden_below_confidence"] == 1  # APP_DEBUG is below the default 0.6 threshold.
    assert events and events[-1][0] == events[-1][1]
    again = structured(second)
    assert again["cached"] is True
    assert by_claim(again, "APP_DEBUG")["verdict"] == "missing"


def test_check_docs_filters_and_caps(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    capped = structured(call(server, "check_docs", {"max_findings": 1, "include_ok": True}))
    assert capped["summary"]["returned"] == 1 and capped["summary"]["truncated"] is True
    elsewhere = structured(call(server, "check_docs", {"paths": ["docs/**/*.md"]}))
    assert elsewhere["findings"] == [] and elsewhere["summary"]["docs_scanned"] == 0
    everything = structured(call(server, "check_docs", {"include_ok": True, "max_findings": 200}))
    assert {f["verdict"] for f in everything["findings"]} >= {"ok", "renamed", "default_changed"}
    invalid = call(server, "check_docs", {"max_findings": 0})
    assert invalid.is_error


def test_check_text_flags_unsaved_stale_snippet_without_writing(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    payload = structured(call(server, "check_text", {"path": "docs/serving.md", "text": STALE_SNIPPET}))
    assert payload["unsaved"] is True and payload["path"] == "docs/serving.md"
    finding = by_claim(payload, "--port")
    assert finding["verdict"] == "renamed" and finding["suggestion"] == "--listen"
    assert finding["location"] == "docs/serving.md:3"
    assert finding["doc_line"] == STALE_SNIPPET.splitlines()[2]
    assert [(e["old_text"], e["new_text"], e["line"]) for e in payload["suggested_edits"]] == [
        ("--port", "--listen", 3)
    ]
    assert not (shared_repo / "docs").exists()


def test_check_text_accepts_correct_snippet(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    payload = structured(call(server, "check_text", {"path": "README.md", "text": CORRECT_SNIPPET, "include_ok": True}))
    assert payload["summary"]["drift"] == 0 and payload["suggested_edits"] == []
    assert {f["claim"]: f["verdict"] for f in payload["findings"]} == {"--listen": "ok"}
    assert "--port" in (shared_repo / "README.md").read_text(encoding="utf-8")


def test_check_document_and_explain(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    document = structured(call(server, "check_document", {"path": "README.md"}))
    assert document["unsaved"] is False and document["summary"]["drift"] == 4
    assert ("--port", "--listen") in {(e["old_text"], e["new_text"]) for e in document["suggested_edits"]}
    finding_id = by_claim(document, "--port")["id"]
    explained = structured(call(server, "explain_finding", {"finding_id": finding_id}))
    assert explained["full_id"].startswith(finding_id) and len(explained["full_id"]) == 70
    # The id was issued by check_document, so it is explained exactly as that call returned it.
    assert explained["rule"] == "DD002" and explained["source"] == "document"
    assert explained["auto_fixable"] is True
    assert explained["matched_symbol"]["name"] == "--listen"
    assert any("Git history" in reason for reason in explained["reasons"])
    # Prefixes without the claim_ namespace are accepted too.
    short = structured(call(server, "explain_finding", {"finding_id": finding_id.removeprefix("claim_")}))
    assert short["full_id"] == explained["full_id"]


def test_explain_finds_unsaved_text_findings_and_rejects_unknown_ids(shared_repo: Path) -> None:
    server = create_server(shared_repo)

    async def body(client: Client) -> tuple[CallToolResult, CallToolResult, CallToolResult]:
        text = await client.call_tool("check_text", {"path": "docs/x.md", "text": STALE_SNIPPET})
        finding_id = structured(text)["findings"][0]["id"]
        explained = await client.call_tool("explain_finding", {"finding_id": finding_id})
        fix = await client.call_tool("preview_fix", {"finding_ids": [finding_id]})
        return explained, fix, await client.call_tool("explain_finding", {"finding_id": "claim_ffffffffff"})

    explained, fix, unknown = with_client(server, body)
    assert structured(explained)["source"] == "unsaved_text"
    assert fix.is_error and "suggested_edits" in error_text(fix)
    assert unknown.is_error and "check_docs again" in error_text(unknown)
    malformed = call(server, "explain_finding", {"finding_id": "zz"})
    assert malformed.is_error and "not a finding id" in error_text(malformed)


def test_preview_fix_returns_diff_and_never_writes(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    before = (shared_repo / "README.md").read_bytes()
    findings = structured(call(server, "check_docs", {"min_confidence": 0}))
    ids = [by_claim(findings, "--port")["id"], by_claim(findings, "APP_DEBUG")["id"]]
    preview = structured(call(server, "preview_fix", {"finding_ids": ids}))
    assert preview["applied"] is False and preview["files"] == ["README.md"]
    assert "--- a/README.md" in preview["diff"]
    assert "-- `serve` accepts `--port` (default 8000)." in preview["diff"]
    assert "+- `serve` accepts `--listen` (default 8000)." in preview["diff"]
    assert len(preview["not_auto_fixable"]) == 1 and "README.md:7" in preview["not_auto_fixable"][0]
    assert (shared_repo / "README.md").read_bytes() == before


def test_apply_fix_writes_and_refreshes_results(fresh_repo: Path) -> None:
    server = create_server(fresh_repo)

    async def body(client: Client) -> tuple[dict[str, Any], dict[str, Any]]:
        findings = structured(await client.call_tool("check_docs", {}))
        finding_id = by_claim(findings, "--port")["id"]
        applied = structured(await client.call_tool("apply_fix", {"finding_ids": [finding_id]}))
        return applied, structured(await client.call_tool("check_docs", {}))

    applied, after = with_client(server, body)
    assert applied["applied"] is True and applied["files"] == ["README.md"]
    readme = (fresh_repo / "README.md").read_text(encoding="utf-8")
    assert "`--listen` (default 8000)" in readme and "--port" not in readme
    assert after["cached"] is False
    assert "--port" not in {f["claim"] for f in after["findings"]}


def test_paths_outside_the_root_are_rejected(shared_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("Use `--port`.\n", encoding="utf-8")
    server = create_server(shared_repo)
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("check_document", {"path": "../secret.md"}, "outside the repository root"),
        ("check_document", {"path": str(outside)}, "outside the repository root"),
        ("check_text", {"path": "../../x.md", "text": "hi"}, "outside the repository root"),
        ("check_document", {"path": "cli.py"}, "not a documentation file"),
        ("check_document", {"path": ".git/description.md"}, "internal metadata"),
        ("check_document", {"path": "missing.md"}, "not a regular file"),
        ("check_docs", {"repo": str(tmp_path)}, "outside the allowed roots"),
        ("coverage", {"important": ["../x.md"]}, "outside the repository root"),
    ]
    for tool, arguments, expected in cases:
        result = call(server, tool, arguments)
        assert result.is_error, (tool, arguments)
        assert expected in error_text(result), error_text(result)
        assert "Traceback" not in error_text(result)


def test_symlink_escape_is_rejected(shared_repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_text("Use `--port`.\n", encoding="utf-8")
    link = shared_repo / "linked.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted here.")
    try:
        result = call(create_server(shared_repo), "check_document", {"path": "linked.md"})
        assert result.is_error and "outside the repository root" in error_text(result)
    finally:
        link.unlink()


def test_extra_roots_are_allowed(shared_repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "README.md").write_text("Run `make build`.\n", encoding="utf-8")
    server = create_server(shared_repo, extra_roots=[other])
    payload = structured(call(server, "check_docs", {"repo": str(other), "include_unverifiable": True}))
    assert payload["repo"] == str(other.resolve())


def test_slow_scans_time_out_with_actionable_error(shared_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(self: DriftService, **_: object) -> object:
        time.sleep(0.5)
        raise AssertionError("abandoned")

    monkeypatch.setattr(DriftService, "full_scan", slow)
    result = call(create_server(shared_repo, timeout=0.05), "check_docs")
    assert result.is_error and "did not finish within" in error_text(result) and "history=false" in error_text(result)


def test_surfaces_coverage_resources_and_prompts(shared_repo: Path) -> None:
    server = create_server(shared_repo)
    surfaces = structured(call(server, "supported_surfaces"))
    assert "cli_flag" in surfaces["claim_kinds"] and "renamed" in surfaces["verdicts"]
    coverage = structured(call(server, "coverage", {"important": ["docs/guide.md"]}))
    statuses = {row["path"]: row["status"] for row in coverage["documents"]}
    assert statuses == {"README.md": "checked", "docs/guide.md": "not-checked"}

    async def body(client: Client) -> tuple[str, str, str]:
        report = await client.read_resource("docs-drift://report/latest")
        table = await client.read_resource("docs-drift://supported-surfaces")
        prompt = await client.get_prompt("review-docs-after-change", {"change": "renamed --port to --listen"})
        texts = [c.text for c in [*report.contents, *table.contents] if isinstance(c, TextResourceContents)]
        message = prompt.messages[0].content
        assert isinstance(message, TextContent)
        return texts[0], texts[1], message.text

    report, table, prompt = with_client(server, body)
    assert '"suggestion": "--listen"' in report
    assert "| Python | argparse | partial |" in table
    assert "renamed --port to --listen" in prompt and "apply_fix" in prompt


def test_missing_extra_has_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in [key for key in sys.modules if key == "mcp" or key.startswith("mcp.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.delitem(sys.modules, "docs_drift.mcp_server")
    with pytest.raises(DocsDriftError) as caught:
        importlib.import_module("docs_drift.mcp_server")
    assert caught.value.hint is not None and "docs-drift[mcp]" in caught.value.hint
    result = CliRunner().invoke(app, ["mcp", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "MCP support is not installed" in result.output and "docs-drift[mcp]" in result.output


# Regression tests for reviewed defects -------------------------------------------------------


def _port_id(server: MCPServer[Any]) -> str:
    return str(by_claim(structured(call(server, "check_docs")), "--port")["id"])


def test_apply_fix_timeout_never_writes_later(fresh_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finding_id = _port_id(create_server(fresh_repo))
    server = create_server(fresh_repo, timeout=0.3)
    before = (fresh_repo / "README.md").read_bytes()
    original = DriftService._plan

    def slow_plan(self: DriftService, *args: Any, **kwargs: Any) -> Any:
        time.sleep(0.8)  # The deadline passes while planning.
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DriftService, "_plan", slow_plan)
    result = call(server, "apply_fix", {"finding_ids": [finding_id]})
    # The worker was not abandoned: by the time the call returns, its outcome is final.
    assert result.is_error and "nothing was written" in error_text(result)
    time.sleep(0.3)
    assert (fresh_repo / "README.md").read_bytes() == before


def test_apply_fix_with_plan_token_refuses_changed_documents(fresh_repo: Path) -> None:
    server = create_server(fresh_repo)
    finding_id = _port_id(server)
    preview = structured(call(server, "preview_fix", {"finding_ids": [finding_id]}))
    assert preview["plan_token"].startswith("plan_")
    readme = fresh_repo / "README.md"
    changed = "Intro line added after the preview.\n" + readme.read_text(encoding="utf-8")
    readme.write_text(changed, encoding="utf-8", newline="\n")
    refused = call(server, "apply_fix", {"finding_ids": [finding_id], "plan_token": preview["plan_token"]})
    assert refused.is_error and "nothing was written" in error_text(refused)
    assert readme.read_text(encoding="utf-8") == changed
    fresh = structured(call(server, "preview_fix", {"finding_ids": [finding_id]}))
    assert fresh["plan_token"] != preview["plan_token"]
    applied = structured(call(server, "apply_fix", {"finding_ids": [finding_id], "plan_token": fresh["plan_token"]}))
    assert applied["applied"] is True and applied["written"] == ["README.md"] and applied["skipped"] == []
    assert "`--listen` (default 8000)" in readme.read_text(encoding="utf-8")


def test_apply_fix_fails_closed_on_stale_plan_unless_opted_in(
    fresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = create_server(fresh_repo)
    finding_id = _port_id(server)
    before = (fresh_repo / "README.md").read_bytes()
    real = plan_rule_fixes
    calls: list[int] = []

    def stale_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise StaleDocumentError("README.md changed since scan; scan again.")
        return real(*args, **kwargs)

    monkeypatch.setattr("docs_drift.mcp_service.plan_rule_fixes", stale_once)
    refused = call(server, "apply_fix", {"finding_ids": [finding_id]})
    assert refused.is_error and "nothing was written" in error_text(refused)
    assert (fresh_repo / "README.md").read_bytes() == before
    calls.clear()
    applied = structured(call(server, "apply_fix", {"finding_ids": [finding_id], "rescan_if_stale": True}))
    assert applied["written"] == ["README.md"]


def test_multi_file_apply_verifies_every_file_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _py_cli()
    first = replace(
        spec.commits[0], files=(*spec.commits[0].files, ("docs/guide.md", "# Guide\n\nStart `serve` with `--port`.\n"))
    )
    _build_repo(tmp_path, replace(spec, name="py-multi", commits=(first, *spec.commits[1:])))
    repo = tmp_path / "py-multi"
    server = create_server(repo)
    findings = structured(call(server, "check_docs"))
    ids = [f["id"] for f in findings["findings"] if f["claim"] == "--port"]
    preview = structured(call(server, "preview_fix", {"finding_ids": ids}))
    assert preview["files"] == ["README.md", "docs/guide.md"]
    before = {name: (repo / name).read_bytes() for name in preview["files"]}
    real = render_file

    def guide_is_stale(root: Path, plan: Any, relative: str) -> Any:
        if relative == "docs/guide.md":
            raise StaleDocumentError("docs/guide.md changed since the scan; scan again before applying edits.")
        return real(root, plan, relative)

    monkeypatch.setattr("docs_drift.mcp_service.render_file", guide_is_stale)
    refused = call(server, "apply_fix", {"finding_ids": ids, "plan_token": preview["plan_token"]})
    assert refused.is_error and "Nothing was written" in error_text(refused)
    assert {name: (repo / name).read_bytes() for name in before} == before
    monkeypatch.setattr("docs_drift.mcp_service.render_file", real)
    applied = structured(call(server, "apply_fix", {"finding_ids": ids, "plan_token": preview["plan_token"]}))
    assert applied["written"] == ["README.md", "docs/guide.md"]


def test_timed_out_scan_stops_and_reports_busy(shared_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import docs_drift.mcp_service as service_module  # noqa: PLC0415 - patch the scanner used by the service.

    state = {"finished": 0, "started": 0}

    def slow_scan(*_: Any, stage_callback: Callable[[str, dict[str, int]], None], **__: Any) -> Any:
        state["started"] += 1
        time.sleep(0.7)
        stage_callback("indexing", {})  # A cancelled ticket raises here and releases the lock.
        state["finished"] += 1
        raise AssertionError("cancelled scans must stop at the checkpoint")

    monkeypatch.setattr(service_module, "run_scan", slow_scan)
    server = create_server(shared_repo, timeout=0.25)

    async def body(client: Client) -> tuple[CallToolResult, CallToolResult]:
        first = await client.call_tool("check_docs", {"history": False})
        second = await client.call_tool("check_docs", {"history": False})
        await anyio.sleep(0.9)
        return first, second

    first, second = with_client(server, body)
    assert first.is_error and "did not finish within" in error_text(first)
    assert second.is_error and "still running" in error_text(second)
    assert state == {"started": 1, "finished": 0}


def test_untracked_files_invalidate_cached_scan(tmp_path: Path) -> None:
    readme = "# Assets\n\nThe logo lives at `assets/logo.svg`.\n"
    _build_repo(tmp_path, Repository("paths", (_commit(1, "Document assets", ("README.md", readme)),)))
    repo = tmp_path / "paths"
    server = create_server(repo)
    first = structured(call(server, "check_docs", {"history": False, "min_confidence": 0}))
    assert by_claim(first, "assets/logo.svg")["verdict"] == "missing"
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.svg").write_text("<svg/>\n", encoding="utf-8")
    second = structured(call(server, "check_docs", {"history": False, "min_confidence": 0}))
    assert second["cached"] is False
    assert "assets/logo.svg" not in {f["claim"] for f in second["findings"]}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("guide.RST", "rst"), ("notes.txt", "text"), ("notes.TEXT", "text"), ("page.MDX", "mdx"), ("a.md", "markdown")],
)
def test_in_memory_documents_use_discovery_formats(shared_repo: Path, path: str, expected: str) -> None:
    result = scan_claims(shared_repo, Config(), doc_paths={path}, contents={path: "Hello.\n"})
    assert [doc.format.value for doc in result.docs] == [expected]
    _, relative = DriftService(shared_repo).document_path(path, must_exist=False)
    assert relative == path


def test_saved_document_outside_globs_can_be_fixed(fresh_repo: Path) -> None:
    (fresh_repo / ".docs-drift.toml").write_text('docs = ["README.md"]\n', encoding="utf-8")
    (fresh_repo / "docs").mkdir()
    extra = fresh_repo / "docs" / "extra.md"
    extra.write_text(STALE_SNIPPET, encoding="utf-8", newline="\n")
    server = create_server(fresh_repo)
    document = structured(call(server, "check_document", {"path": "docs/extra.md"}))
    finding_id = by_claim(document, "--port")["id"]
    explained = structured(call(server, "explain_finding", {"finding_id": finding_id}))
    assert explained["source"] == "document" and explained["auto_fixable"] is True
    preview = structured(call(server, "preview_fix", {"finding_ids": [finding_id]}))
    assert (
        preview["files"] == ["docs/extra.md"] and "+Start it with `serve`, which accepts `--listen`." in preview["diff"]
    )
    applied = structured(call(server, "apply_fix", {"finding_ids": [finding_id], "plan_token": preview["plan_token"]}))
    assert applied["written"] == ["docs/extra.md"]
    assert "`--listen`" in extra.read_text(encoding="utf-8")


def test_explain_uses_the_call_that_issued_the_id(shared_repo: Path) -> None:
    server = create_server(shared_repo)

    async def body(client: Client) -> dict[str, Any]:
        await client.call_tool("check_docs", {})
        text = structured(await client.call_tool("check_text", {"path": "README.md", "text": STALE_SNIPPET}))
        finding_id = by_claim(text, "--port")["id"]
        return structured(await client.call_tool("explain_finding", {"finding_id": finding_id}))

    explained = with_client(server, body)
    # Same claim id as README.md:4 on disk, but the explanation matches the unsaved text that issued it.
    assert explained["source"] == "unsaved_text" and explained["finding"]["location"] == "README.md:3"


def test_claude_project_dir_is_the_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import docs_drift.mcp_server as server_module  # noqa: PLC0415 - patch the transport entry point.

    seen: list[Path] = []
    monkeypatch.setattr(server_module, "serve", lambda repo, **_: seen.append(repo))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert CliRunner().invoke(app, ["mcp"]).exit_code == 0
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert CliRunner().invoke(app, ["mcp", "--repo", str(explicit)]).exit_code == 0
    assert seen == [project.resolve(), explicit.resolve()]
