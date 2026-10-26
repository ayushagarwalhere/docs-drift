"""Shared model invariants, content-based identities, and the JSON report contract."""

from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

import pytest
from pydantic import ValidationError

from docs_drift.config import Config
from docs_drift.ids import make_claim_id, make_section_id, make_symbol_id, normalize_repo_path
from docs_drift.models import (
    Claim,
    ClaimKind,
    CommitRef,
    DocFile,
    DocFormat,
    Evidence,
    Extraction,
    Finding,
    ScanResult,
    Section,
    SectionContext,
    Symbol,
    SymbolKind,
    Verdict,
)


def claim(line: int = 4, value: str = "--port", occurrence: int = 0) -> Claim:
    return Claim(
        claim_id=make_claim_id("README.md", ["Usage", "Server options"], ClaimKind.cli_flag, value, occurrence),
        kind=ClaimKind.cli_flag,
        raw_text=f"`{value}`",
        value=value,
        qualifier="serve",
        doc_path="README.md",
        line=line,
        column=3,
        section_id=make_section_id("README.md", ["Usage", "Server options"]),
        extraction=Extraction.inline_code,
        extraction_confidence=0.9,
    )


def symbol(line: int = 10, name: str = "serve.--port") -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(SymbolKind.cli_flag, name, "src/cli.py"),
        kind=SymbolKind.cli_flag,
        name="--port",
        qualified_name=name,
        aliases=["-p"],
        default="8000",
        file="src\\cli.py",
        line=line,
        language="python",
        framework="argparse",
        parent_id=make_symbol_id(SymbolKind.cli_command, "serve", "src/cli.py"),
    )


def result(findings: list[Finding] | None = None) -> ScanResult:
    return ScanResult(
        repo_path=Path("example-repo"),
        head_sha="a" * 40,
        scan_time=datetime(2026, 10, 18, 9, tzinfo=UTC),
        config_used=Config(),
        findings=[] if findings is None else findings,
    )


def finding(verdict: Verdict = Verdict.renamed) -> Finding:
    return Finding(
        claim=claim(),
        matched_symbol=symbol(),
        verdict=verdict,
        suggestion="--listen",
        evidence=[Evidence(file="README.md", line=4, text="`--port`")],
        confidence=0.95,
        reasons=["History links the old flag to its replacement."],
    )


def test_ids_survive_line_shifts() -> None:
    assert claim(4).claim_id == claim(100).claim_id
    assert symbol(10).symbol_id == symbol(200).symbol_id
    section = Section(
        section_id=make_section_id("README.md", ["Usage"]),
        doc_path="README.md",
        heading="Usage",
        heading_level=1,
        heading_path=["Usage"],
        start_line=1,
        end_line=10,
    )
    moved = Section.model_validate({**section.model_dump(), "start_line": 11, "end_line": 20})
    assert section.section_id == moved.section_id


def test_changed_values_and_symbol_names_change_ids() -> None:
    assert claim(value="--port").claim_id != claim(value="--listen").claim_id
    assert symbol(name="serve.--port").symbol_id != symbol(name="serve.--listen").symbol_id
    assert make_symbol_id("cli_flag", "serve.--port", "src/cli.py") != make_symbol_id(
        "cli_flag", "serve.--port", "src/other.py"
    )


def test_occurrences_and_heading_scope_disambiguate_ids() -> None:
    assert claim().claim_id == claim(occurrence=0).claim_id
    assert claim().claim_id != claim(occurrence=1).claim_id
    assert make_claim_id("README.md", ["Client"], "cli_flag", "--port") != claim().claim_id
    assert make_claim_id("README.md", ["A/B", "C"], "symbol", "f") != make_claim_id(
        "README.md", ["A", "B/C"], "symbol", "f"
    )
    assert make_section_id("README.md", ["Usage"]) != make_section_id("README.md", ["Usage"], 1)
    with pytest.raises(ValueError, match="positive counter"):
        make_claim_id("README.md", [], "cli_flag", "--port", -1)


