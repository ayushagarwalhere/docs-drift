# Organization workflow verification — 22 October 2026

Local Windows / Python 3.13.7 checks. No commit, publication or deployment is
implied, and GitHub's hosted matrix was not rerun by these local commands.

- Full regression run: **601 passed**, overall coverage **6582/7602 (86.58%)**.
  Extraction **1147/1264 (90.74%)**, matching **314/333 (94.29%)**. Command:
  `pytest --cov=docs_drift --cov=scripts --cov-report=term --cov-report=json:.docs-drift/coverage-org-verified.json --basetemp .pytest-tmp-org-verified -q`,
  with `DOCS_DRIFT_LLM_MODE=replay` and `DOCS_DRIFT_DIST=.docs-drift/org-final-dist`.
- Subsequent targeted tests after checkout-root, Git failure and Action-output
  hardening: **84 passed, 1 skipped**. That earlier targeted run omitted the
  distribution directory; the final full run above supplied one and had no skips. Command:
  `pytest tests/test_ci_and_worker.py tests/test_index.py tests/test_portfolio.py tests/test_policy.py tests/test_public_security.py tests/test_release.py -q --basetemp .pytest-tmp-org-release-final`.
- The intermediate complete run caught a documentation-discovery regression
  caused by the stricter source-listing failure. It was fixed without changing
  the existing test contract; the affected group passed **78 tests**, and the
  final complete run above passed. Baseline traceback privacy also passed its
  focused **3-test** group.
- Ruff checks and formatting passed. Strict mypy passed **151 source files** for
  both `--platform linux` and `--platform win32`; this does not substitute for a
  native Linux test run. Strict MkDocs build and workflow actionlint passed.
- Composite Action YAML parsed and its adoption inputs were verified. Actionlint
  validates workflow files; passing a composite `action.yml` directly to it is
  invalid and was not counted as a successful composite validation.
- Wheel and source archive built locally; `scripts.release.check_archives`
  passed. Release resource checks now require the portfolio template and modules.
- The self-check, run as the checkout owner, exited **0**.
  Its generated report is `.docs-drift/org-dogfood-final.json`. Running as the sandbox user first caused
  Git's ownership check to reject the checkout. Its old fallback traversed ignored
  generated trees; the process was stopped and tracked-source discovery now
  fails explicitly instead. No global Git trust exception was added.
- Portfolio scans completed on the actual Flask and Express checkout roots.
  [Results and three observed false positives](ORG-PILOT-2026-10-22.md) are recorded
  separately. No throughput, memory, accuracy or recall claim is inferred.

New targeted checks exercise baseline preservation, changed-verdict regressions,
confidence/budget boundaries, redacted warnings, invalid policy rejection,
independent Action artifact directories, manifest preflight, checkout-root versus
snapshot selection, scan-error isolation, report links, Windows UTF-8 BOM input
and the no-filesystem-fallback boundary after a failed Git listing.

The static dashboard has functional/template checks, but no connected browser was
available for visual rendering. Independent labels, default-enforcement quality,
container execution and a supported published release remain release gates in
[product maturity](PRODUCT.md).
