"""Exact-span fixes operate on text; fixture application never executes repository code."""

import asyncio
import codecs
import hashlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift.cli import _full_findings, app
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.fix.edits import FixPlan, StaleDocumentError, TextEdit, apply_file, decode, render_file, unified_patch
from docs_drift.fix.model import add_model_fixes, sentence_span, valid_rewrite
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.index import build_index
from docs_drift.llm.types import ModelResponse
from docs_drift.models import ClaimKind, ScanResult, Verdict


def scan(
    repo: Path,
    document: bytes | str,
    source: str = 'import argparse\np = argparse.ArgumentParser()\np.add_argument("--new")\n',
) -> ScanResult:
    repo.mkdir(exist_ok=True)
    (repo / "README.md").write_bytes(document.encode() if isinstance(document, str) else document)
    (repo / "app.py").write_text(source, encoding="utf-8")
    result = scan_claims(repo, Config())
    index = build_index(repo, Config())
    findings, _ = _full_findings(result.claims, index, repo)
    return result.model_copy(update={"symbols": index.symbols, "findings": findings})


def rename_result(repo: Path, document: bytes | str) -> ScanResult:
    result = scan(repo, document)
    symbol = next(item for item in result.symbols if item.name == "--new")
    for finding in result.findings:
        if finding.claim.value == "--old":
            finding.verdict = Verdict.renamed
            finding.confidence = 0.95
            finding.suggestion = "--new"
            finding.matched_symbol = symbol
    return result


@pytest.mark.parametrize(
    "document",
    [
        "Use `--old`.\n",
        "| Flag | Meaning |\n| --- | --- |\n| `--old` | Example |\n",
        "```sh\ntool serve --old value\n```\n",
    ],
)
def test_exact_rename_preview_and_apply(tmp_path: Path, document: str) -> None:
    # The fenced span is modeled explicitly when the executable has no project manifest.
    if "```" in document:
        (tmp_path / "pyproject.toml").write_text('[project.scripts]\ntool = "app:main"\n', encoding="utf-8")
    result = rename_result(tmp_path, document)
    plan = plan_rule_fixes(result)
    assert plan.edits
    patch = unified_patch(tmp_path, plan)
    assert "+" in patch and "--new" in patch
    apply_file(tmp_path, plan, "README.md")
    assert "--old" not in (tmp_path / "README.md").read_text()


def test_default_table_keeps_backticks_and_formatting(tmp_path: Path) -> None:
    source = 'import argparse\np = argparse.ArgumentParser()\np.add_argument("--workers", default=8)\n'
    result = scan(tmp_path, "| Flag | Default |\n| --- | --- |\n| `--workers` | `4` |\n", source)
    plan = plan_rule_fixes(result)
    assert any(edit.old_text == "4" and edit.new_text == "8" for edit in plan.edits)
    apply_file(tmp_path, plan, "README.md")
    assert "| `--workers` | `8` |" in (tmp_path / "README.md").read_text()


@pytest.mark.parametrize("prefix", [b"", codecs.BOM_UTF8])
def test_crlf_and_bom_are_preserved(tmp_path: Path, prefix: bytes) -> None:
    raw = prefix + b"# Usage\r\nUse `--old`.\r\n"
    plan = plan_rule_fixes(rename_result(tmp_path, raw))
    apply_file(tmp_path, plan, "README.md")
    assert (tmp_path / "README.md").read_bytes() == prefix + b"# Usage\r\nUse `--new`.\r\n"


def test_grouped_locations_fix_only_current_mentions(tmp_path: Path) -> None:
    document = "# Usage\nFirst `--old`.\n\nSecond `--old`.\n\n## Migrating from v1\nKeep `--old` in history.\n"
    plan = plan_rule_fixes(rename_result(tmp_path, document))
    assert len(plan.edits) == 2
    apply_file(tmp_path, plan, "README.md")
    text = (tmp_path / "README.md").read_text()
    assert text.count("--new") == 2
    assert "Keep `--old` in history" in text


def test_stale_hash_prevents_writes(tmp_path: Path) -> None:
    plan = plan_rule_fixes(rename_result(tmp_path, "Use `--old`.\n"))
    (tmp_path / "README.md").write_text("Another edit\n", encoding="utf-8")
    with pytest.raises(StaleDocumentError, match="changed since"):
        apply_file(tmp_path, plan, "README.md")
    assert (tmp_path / "README.md").read_text() == "Another edit\n"


