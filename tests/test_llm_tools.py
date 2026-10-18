"""Exercise fixture recording and fix verification helpers without network calls."""

import asyncio
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.llm.client import ClientError, Recording
from docs_drift.llm.pricing import DEFAULT_MODEL
from docs_drift.llm.prompts import PROMPT_VERSION, build_request
from docs_drift.llm.types import ModelRequest, ModelResponse
from scripts import record, review_fixes


def test_recording_dry_run_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = build_request(DEFAULT_MODEL, "example", [])
    monkeypatch.setattr(record, "fixture_requests", lambda root, config: [request])
    monkeypatch.setattr("sys.argv", ["record.py", "--dry-run"])
    assert record.main() == 0
    assert request.request_hash() in capsys.readouterr().out
    old = tmp_path / "stale.json"
    saved = Recording.model_validate(
        {
            "request_hash": "stale",
            "model": DEFAULT_MODEL,
            "prompt_version": PROMPT_VERSION,
            "response": {
                "result": {
                    "verdict": "not_found",
                    "evidence": [],
                    "explanation": "Synthetic setup",
                    "suggested_doc_text": None,
                }
            },
        }
    )
    old.write_text(saved.model_dump_json(), encoding="utf-8")
    synthetic = tmp_path / "synthetic.json"
    synthetic.write_text(saved.model_copy(update={"provenance": "synthetic"}).model_dump_json(), encoding="utf-8")

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse.model_validate(
                {
                    "result": {
                        "verdict": "not_found",
                        "evidence": [],
                        "explanation": "Synthetic test",
                        "suggested_doc_text": None,
                    }
                }
            )

    monkeypatch.setattr(record, "RecordedClient", Client)
    monkeypatch.setattr("sys.argv", ["record.py", "--output", str(tmp_path), "--prune"])
    assert record.main() == 0
    assert not old.exists()
    assert synthetic.exists()


def test_recording_budget_rejects_calls() -> None:
    config = Config.model_validate({"llm": {"budget": 0}})
    with pytest.raises(ClientError, match="budget reached"):
        asyncio.run(record.record_requests([build_request(DEFAULT_MODEL, "example", [])], config, Path("unused")))


def test_every_fixture_rule_fix_rescans_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert review_fixes.main() == 0
    output = capsys.readouterr().out
    assert "all proposed fixes verified OK" in output
    assert "Total: 11 auto-fixable, 3 suggestion-only findings." in output
