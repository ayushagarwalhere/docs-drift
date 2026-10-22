"""Incremental adoption must neither hide regressions nor mutate full evidence."""

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from docs_drift.baseline import load_baseline, save_baseline
from docs_drift.models import Verdict
from docs_drift.policy import GatePolicy, evaluate_gate
from tests.test_models import finding, result


def test_baseline_allows_existing_debt_but_changed_verdict_fails(tmp_path: Path) -> None:
    old = finding(Verdict.missing)
    path = tmp_path / "baseline.json"
    save_baseline(path, [old])
    scan = result([old])
    accepted = evaluate_gate(scan, GatePolicy(), load_baseline(path))
    assert accepted.summary.status == "passed"
    assert accepted.summary.baseline_drift == 1
    assert accepted.new_findings == []
    assert scan.findings == [old]
    changed = evaluate_gate(result([finding(Verdict.renamed)]), GatePolicy(), load_baseline(path))
    assert changed.summary.status == "failed"
    assert changed.summary.new_drift == 1


def test_confidence_and_budget_boundaries_are_explicit() -> None:
    high = finding().model_copy(update={"confidence": 0.8})
    low = finding(Verdict.missing).model_copy(update={"confidence": 0.79})
    scan = result([high, low, finding(Verdict.unverifiable), finding(Verdict.ok)])
    gate = evaluate_gate(scan, GatePolicy(min_confidence=0.8, max_findings=1))
    assert gate.summary.status == "passed"
    assert gate.summary.total_drift == 2
    assert gate.summary.qualifying_new_drift == 1
    assert gate.summary.below_threshold_new_drift == 1
    assert len(scan.findings) == 4
    assert evaluate_gate(scan, GatePolicy(min_confidence=0.79, max_findings=1)).summary.status == "failed"
    assert evaluate_gate(scan, GatePolicy(fail_on_drift=False)).summary.status == "report_only"


def test_gate_warnings_are_redacted_without_modifying_scan() -> None:
    scan = result()
    scan.warnings = ["password=" + "example-sensitive-value"]
    gate = evaluate_gate(scan, GatePolicy())
    assert gate.summary.warnings[0] == "[REDACTED]"
    assert "example-sensitive-value" not in gate.summary.model_dump_json()
    assert scan.warnings[0] != "[REDACTED]"


def test_empty_or_unversioned_scan_is_visible_in_gate_warnings() -> None:
    scan = result().model_copy(update={"head_sha": "0" * 40})
    gate = evaluate_gate(scan, GatePolicy())
    assert gate.summary.claims_extracted == 0
    assert any("No documentation claims" in warning for warning in gate.summary.warnings)
    assert any("unversioned" in warning for warning in gate.summary.warnings)


@pytest.mark.parametrize("confidence", [-0.1, 1.1, math.nan, math.inf])
def test_invalid_policy_fails_closed(confidence: float) -> None:
    with pytest.raises(ValidationError):
        GatePolicy(min_confidence=confidence)


@pytest.mark.parametrize("budget", [-1, 0.5, True])
def test_budget_must_be_nonnegative_integer(budget: object) -> None:
    with pytest.raises(ValidationError):
        GatePolicy.model_validate({"max_findings": budget})
