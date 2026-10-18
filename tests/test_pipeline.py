"""Integration tests for extraction merge/filter behavior without executing repository code."""

from pathlib import Path

from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims


def test_scan_deduplicates_with_locations_and_filters_ignored_claims(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Usage\n\nThe `--port` option is documented.\n\n## Another\n\nThe `--port` option is repeated.\n",
        encoding="utf-8",
    )
    result = scan_claims(tmp_path, Config(ignore=["cli_flag:--port"]))
    assert result.claims == []
    assert result.sections
    assert result.timings.keys() >= {"discover", "parse", "inline", "blocks", "defaults", "merge_filter"}


def test_scan_suppression_retains_claim_and_reason(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Usage\n\n<!-- docs-drift-ignore-next-line -->\n`--secret`\n", encoding="utf-8"
    )
    result = scan_claims(tmp_path, Config())
    assert result.claims == []
    assert [(item.claim.value, item.reason) for item in result.suppressed] == [
        ("--secret", "docs-drift-ignore-next-line")
    ]


def test_scan_counts_historical_claims_and_suppresses_historical_finding(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Usage\n\nThe flag is `--listen`.\n\n## Migrating from v1\n\nOld name `--port`.\n", encoding="utf-8"
    )
    result = scan_claims(tmp_path, Config())
    assert any(claim.value == "--listen" for claim in result.claims)
    assert all(claim.value != "--port" for claim in result.claims)
    assert result.historical_ignored >= 1
