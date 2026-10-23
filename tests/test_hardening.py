"""Regressions from own-repo scans and reproducible Windows report contracts."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docs_drift.analysis import changed_paths, impact, insights
from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.history import GitHistory
from docs_drift.index import build_index
from docs_drift.report import render
from docs_drift.scanning import run_scan
from docs_drift.support import support_table
from scripts.generate_reference import render_reference
from tests.fixtures.build_fixtures import build_fixtures
from tests.test_extract_inline import extract
from tests.test_history import _commit, _git

ROOT = Path(__file__).resolve().parents[1]


def test_typer_annotated_callback_and_required_options_are_static(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text("""from typing import Annotated
import typer
app = typer.Typer()
cache = typer.Typer(add_completion=False)
app.add_typer(cache, name="cache")
@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False): pass
@app.command()
def scan(base: Annotated[str, typer.Option()], *, mode: Annotated[str, typer.Option()] = "fast"): pass
""")
    index = build_index(tmp_path, Config(), no_cache=True)
    names = {s.name for s in index.symbols}
    assert {"--base", "--mode", "--verbose", "scan", "cache", "--show-completion"} <= names
    verbose = next(s for s in index.symbols if s.name == "--verbose")
    assert "-v" in verbose.aliases and verbose.qualified_name.endswith("root.--verbose")


def test_shell_commands_globs_and_extensions_are_not_file_existence_claims() -> None:
    result = extract("""# Usage
`docs-drift scan --repo ./project` and `*.md` and `.rst` are examples.
Never commit `.env` or `.env.*`.
The generated `output/report.json` is created on demand.
Use `src/app.py` to find the implementation.
""")
    assert [(c.value, c.qualifier) for c in result.claims if c.kind == "file_path"] == [
        ("output/report.json", "non-checkout-path"),
        ("src/app.py", None),
    ]


def test_generated_debug_log_is_not_required_to_exist(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "Failures write the generated debug log `.docs-drift/last-error.log`.\n\n"
        "Use `src/missing.py` to find the implementation.\n"
    )
    result = run_scan(tmp_path, Config())
    log = next(f for f in result.findings if f.claim.value == ".docs-drift/last-error.log")
    assert log.verdict == "unverifiable"
    assert not log.is_drift
    source = next(f for f in result.findings if f.claim.value == "src/missing.py")
    assert source.verdict == "missing"


def test_table_description_is_not_a_literal_default() -> None:
    result = extract("| Variable | Default and purpose |\n| --- | --- |\n| `APP_PORT` | `80`; HTTP port |\n")
    assert all(c.expected_default is None for c in result.claims)


def test_validation_docs_do_not_require_local_artifacts_in_a_clean_checkout(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("CI-DIAGNOSIS.md", "ORG-PILOT-2026-10-22.md", "ORG-VERIFICATION-2026-10-22.md"):
        (docs / name).write_text((ROOT / "docs" / name).read_text(encoding="utf-8"), encoding="utf-8")
    result = run_scan(tmp_path, Config(), history=False, no_cache=True)
    artifacts = [f for f in result.findings if f.claim.kind == "file_path" and f.claim.value.startswith(".docs-drift/")]
    assert {
        ".docs-drift/ci-diagnosis/",
        ".docs-drift/ci-diagnosis/final-tests.xml",
        ".docs-drift/coverage.json",
        ".docs-drift/org-pilot-corrected/index.html",
        ".docs-drift/org-dogfood-final.json",
    } <= {f.claim.value for f in artifacts}
    assert all(not f.is_drift for f in artifacts), [(f.claim.value, f.verdict) for f in artifacts]


def test_same_name_packages_do_not_duplicate_findings(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="same"\n')
    (tmp_path / "package.json").write_text('{"name":"same"}')
    nested = tmp_path / "editor"
    nested.mkdir()
    (nested / "package.json").write_text('{"name":"same"}')
    (tmp_path / "README.md").write_text("Use `missing.py`.\n")
    result = run_scan(tmp_path, Config())
    assert len(result.findings) == 1
    assert len({f.claim.claim_id for f in result.findings}) == len(result.findings)


def test_history_does_not_call_a_reappearing_name_a_rename(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, 'docs = ["one"]\n', "2026-01-01T00:00:00+00:00")
    _commit(tmp_path, 'docs = ["two"]\n', "2026-01-02T00:00:00+00:00")
    assert GitHistory(tmp_path).search("docs", ("docs",)).rename is None


def test_impact_validates_refs_before_running_git(tmp_path: Path) -> None:
    for ref in ("--help", "HEAD..main", "HEAD main"):
        with pytest.raises(ValueError, match="single Git revision"):
            changed_paths(tmp_path, ref, "HEAD")
        with pytest.raises(ValueError, match="single Git revision"):
            impact(tmp_path, Config(), "HEAD", ref)


@pytest.mark.parametrize("fixture", ["py-cli", "node-tool", "fastapi-svc", "clean-repo"])
def test_json_byte_identical_with_history_cold_and_warm(tmp_path: Path, fixture: str) -> None:
    build_fixtures(tmp_path)
    repo = tmp_path / fixture
    first = render(run_scan(repo, Config(), no_cache=True), "json")
    second = render(run_scan(repo, Config()), "json")
    assert first == second
    assert '"repo_path": "."' in first
    assert "\\\\" not in first


def test_crlf_and_path_with_spaces(tmp_path: Path) -> None:
    repo = tmp_path / "path with spaces"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"# Usage\r\nUse `module.py`.\r\n")
    (repo / "module.py").write_text("answer = 42\n")
    result = run_scan(repo, Config())
    assert len(result.findings) == 1 and result.findings[0].verdict == "ok"
    assert result.findings[0].claim.line == 2
    assert insights(result)["claims"] == 1


def test_generated_reference_and_readme_support_table_are_fresh() -> None:
    assert (ROOT / "docs/configuration.md").read_text(encoding="utf-8") == render_reference()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme.split("<!-- support-table:start -->")[1].split("<!-- support-table:end -->")[0].strip()
    assert table == support_table().strip()


def test_every_readme_shell_command_against_trusted_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixtures(tmp_path)
    repo = tmp_path / "py-cli"
    monkeypatch.chdir(repo)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = []
    for block in re.findall(r"```sh\n(.*?)```", readme, re.S):
        for source_line in block.splitlines():
            line = source_line.removeprefix("uv run ").removeprefix("uvx ")
            if line.startswith("docs-drift "):
                command = line.partition("#")[0].strip().split()[1:]
                commands.append(command)
                result = CliRunner().invoke(app, [*command, "--repo", str(repo)])
                assert result.exit_code == (1 if command[0] == "check" else 0), result.output
    assert len(commands) >= 8
