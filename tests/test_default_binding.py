"""A documented default must not be compared with a same-named flag of a different command."""

from pathlib import Path

from docs_drift.config import Config
from docs_drift.models import ScanResult, Verdict
from docs_drift.scanning import run_scan

HEADER = 'from typing import Annotated\n\nimport typer\n\napp = typer.Typer()\nDEFAULT_LABEL = "docs drift"\n'
BADGE = (
    "\n\n@app.command()\n"
    'def badge(label: Annotated[str, typer.Option("--label")] = DEFAULT_LABEL) -> None:\n'
    "    print(label)\n"
)
ISSUE = (
    "\n\n@app.command()\n"
    'def issue(label: Annotated[str, typer.Option("--label")] = "docs-drift") -> None:\n'
    "    print(label)\n"
)


def _scan(tmp_path: Path, source: str, readme: str) -> ScanResult:
    (tmp_path / "cli.py").write_text(source, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    return run_scan(tmp_path, Config(), history=False, no_cache=True)


def test_constant_default_on_one_command_makes_shared_flag_default_ambiguous(tmp_path: Path) -> None:
    result = _scan(tmp_path, HEADER + BADGE + ISSUE, "# Badge\n\n- `--label`: left-hand text (default `docs drift`).\n")
    defaults = [f for f in result.findings if f.claim.value == "--label" and f.claim.expected_default]
    assert defaults, "the documented default should be extracted"
    assert all(f.verdict != Verdict.default_changed for f in defaults)


def test_single_declaration_still_reports_a_changed_default(tmp_path: Path) -> None:
    result = _scan(tmp_path, HEADER + ISSUE, "# Issue\n\n- `--label`: issue label (default `drift`).\n")
    changed = [f for f in result.findings if f.claim.value == "--label" and f.verdict == Verdict.default_changed]
    assert changed and "docs-drift" in (changed[0].suggestion or "")