def test_windows_paths_are_portable_in_models_and_ids() -> None:
    doc = DocFile.model_validate(
        {"path": PureWindowsPath("docs/guide.md"), "content_hash": "b" * 64, "format": "markdown"}
    )
    assert doc.path == "docs/guide.md"
    assert symbol().file == "src/cli.py"
    assert Evidence(file="docs\\guide.md", line=1, text="").file == "docs/guide.md"
    assert make_claim_id("docs\\guide.md", [], "cli_flag", "--port") == make_claim_id(
        "docs/guide.md", [], "cli_flag", "--port"
    )
    assert make_symbol_id("function", "f", PureWindowsPath("src/module.py")) == make_symbol_id(
        "function", "f", "src/module.py"
    )


@pytest.mark.parametrize("path", ["C:\\repo\\README.md", "C:README.md", "/README.md", "../README.md", "", ".", 12])
def test_invalid_repository_paths_are_rejected(path: object) -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        normalize_repo_path(path)


def test_full_json_round_trip() -> None:
    report = result([finding()])
    report.docs = [DocFile(path="README.md", content_hash="b" * 64, format=DocFormat.markdown)]
    report.sections = [
        Section(
            section_id=claim().section_id,
            doc_path="README.md",
            heading="Server options",
            heading_level=2,
            heading_path=["Usage", "Server options"],
            start_line=3,
            end_line=10,
            context=SectionContext.historical,
        )
    ]
    report.claims = [claim()]
    report.symbols = [symbol()]
    report.findings[0].introduced_by = CommitRef(
        sha="a" * 40,
        short_sha="a" * 7,
        author_name="Priya Shah",
        author_email="priya@example.invalid",
        date=datetime(2026, 10, 2, 9, tzinfo=UTC),
        summary="Rename the port flag",
    )
    report.timings = {"docs": 0.01, "match": 0.02}
    serialized = report.model_dump_json()
    assert '"schema_version":1' in serialized
    assert ScanResult.model_validate_json(serialized) == report
    with pytest.raises(ValidationError):
        ScanResult.model_validate_json(serialized.replace('"schema_version":1', '"schema_version":2'))


@pytest.mark.parametrize("verdict", list(Verdict))
def test_drift_property(verdict: Verdict) -> None:
    assert finding(verdict).is_drift is (
        verdict
        in {
            Verdict.missing,
            Verdict.renamed,
            Verdict.default_changed,
            Verdict.invalid_command,
            Verdict.version_mismatch,
            Verdict.unknown_extra,
        }
    )


def test_summary_counts_findings_including_zero_categories() -> None:
    summary = result([finding(Verdict.ok), finding(), finding(Verdict.missing)]).summary()
    assert summary["by_verdict"] == {
        "ok": 1,
        "missing": 1,
        "renamed": 1,
        "default_changed": 0,
        "invalid_command": 0,
        "unverifiable": 0,
        "version_mismatch": 0,
        "unknown_extra": 0,
    }
    assert summary["by_kind"]["cli_flag"] == 3
    assert summary["by_kind"]["env_var"] == 0
    assert all(count == 0 for count in result().summary()["by_kind"].values())


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_confidence_bounds(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate({**finding().model_dump(), "confidence": confidence})
    with pytest.raises(ValidationError):
        Claim.model_validate({**claim().model_dump(), "extraction_confidence": confidence})


def test_evidence_and_reasons_are_required() -> None:
    for field in ("evidence", "reasons"):
        with pytest.raises(ValidationError):
            Finding.model_validate({**finding().model_dump(), field: []})


def test_invalid_section_span_and_commit_reference() -> None:
    with pytest.raises(ValidationError, match="end_line"):
        Section(
            section_id=claim().section_id,
            doc_path="README.md",
            heading="",
            heading_level=0,
            heading_path=[],
            start_line=5,
            end_line=1,
        )
    with pytest.raises(ValidationError, match="prefix"):
        CommitRef(
            sha="a" * 40,
            short_sha="b" * 7,
            author_name="A",
            author_email="a@example.invalid",
            date=datetime(2026, 10, 18, tzinfo=UTC),
            summary="Example",
        )
    with pytest.raises(ValidationError):
        ScanResult.model_validate({**result().model_dump(), "timings": {"match": -1}})
