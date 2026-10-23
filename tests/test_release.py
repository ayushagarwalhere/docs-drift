"""Release contract, archive contents and repeatable recording-state checks."""

import io
import json
import os
import subprocess
from datetime import date
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import httpx
import pytest

from docs_drift.config import Config
from docs_drift.index.ts_parser import parser_for
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from scripts import check_readme_links, dogfood, verify_install
from scripts.demo_setup import prepare
from scripts.release import check_archives, notes, requires_release_candidate, tag_version
from scripts.submission_reminders import reminders


@pytest.mark.parametrize(("tag", "expected"), [("v0.1.0", "0.1.0"), ("v0.1.0-rc.1", "0.1.0rc1")])
def test_tag_version(tag: str, expected: str) -> None:
    assert tag_version(tag) == expected


@pytest.mark.parametrize(
    ("tag", "required"), [("v0.1.0", True), ("v0.1.0-rc.1", False), ("v0.1.1", False), ("v0.2.0", False)]
)
def test_first_stable_requires_rc_but_later_versions_are_one_tag_away(tag: str, required: bool) -> None:
    assert requires_release_candidate(tag) is required


@pytest.mark.parametrize("tag", ["v0", "submission-2026-10-30", "v0.1.0-rc.0", "0.1.0", "v1.0.0;echo bad"])
def test_non_release_tags_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="Use v"):
        tag_version(tag)


def test_changelog_notes_do_not_include_unreleased_or_other_versions() -> None:
    source = "## [Unreleased]\nfuture\n## [0.1.0] - 2026-10-18\nfirst\n## [0.0.1]\nold\n"
    result = notes(source, "0.1.0rc1")
    assert "Release candidate" in result and "first" in result
    assert "future" not in result and "old" not in result
    with pytest.raises(ValueError, match="Missing changelog"):
        notes(source, "0.2.0")


def test_repeatable_demo_head_output_and_existing_directory_protection(tmp_path: Path) -> None:
    first = prepare(tmp_path / "take-01")
    second = prepare(tmp_path / "take-02")
    assert first["head"] == second["head"] and first["claim"] == second["claim"]
    assert (tmp_path / "take-01/scan.json").read_bytes() == (tmp_path / "take-02/scan.json").read_bytes()
    data = json.loads((tmp_path / "take-01/scan.json").read_text())
    assert data["package_version"] == version("docs-drift") and data["schema_version"] == 1
    assert len(data["findings"]) <= 3
    assert any(f["introduced_by"] for f in data["findings"])
    review = json.loads((tmp_path / "take-01/review-preview.json").read_text())
    assert len(review["comments"]) == 2
    assert "2 doc statements outdated" in review["body"]
    with pytest.raises(FileExistsError, match="fresh"):
        prepare(tmp_path / "take-01")


