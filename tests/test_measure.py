"""Measurement validates the fixed key, scores fixtures, and reports exact commands."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.measure import AnswerKey, load_key, main


def test_loads_key_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    key = load_key(Path(__file__).parent / "fixtures/answer_key.json")
    assert key.findings
    assert key.non_findings
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "Claim recall: 13/13 (100.0%)" in output
    assert "Keyed finding recall: 13/13 (100.0%)" in output
    assert "Keyed-case precision: 13/13 (100.0%)" in output
    assert "Command: uv run python scripts/measure.py" in output


@pytest.mark.parametrize("change", ["bad_sha", "bad_line", "unsafe_path", "unknown_key", "duplicate", "new_name"])
def test_rejects_invalid_key(change: str) -> None:
    key = json.loads((Path(__file__).parent / "fixtures/answer_key.json").read_text(encoding="utf-8"))
    match change:
        case "bad_sha":
            key["findings"][0]["introduced_by"] = "not-a-sha"
        case "bad_line":
            key["findings"][0]["line"] = 0
        case "unsafe_path":
            key["findings"][0]["doc_path"] = "../README.md"
        case "unknown_key":
            key["typo"] = True
        case "duplicate":
            key["findings"].append(key["findings"][0])
        case "new_name":
            key["findings"][0]["expected_new"] = None
    with pytest.raises(ValidationError):
        AnswerKey.model_validate(key)


def test_missing_key_is_actionable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--key", str(tmp_path / "missing.json")]) == 1
    assert "never alter it to match detector output" in capsys.readouterr().err
