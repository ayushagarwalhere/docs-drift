from pathlib import Path

from docs_drift.baseline import load_baseline, new_findings, save_baseline
from docs_drift.ids import make_claim_id, make_section_id
from docs_drift.models import Claim, ClaimKind, Evidence, Extraction, Finding, Verdict


def _finding(value: str) -> Finding:
    path = "README.md"
    heading = ["Usage"]
    return Finding(
        claim=Claim(
            claim_id=make_claim_id(path, heading, ClaimKind.cli_flag.value, value),
            kind=ClaimKind.cli_flag,
            raw_text=f"`{value}`",
            value=value,
            doc_path=path,
            line=2,
            column=1,
            section_id=make_section_id(path, heading),
            extraction=Extraction.inline_code,
            extraction_confidence=0.9,
        ),
        verdict=Verdict.missing,
        evidence=[Evidence(file=path, line=2, text=value)],
        confidence=0.8,
        reasons=["No match."],
    )


def test_baseline_round_trip_and_new_findings(tmp_path: Path) -> None:
    old = _finding("--old")
    new = _finding("--new")
    path = tmp_path / "baseline.json"
    save_baseline(path, [old])
    baseline = load_baseline(path)
    assert new_findings([old, new], baseline) == [new]


def test_baseline_prunes_fixed_findings_on_update(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding("--old")])
    save_baseline(path, [])
    assert load_baseline(path).findings == []