def test_package_version_is_separate_from_schema_and_schema_copy_is_current(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Empty project\n")
    result = run_scan(tmp_path, Config())
    assert result.package_version == version("docs-drift") and result.schema_version == 1
    schema = json.loads(files("docs_drift").joinpath("schemas/scan-result.v1.json").read_text(encoding="utf-8"))
    assert schema == ScanResult.model_json_schema()


def test_native_grammars_work_in_fresh_directory_without_network_or_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("docs_drift.index.ts_parser._PARSERS", {})
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    assert not parser_for("javascript").parse(b"export const value = process.env.API_URL;").root_node.has_error
    assert not parser_for("typescript").parse(b"export const value: string = 'ready';").root_node.has_error
    assert not (tmp_path / ".docs-drift").exists()


def test_weekly_cards_cover_start_to_deadline_and_do_not_invent_past_uploads() -> None:
    rows = reminders(date(2026, 10, 1))
    assert rows[0][0] == date(2026, 10, 1) and rows[-1][1] == date(2026, 10, 30)
    assert [str(upload) for _, _, upload in rows] == [
        "2026-10-07",
        "2026-10-14",
        "2026-10-21",
        "2026-10-28",
        "2026-10-29",
    ]
    assert reminders(date(2026, 10, 30))[0][2] == date(2026, 10, 30)
    with pytest.raises(ValueError, match="start"):
        reminders(date(2026, 10, 31))


def test_built_wheel_and_sdist_content_contract() -> None:
    directory = os.environ.get("DOCS_DRIFT_DIST")
    if not directory:
        pytest.skip("Set DOCS_DRIFT_DIST to the actual uv build output; release CI always does.")
    check_archives(Path(directory))


def test_testpypi_requirement_pins_its_wheel_without_mixing_dependency_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = {
        "filename": "docs_drift-0.1.0rc1-py3-none-any.whl",
        "url": "https://test-files.pythonhosted.org/packages/example.whl",
        "digests": {"sha256": "a" * 64},
    }
    monkeypatch.setattr(
        "scripts.verify_install.urllib.request.urlopen",
        lambda *a, **kw: io.BytesIO(json.dumps({"urls": [artifact]}).encode()),
    )
    spec = verify_install.requirement("testpypi", "0.1.0rc1", extras=True)
    assert spec.startswith("docs-drift[web,graph,llm] @ https://test-files.pythonhosted.org/")
    assert spec.endswith("#sha256=" + "a" * 64)
    artifact["url"] = "https://attacker.invalid/example.whl"
    with pytest.raises(ValueError, match="artifact host"):
        verify_install.requirement("testpypi", "0.1.0rc1")


@pytest.mark.parametrize("method", ["pipx", "uv", "pip"])
def test_install_verification_uses_isolated_homes_and_checks_scan_contract(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if "scan" in command:
            destination = Path(command[command.index("--output") + 1])
            destination.write_text(json.dumps({"package_version": "0.1.0", "schema_version": 1, "findings": [1]}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.verify_install.subprocess.run", run)
    monkeypatch.setattr("scripts.verify_install.subprocess.check_output", lambda *a, **kw: "0.1.0\n")
    monkeypatch.setattr("scripts.verify_install.venv.create", lambda *a, **kw: None)
    verify_install.verify(method, "pypi", "0.1.0", Path("builder.py").resolve())
    command, kwargs = next((command, kwargs) for command, kwargs in calls if "scan" in command)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert str(kwargs["cwd"]) in env["PIPX_HOME"] and str(kwargs["cwd"]) in env["UV_TOOL_DIR"]
    assert str(kwargs["cwd"]) in env["PIPX_MAN_DIR"]
    assert "--repo" in command and "--no-color" in command


def test_link_checker_parses_badges_and_reports_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = check_readme_links.targets("[![CI](https://example.invalid/badge)](README.md) [missing](gone.md)")
    assert urls == ["README.md", "gone.md", "https://example.invalid/badge"]
    (tmp_path / "README.md").write_text("hello")
    assert check_readme_links.check("README.md#section", tmp_path)["ok"]
    assert not check_readme_links.check("gone.md", tmp_path)["ok"]
    monkeypatch.setattr("scripts.check_readme_links.httpx.get", lambda *a, **kw: httpx.Response(404))
    assert not check_readme_links.check("https://example.invalid/missing", tmp_path)["ok"]

    def fail(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("scripts.check_readme_links.httpx.get", fail)
    assert check_readme_links.check("https://example.invalid/offline", tmp_path)["status"] == "ConnectError"


def test_validation_snapshot_removes_deleted_files_and_preserves_original_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", *args], cwd=root)

    git("init", "-q")
    (root / ".gitignore").write_text(".docs-drift/\n")
    (root / "README.md").write_text("first\n")
    (root / "obsolete.yml").write_text("old publishing workflow\n")
    git("add", "--all")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "initial",
    )
    target = root / ".docs-drift/dogfood/path with spaces/repository"
    monkeypatch.setattr(dogfood, "ROOT", root)
    monkeypatch.setattr(dogfood, "TARGET", target)
    before = git("ls-files", "--stage", "-z")
    dogfood.prepare()
    assert (target / "obsolete.yml").exists()
    (root / "obsolete.yml").unlink()
    (root / "README.md").write_text("current\n")
    dogfood.prepare()
    assert not (target / "obsolete.yml").exists()
    assert (target / "README.md").read_text() == "current\n"
    assert git("ls-files", "--stage", "-z") == before


def test_link_checker_command_records_failures_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("[broken](missing.md)\n")
    monkeypatch.setattr("sys.argv", ["check-readme-links", "--out", "links.json"])
    with pytest.raises(SystemExit) as failure:
        check_readme_links.main()
    assert failure.value.code == 1
    assert not json.loads((tmp_path / "links.json").read_text())[0]["ok"]
    (tmp_path / "missing.md").write_text("now present")
    with pytest.raises(SystemExit) as success:
        check_readme_links.main()
    assert success.value.code == 0
