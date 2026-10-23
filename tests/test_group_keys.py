"""Narrow generated-hash exceptions are verified rather than broad secret-scan exclusions."""

import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.verify_group_keys import verify_document, verify_repository


def generated() -> dict[str, str | None]:
    return {
        "kind": "cli_flag",
        "value": "--verbose",
        "qualifier": None,
        "group_key": hashlib.sha256(b"cli_flag\0--verbose\0").hexdigest(),
    }


def test_only_recomputed_grouping_hashes_are_accepted() -> None:
    assert verify_document({"claims": [generated(), {"group_key": None}]}) == 1
    for invalid in (
        {"group_key": "0" * 64},
        {**generated(), "value": "changed"},
        {**generated(), "qualifier": ["bad"]},
    ):
        with pytest.raises(ValueError):
            verify_document(invalid)


def test_secret_exception_only_allows_the_exact_generated_field_in_named_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / ".gitleaks.toml").read_text())
    exception = config["rules"][0]["allowlists"][0]
    assert exception["condition"] == "AND" and exception["regexTarget"] == "line"
    pattern = exception["regexes"][0]
    assert re.fullmatch(pattern, '  "group_key": "' + "a" * 64 + '",')
    assert not re.fullmatch(pattern, '  "api_key": "' + "a" * 64 + '",')
    assert not any(re.fullmatch(p, "src/credentials.json") for p in exception["paths"])


def test_history_hash_verification_keeps_deleted_files_in_scope(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / ".gitleaks.toml").write_text(
        "[[rules]]\nid=\"generic-api-key\"\n[[rules.allowlists]]\npaths=['^example\\.json$']\n"
    )
    path = tmp_path / "example.json"
    path.write_text(json.dumps([generated()]))
    git("add", ".")
    git("commit", "-m", "generated hash")
    assert verify_repository(tmp_path, history=True) == (2, 2)
    path.unlink()
    git("add", ".")
    git("commit", "-m", "remove example")
    assert verify_repository(tmp_path, history=True) == (1, 1)
    path.write_text(json.dumps({**generated(), "group_key": "0" * 64}))
    with pytest.raises(ValueError, match="does not match"):
        verify_repository(tmp_path)
