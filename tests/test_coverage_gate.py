"""Coverage gates fail for exact counts below the requested thresholds."""

import json
from pathlib import Path

import pytest

from scripts.check_coverage import main


@pytest.mark.parametrize(
    "core, docs, match, passed",
    [(750, 900, 900, True), (749, 900, 900, False), (1000, 899, 1000, False), (1000, 1000, 899, False)],
)
def test_exact_coverage_thresholds(  # noqa: PLR0913, PLR0917 - independent coverage threshold matrix.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: int, docs: int, match: int, passed: bool
) -> None:
    files = {
        name: {"summary": {"covered_lines": covered, "num_statements": 1000}}
        for name, covered in [
            ("src/docs_drift/cli.py", core),
            ("src/docs_drift/docs/pipeline.py", docs),
            ("src/docs_drift/match/engine.py", match),
        ]
    }
    (tmp_path / ".docs-drift").mkdir()
    (tmp_path / ".docs-drift/coverage.json").write_text(json.dumps({"files": files}))
    monkeypatch.chdir(tmp_path)
    if passed:
        main()
    else:
        with pytest.raises(SystemExit) as raised:
            main()
        assert raised.value.code == 1
