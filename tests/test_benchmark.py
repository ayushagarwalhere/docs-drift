"""Human-label integrity, small-sample uncertainty and weighted audit calculations."""

import json
from pathlib import Path

import pytest

from benchmark import compute, run
from benchmark.compute import read_labels, summarize, wilson


def test_wilson_small_samples_and_empty() -> None:
    assert wilson(0, 0) is None
    assert wilson(0, 1) == pytest.approx((0, 0.7934506856227626))
    assert wilson(1, 1) == pytest.approx((0.20654931437723745, 1))
    assert wilson(5, 10) == pytest.approx((0.236593090512564, 0.7634069094874361))
    with pytest.raises(ValueError):
        wilson(2, 1)


def test_pending_labels_are_never_accuracy_and_recall_uses_population_weights() -> None:
    rows = [
        {"row_id": "p1", "repo": "a", "sample": "precision", "tool_verdict": "missing"},
        {"row_id": "p2", "repo": "a", "sample": "precision", "tool_verdict": "missing"},
        {"row_id": "r1", "repo": "a", "sample": "recall", "stratum": "ok"},
        {"row_id": "r2", "repo": "a", "sample": "recall", "stratum": "unextracted"},
    ]
    labels = {row["row_id"]: {"verdict": "", "note": ""} for row in rows}
    populations = {"a": {"precision": 100, "recall": {"ok": {"population": 20}, "unextracted": {"population": 200}}}}
    pending = summarize(rows, labels, populations)
    assert pending["pending_labels"] == 4
    assert pending["weighted_audit_recall"] is None
    assert pending["precision"]["repo:a"]["wilson95"] is None
    for key, verdict in {"p1": "true", "p2": "false", "r1": "stale", "r2": "not-a-claim"}.items():
        labels[key]["verdict"] = verdict
    complete = summarize(rows, labels, populations)
    assert complete["precision"]["repo:a"]["precision"] == 0.5
    assert complete["estimated_true_positives"] == 50
    assert complete["estimated_missed_stale_statements"] == 20
    assert complete["weighted_audit_recall"] == 50 / 70
    assert complete["recall_interval"] is None


def test_label_sheet_rejects_deleted_duplicate_or_invalid_rows(tmp_path: Path) -> None:
    path = tmp_path / "labels.md"
    rows = [{"row_id": "abc", "sample": "precision"}]
    blank = "| abc | repo | doc | statement | code | missing | suggestion |  |  |\n"
    path.write_text(blank)
    assert read_labels(path, rows)["abc"]["verdict"] == ""
    for bad in ("", blank + blank, blank.replace("|  |  |", "| stale |  |")):
        path.write_text(bad)
        with pytest.raises(ValueError):
            read_labels(path, rows)


def test_sampling_reproducible_per_repo() -> None:
    items = list(range(200))
    assert run.sample(items, 25, 123, "owner/repo") == run.sample(items, 25, 123, "owner/repo")
    assert run.sample(items, 25, 124, "owner/repo") != run.sample(items, 25, 123, "owner/repo")
    assert len(run.sample(items[:3], 25, 123, "owner/repo")) == 3
    assert run.link({"repo_url": "https://github.com/a/b", "sha": "abc"}, "docs/a b.md", 7).endswith(
        "/abc/docs/a%20b.md#L7"
    )


def test_frozen_run_rejects_detector_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stamp = {"source_sha256": "original", "protocol_sha256": "unused"}
    (tmp_path / "stamp.json").write_text(json.dumps(stamp))
    monkeypatch.setattr(run, "source_digest", lambda: "changed")
    with pytest.raises(ValueError, match="Scanner changed"):
        run.freeze(tmp_path)
    assert json.loads((tmp_path / "stamp.json").read_text()) == stamp


def test_report_requires_human_attribution_and_preserves_published_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All labels here are synthetic test input in a temporary tree, never the actual owner sheet."""
    target = tmp_path / "benchmark/runs/test"
    target.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("<!-- accuracy:start -->\npending\n<!-- accuracy:end -->\n")
    stamp = {
        "date": "2026-10-18",
        "version": "test",
        "scanner_commit": "a" * 40,
        "source_sha256": "b" * 64,
        "dirty": True,
        "finding_seed": 1,
        "recall_seed": 2,
    }
    monkeypatch.setattr(compute, "ROOT", tmp_path)
    monkeypatch.setattr(compute, "freeze", lambda path: stamp)
    rows = [
        {"row_id": "p", "repo": "a/b", "sample": "precision", "tool_verdict": "missing"},
        {"row_id": "r", "repo": "a/b", "sample": "recall", "stratum": "ok"},
    ]
    population = {"a/b": {"precision": 1, "recall": {"ok": {"population": 1}}}}
    (target / "samples.json").write_text(json.dumps({"rows": rows, "populations": population}))
    (target / "repos.json").write_text(json.dumps([{"name": "a/b"}]))
    (target / "status.json").write_text(json.dumps({"a/b": {"status": "done"}}))
    (target / "a--b.measurement.json").write_text(
        json.dumps(
            {"seconds": 1.1, "claims": 2, "findings": {"by_verdict": {"unverifiable": 0, "missing": 1, "ok": 1}}}
        )
    )
    sheet = tmp_path / "sheet.md"
    blank = "| p | repo | doc | statement | code | missing | suggestion |  |  |\n"
    blank += "| r | repo | doc | statement | code | ok | suggestion |  |  |\n"
    sheet.write_text(blank)
    with pytest.raises(ValueError, match="Human labels"):
        compute.report(target, sheet, None, final=True, drafts=True)
    assert not (target / "RESULTS.md").exists()
    filled = blank.replace(
        "| p | repo | doc | statement | code | missing | suggestion |  |",
        "| p | repo | doc | statement | code | missing | suggestion | true |",
    )
    filled = filled.replace(
        "| r | repo | doc | statement | code | ok | suggestion |  |",
        "| r | repo | doc | statement | code | ok | suggestion | current |",
    )
    sheet.write_text(filled)
    with pytest.raises(ValueError, match="labeler's name"):
        compute.report(target, sheet, None)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs/benchmark.md").write_text(
        f"# Old results\n\n{compute.AGENT_REVIEW_HEADING}, October 2026\n\nKept by hand.\n", encoding="utf-8"
    )
    report = compute.report(target, sheet, "Synthetic reviewer — test", final=True)
    assert "No false positives were found in the human-labeled sample" in report
    assert "Synthetic reviewer" in (tmp_path / "README.md").read_text(encoding="utf-8")
    page = (tmp_path / "docs/benchmark.md").read_text(encoding="utf-8")
    assert "Results" in page
    assert "Old results" not in page
    assert page.endswith("Kept by hand.\n")
    # A second run reads the UTF-8 index correctly on Windows.
    assert compute.report(target, sheet, "Synthetic reviewer — test", final=True) == report
    raw_measurement = target / "a--b.measurement.json"
    (target / "measurements.json").write_text(
        json.dumps({"a/b": json.loads(raw_measurement.read_text())}), encoding="utf-8"
    )
    raw_measurement.unlink()
    assert compute.report(target, sheet, "Synthetic reviewer — test", final=True) == report
    sheet.write_text(filled.replace("| true |", "| false |"))
    with pytest.raises(ValueError, match="Published labels changed"):
        compute.report(target, sheet, "Synthetic reviewer — test", final=True)
