"""Pilot reviews preserve blank labels, pinned inputs and self-reported adoption decisions."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import maintainer_pilot as pilot
from tests.fixtures.build_fixtures import build_fixtures


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot_repo: Path) -> Path:

    def freeze(folder: Path, **kwargs: Any) -> None:
        folder.mkdir()

    monkeypatch.setattr(pilot, "freeze", freeze)
    folder = tmp_path / "pilot"
    pilot.prepare(pilot_repo, folder, "https://github.com/acme/cli", limit=2)
    return folder


@pytest.fixture(scope="module")
def pilot_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pilot-repos")
    build_fixtures(root)
    return root / "py-cli"


def label(folder: Path, verdict: str = "true") -> None:
    path = folder / "labels.md"
    path.write_text(path.read_text().replace("|  |  |", f"| {verdict} | example: source reviewed |"))


def feedback(folder: Path, **values: Any) -> None:
    data = pilot.Feedback().model_dump()
    data.update(values)
    (folder / "feedback.json").write_text(json.dumps(data))


def test_blank_review_has_no_accuracy_recall_or_adoption_claim(review: Path) -> None:
    result = pilot.report(review)
    assert result["pending"] == 2
    assert result["sample_precision"] is None and result["recall"] is None
    assert result["feedback_self_reported"]["would_keep_in_ci"] is None
    assert (review / "report.html").is_file()
    assert "/blob/" in (review / "labels.md").read_text()
    assert "false positives" not in (review / "labels.md").read_text()


def test_review_requires_human_attribution_and_reports_only_the_selected_sample(review: Path) -> None:
    label(review, "false")
    with pytest.raises(ValueError, match="named reviewer"):
        pilot.report(review)
    feedback(
        review,
        reviewer="Test maintainer",
        reviewer_role="maintainer",
        human_review_attested=True,
        would_keep_in_ci=False,
        decision="disabled",
        rationale="Too noisy",
        review_minutes=10,
    )
    result = pilot.report(review)
    assert result["false_positives"] == 2 and result["sample_precision"] == 0
    assert result["sample_wilson95"] is not None and result["recall"] is None
    assert result["false_positive_categories"] == {"example": 2}
    assert "self-reported" in result["limits"]


def test_changed_scan_and_deleted_label_rows_are_rejected(review: Path) -> None:
    original = (review / "scan.json").read_bytes()
    (review / "scan.json").write_text("{}")
    with pytest.raises(ValueError, match="Frozen pilot input changed"):
        pilot.report(review)
    (review / "scan.json").write_bytes(original)
    (review / "labels.md").write_text("# Removed all rows")
    with pytest.raises(ValueError, match="missing sampled rows"):
        pilot.report(review)


def test_existing_review_is_never_overwritten(review: Path) -> None:
    with pytest.raises(ValueError, match="never overwritten"):
        pilot.prepare(review.parent / "repos/py-cli", review, "https://github.com/acme/cli")


def test_partial_labels_do_not_get_a_precision_number(review: Path) -> None:
    path = review / "labels.md"
    path.write_text(path.read_text().replace("|  |  |", "| true | src/cli.py inspected |", 1))
    feedback(review, reviewer="Test", reviewer_role="contributor", human_review_attested=True)
    result = pilot.report(review)
    assert result["pending"] == 1 and result["sample_precision"] is None


def test_preparation_does_not_dirty_the_target_checkout(review: Path, pilot_repo: Path) -> None:
    assert subprocess.check_output(["git", "-C", str(pilot_repo), "status", "--porcelain"], text=True) == ""


def test_review_notes_are_required_and_decisions_need_attribution(review: Path) -> None:
    label(review)
    feedback(review, reviewer="Test", reviewer_role="maintainer", human_review_attested=True)
    path = review / "labels.md"
    path.write_text(path.read_text().replace("example: source reviewed", ""))
    with pytest.raises(ValueError, match="source citation"):
        pilot.report(review)


def test_prepare_rejects_dirty_checkout_and_invalid_limits(
    tmp_path: Path,
    pilot_repo: Path,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        pilot.prepare(pilot_repo, tmp_path / "review", "https://github.com/acme/cli", limit=0)
    path = pilot_repo / "uncommitted.md"
    path.write_text("Not part of the pinned revision")
    try:
        with pytest.raises(ValueError, match="clean target checkout"):
            pilot.prepare(pilot_repo, tmp_path / "review", "https://github.com/acme/cli")
    finally:
        path.unlink()
