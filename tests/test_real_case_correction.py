"""Synthetic archive tests for the explicit real-case verifier; never run target programs."""

import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import verify_real_case as case


def archive(*, wrong_option: bool = False, escape: bool = False) -> bytes:
    readme = ["# Synthetic archive for verifier tests\n"] + ["\n"] * 179
    readme[60] = "Pass `--line-number`.\n" if not wrong_option else "Nothing to fix.\n"
    readme[179] = "Pass `--p`.\n"
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as snapshot:
        snapshot.writestr("README.md", "".join(readme))
        snapshot.writestr(
            "src/cli.py",
            "import click\n@click.command()\n"
            "@click.option('--line-numbers', '-n', is_flag=True)\n"
            "@click.option('--print', '-p', is_flag=True)\ndef main(): pass\n",
        )
        if escape:
            snapshot.writestr("../escape.py", "raise RuntimeError('must not run')")
    return buffer.getvalue()


def test_reviewed_patch_applies_and_corrected_claims_resolve_to_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = []

    def snapshot(args: list[str]) -> bytes:
        commands.append(args)
        return archive()

    monkeypatch.setattr("scripts.verify_real_case.subprocess.check_output", snapshot)
    output = tmp_path / "evidence"
    result = case.verify(tmp_path, output)
    assert commands[0][-1] == case.PIN
    assert result["git_apply_check"] == "passed" and result["target_code_executed"] is False
    rows = json.loads((output / "correction-verification.json").read_text())["rows"]
    assert len(rows) == 2 and all(row["after"] == "ok" for row in rows)
    patch = (output / "rich-cli-correction.patch").read_text()
    assert "+Pass `--line-numbers`." in patch and "+Pass `-p`." in patch
    assert not (tmp_path / "README.md").exists()
    with pytest.raises(ValueError, match="never overwritten"):
        case.verify(tmp_path, output)


@pytest.mark.parametrize("bad", ["wrong-option", "escaping-path"])
def test_unexpected_snapshots_fail_without_creating_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    monkeypatch.setattr(
        "scripts.verify_real_case.subprocess.check_output",
        lambda args: archive(
            wrong_option=bad == "wrong-option",
            escape=bad == "escaping-path",
        ),
    )
    with pytest.raises(ValueError):
        case.verify(tmp_path, tmp_path / "evidence")
    assert not (tmp_path / "evidence").exists()