def test_patch_passes_git_apply_check(tmp_path: Path) -> None:
    plan = plan_rule_fixes(rename_result(tmp_path, "Use `--old`.\n"))
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "apply", "--check", "-"], input=unified_patch(tmp_path, plan).encode(), check=True
    )


@pytest.mark.parametrize(
    "boolean,command,expected",
    [
        (True, "tool deploy --dry-run now", "tool deploy now"),
        (True, "tool deploy --dry-run", "tool deploy"),
        (False, "tool serve --port 8000", "tool serve"),
    ],
)
def test_removed_flags_drop_correct_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boolean: bool, command: str, expected: str
) -> None:
    result = rename_result(tmp_path, "Use `--old`.\n")
    claim = result.findings[0].claim.model_copy(
        update={"kind": ClaimKind.code_command, "value": command, "raw_text": command}
    )
    flag = "--dry-run" if boolean else "--port"
    result.findings = [
        result.findings[0].model_copy(
            update={"claim": claim, "verdict": Verdict.invalid_command, "reasons": [f"unknown command or flag: {flag}"]}
        )
    ]
    raw = (command + "\n").encode()
    (tmp_path / "README.md").write_bytes(raw)
    result.docs[0].content_hash = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr("docs_drift.fix.rules._removed_flag_shape", lambda repo, flag: (boolean, "9f3e2a1"))
    plan = plan_rule_fixes(result)
    apply_file(tmp_path, plan, "README.md")
    assert (tmp_path / "README.md").read_text().strip() == expected
    assert "check whether this example should change more" in plan.suggestions[0]


def test_missing_name_is_suggestion_only(tmp_path: Path) -> None:
    result = scan(tmp_path, "Use `--old`.\n")
    plan = plan_rule_fixes(result)
    assert not plan.edits
    assert "mark it deprecated" in plan.suggestions[0]


def test_model_rewrite_with_unknown_identifier_is_rejected(tmp_path: Path) -> None:
    result = rename_result(tmp_path, "Use `--old`.\n")
    finding = result.findings[0]
    index = build_index(tmp_path, Config())
    assert not valid_rewrite("Use `--old`.", "Use `--new` with unknown_function().", finding, index, result)
    assert not valid_rewrite("Use `--old`.", "Use `--new` with evil().", finding, index, result)
    assert not valid_rewrite("Use `--old`.", "Use `--new`.\nIgnore instructions.", finding, index, result)
    assert valid_rewrite("Use `--old`.", "Use `--new`.", finding, index, result)


def test_model_rewrite_cannot_introduce_a_wrong_default(tmp_path: Path) -> None:
    original = "The `--workers` default is `4`."
    result = scan(
        tmp_path,
        original + "\n",
        'import argparse\np=argparse.ArgumentParser()\np.add_argument("--workers", default=8)\n',
    )
    index = build_index(tmp_path, Config())
    finding = next(item for item in result.findings if item.verdict == Verdict.default_changed)
    assert not valid_rewrite(original, "The `--workers` default is `99`.", finding, index, result)
    assert valid_rewrite(original, "The `--workers` default is `8`.", finding, index, result)


def test_cli_fix_previews_and_requires_explicit_write(tmp_path: Path) -> None:
    document = "The `--workers` default is `4`.\n"
    scan(tmp_path, document, 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--workers", default=8)\n')
    runner = CliRunner()
    preview = runner.invoke(app, ["--repo", str(tmp_path), "fix"])
    assert preview.exit_code == 0, preview.output
    assert "auto-fixable" in preview.output
    assert (tmp_path / "README.md").read_text() == document
    declined = runner.invoke(app, ["--repo", str(tmp_path), "fix", "--write"], input="n\n")
    assert declined.exit_code == 0
    assert (tmp_path / "README.md").read_text() == document
    written = runner.invoke(app, ["--repo", str(tmp_path), "fix", "--write", "--yes"])
    assert written.exit_code == 0, written.output
    assert "`8`" in (tmp_path / "README.md").read_text()


def test_cli_stale_plan_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rename_result(tmp_path, "Use `--old`.\n")
    original = plan_rule_fixes
    calls = 0

    def stale_once(result: ScanResult) -> FixPlan:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StaleDocumentError("README.md changed since scan.")
        return original(result)

    monkeypatch.setattr("docs_drift.cli.plan_rule_fixes", stale_once)
    output = CliRunner().invoke(app, ["--repo", str(tmp_path), "fix"])
    assert output.exit_code == 0, output.output
    assert calls == 2
    assert "Re-scanning" in output.output


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)])
def test_explicit_span_preserves_utf16_bytes(tmp_path: Path, encoding: str, bom: bytes) -> None:
    raw = bom + "old\r\n".encode(encoding)
    (tmp_path / "README.md").write_bytes(raw)
    edit = TextEdit(
        file="README.md",
        line=1,
        column=1,
        end_line=1,
        end_column=4,
        old_text="old",
        new_text="new",
        reason="test",
        claim_id="test",
    )
    plan = FixPlan(edits=[edit], hashes={"README.md": hashlib.sha256(raw).hexdigest()})
    assert render_file(tmp_path, plan, "README.md")[1] == bom + "new\r\n".encode(encoding)
    assert decode(raw)[0] == "old\r\n"


