# CI diagnosis — 22 October 2026

Inspected authenticated job logs for main commit
`3e0b885bdeaf0cd24e3a27efee0d6621f2ef9b9a`, the revision in the screenshot.
Raw logs stay in the ignored local `.docs-drift/ci-diagnosis/` directory.

| Check | Actual failure | Fix |
| --- | --- | --- |
| Linux / Python 3.11 and 3.13 | mypy: `Module has no attribute CREATE_NEW_PROCESS_GROUP` in web Git/worker launch code | Use explicit `sys.platform == "win32"` branches so Linux typing does not analyze Windows-only constants |
| Windows / Python 3.11 and 3.13 | `test_help_renders` and `test_default_repo_is_current_directory` compare styled output as plain text | Strip ANSI styling before textual assertions; retain real CLI help/error checks |
| CI dogfood | own-repository check exits 1 on false positives | Require matching changed-line structure for rename evidence; clearly describe the debug log as generated |
| Documentation build | MkDocs and its preceding tests pass; the subsequent own-repository check exits 1 | Same scanner/doc fixes as dogfood; retain strict MkDocs validation |
| Documentation publish | Skipped after build fails; also guarded by `DOCS_DRIFT_PAGES_ENABLED` | Expected gate behavior; do not enable publishing just to make a skipped check green |
| History secrets | Passed | No weakening or removal |

Source runs: [CI](https://github.com/ayushagarwalhere/docs-drift/actions/runs/39164123130)
and [Documentation](https://github.com/ayushagarwalhere/docs-drift/actions/runs/39164123144).

## Scanner and diagnostic fixes

The self-scan incorrectly promoted `uv run just setup` into a high-confidence
rename to `docs-drift`, and `--help` into a rename to `--repo`, because the old and
new names occurred somewhere in the same large diff hunk. History now requires
the changed lines to have matching structure after replacing the relevant name;
a matching Python field assignment may also change its literal default.
Unrelated additions cannot corroborate a rename; removal attribution remains
available. This deliberately abstains on more complex simultaneous rewrites.
The fixture answer key and report snapshots were preserved; the existing
rename-plus-default-change case remains recognized.

Git pickaxe arguments are joined (`-S<token>`) so a literal token such as
`--help` cannot be interpreted as Git's help option. The generated debug-log path
is no longer described as an existing repository file. Thresholds, benchmark
labels, fixture answer keys and failing-check semantics were not relaxed.

Failed quiet checks now emit their report, including when the global quiet
option captures stdout. Both self-check workflows explicitly request readable,
uncolored output, so future failures will explain their findings.

## Additional failures found during local verification

- A shutdown race could cancel the await on a cleanup thread, returning before
  deletion finished. Cleanup is now shielded and awaited even during shutdown.
  A regression test pauses cleanup while closing the queue and verifies that
  shutdown waits and removes the workspace.
- The audit reported `PYSEC-2026-4320` for locked `certifi 2026.7.22`. Only certifi
  was updated in the lockfile, to fixed version `2026.10.19`. The subsequent
  dependency audit reported no known vulnerabilities.
- Corrected formatting in the recently edited smoke script. Added integration
  checks for the deployment smoke flow and for preserving precomputed public
  reports and synthetic labels when rebuilding web examples.

These are local fixes. Existing GitHub check results belong to the old commit
and will not change until the reviewed changes are committed and pushed. The
repository's CONTRIBUTING.md requests review and a proposed commit message,
rather than an automatic commit.

Suggested commit message: `fix: repair cross-platform CI and self-scan evidence`

## Final local validation

- Full suite on Windows/Python 3.13.7: **566 passed**, no failures or skips.
  The existing Starlette TestClient deprecation notice remains a warning.
- Overall coverage: **6391/7412 (86.23%)**, above the unchanged 85% gate.
  Extraction and matching gates also pass at their unchanged 90% targets.
- Full-project mypy passes with both `--platform linux` and `--platform win32`.
  This checks platform typing; actual GitHub runner execution still needs a push.
- Ruff lint and formatting, all configured pre-commit hooks, deptry, dependency
  audit, strict MkDocs build and Actionlint pass.
- Own-repository check exits 0 with zero qualifying drift failures. Lower
  confidence and unsupported findings remain visible; no blanket suppression,
  baseline, threshold reduction or fixture-key change was introduced.
- Wheel and sdist build and pass the release content contract. Local artifacts:
  `.docs-drift/ci-dist/`. Nothing was uploaded or published.
- Exact test results: `.docs-drift/ci-diagnosis/final-tests.xml`;
  coverage: `.docs-drift/coverage.json`.