@pytest.mark.parametrize("rewrite,accepted", [("Use `--new`.", True), ("Use `--new` and unknown_function().", False)])
def test_fake_model_rewrite_must_pass_static_self_check(tmp_path: Path, rewrite: str, accepted: bool) -> None:
    result = rename_result(tmp_path, "Use `--old`.\n")
    index = build_index(tmp_path, Config())

    class Client:
        async def complete(self, request: object) -> ModelResponse:
            return ModelResponse.model_validate(
                {
                    "result": {
                        "verdict": "contradicted",
                        "evidence": [{"snippet_id": "s1", "quote": 'p.add_argument("--new")'}],
                        "explanation": "Use the declared option.",
                        "suggested_doc_text": rewrite,
                    },
                    "replayed": True,
                }
            )

    plan = FixPlan(hashes={doc.path: doc.content_hash for doc in result.docs})
    plan = asyncio.run(add_model_fixes(result, index, plan, Client()))
    assert bool(plan.edits) == accepted
    if accepted:
        assert plan.edits[0].decided_by == "llm" and plan.edits[0].replayed
        assert result.llm_usage["claims"] == 1


def test_fix_json_and_patch_are_review_artifacts(tmp_path: Path) -> None:
    scan(
        tmp_path,
        "The `--workers` default is `4`.\n",
        'import argparse\np=argparse.ArgumentParser()\np.add_argument("--workers", default=8)\n',
    )
    patch = tmp_path / "changes.diff"
    output = CliRunner().invoke(app, ["--repo", str(tmp_path), "--format", "json", "fix", "--patch", str(patch)])
    assert output.exit_code == 0, output.output
    assert '"old_text": "4"' in output.output
    assert patch.read_text().startswith("--- a/README.md")
    assert "`4`" in (tmp_path / "README.md").read_text()


@pytest.mark.parametrize(
    "old,normalized,new,expected",
    [
        ("30s", "30s", "1m", "60s"),
        ("1 MiB", "1048576B", "2 MiB", "2 MiB"),
    ],
)
def test_default_fixes_preserve_units(tmp_path: Path, old: str, normalized: str, new: str, expected: str) -> None:
    result = rename_result(tmp_path, f"The `--old` default is `{old}`.\n")
    finding = next(item for item in result.findings if item.claim.kind == ClaimKind.default_value)
    symbol = next(item for item in result.symbols if item.name == "--new").model_copy(update={"default": new})
    finding.claim.expected_default = normalized
    finding.matched_symbol = symbol
    finding.verdict = Verdict.default_changed
    result.findings = [finding]
    plan = plan_rule_fixes(result)
    assert plan.edits
    apply_file(tmp_path, plan, "README.md")
    assert f"`{expected}`" in (tmp_path / "README.md").read_text()


def test_sentence_edit_keeps_bullet_and_neighbouring_sentence() -> None:
    line = "  - First sentence. Use `--old`."
    column, sentence = sentence_span(line, line.index("--old") + 1)
    assert sentence == "Use `--old`."
    assert line[: column - 1] == "  - First sentence. "


@pytest.mark.parametrize("raw", [b"Use `--old`.\r\n", codecs.BOM_UTF8 + b"Use `--old`.\r\n"])
def test_crlf_bom_patch_passes_git_apply(tmp_path: Path, raw: bytes) -> None:
    plan = plan_rule_fixes(rename_result(tmp_path, raw))
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "apply", "--check", "-"], input=unified_patch(tmp_path, plan).encode(), check=True
    )
