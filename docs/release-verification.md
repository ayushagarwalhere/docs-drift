# First-release verification

18 October 2026. **Preparation is not publication.** This table distinguishes
local checks from checks that require an owner-approved public release.

| Check | Actual status | Evidence / next step |
| --- | --- | --- |
| Full suite on a clean validation clone | PASS | 560 passed, one dependency deprecation warning, 436.73 s; installed 0.1.0rc1 wheel. Validation commit 2ade09e32a807aa3303716ecbd9ab0e9b495164b, not remote main. Subsequent SARIF help-link change passed focused report tests. |
| Coverage gates | PASS | Product package + scripts: 6272/7373; docs: 1146/1261; match: 314/333. Exact-count gates, no exclusions added to reach them. |
| Built wheel outside src tree | RECHECKING | Fresh-directory checks found an error-path display issue and on-demand grammar download timeout. Diagnostics were fixed; dedicated JS/TS grammar wheels now replace runtime downloads. Final isolated full suite pending. |
| Archive and metadata checks | PASS | Actual wheel/sdist inspected; Twine strict passes; relative README images become absolute GitHub URLs. RC version is 0.1.0rc1. |
| VS Code VSIX | PASS, packaged only | Local VSIX built; no Marketplace publication or native editor recording claimed. |
| Workflow lint | PASS | Checksum-verified actionlint 1.7.12, all workflows; shellcheck/pyflakes integrations disabled. |
| Real PyPI pipx install + public scan | BLOCKED, not run | PyPI project returned 404 before publication. Run after approved stable publish; never silently install a similarly named project. |
| TestPyPI install matrix | PREPARED, not run | 27 OS/Python/installer jobs in release.yml; requires owner UI setup and RC publication approval. |
| GitHub Action on test repo + PR comment | PREPARED, not run | No test repo/PR/comment published; see action-release-check.md for separate approvals. |
| Production smoke script | BLOCKED, not run | No approved deployed public URL. Local tests do not count as production proof. |
| Own-repo zero drift | PASS | scan/insights/impact over available history/fix preview/check; check --quiet exits 0. HEAD~10 unavailable in the seven-commit source history. No new baseline hides findings. |
| README links | PENDING publication recheck | Initial 26 links/images passed. The newly added real PyPI link necessarily returns 404 until the first publish; demo/video remain explicit slots. Recheck the final README after publication. |
| LICENSE, SECURITY.md, CONTRIBUTING.md | PASS | Present in working tree; archive contents checked separately. |
| Full Git history secret scan | PASS with documented false positives | Gitleaks 8.30.1: original 7 commits clean. Full validation refs (9 commits) clean with a narrow exception for recomputed group_key SHA256 identifiers. All 244 initial matches were verified derived hashes; a positive-control key is still detected. |
| Uptime monitor active through 6 Nov | BLOCKED, not active | Workflow and date window prepared; needs approved hosting URL, repository variables and an observed successful run. |
| Private physical phone/laptop checks | OWNER CHECK, not run | Check on a real phone and laptop; no emulated-device claim. |
| Video duration and captions | OWNER CHECK, not run | Shot list totals 113 s; final exported/uploaded video not yet supplied. |
| Weekly Insights cards | OWNER CHECK | Actual challenge start date and prior upload receipts still needed. |

Known limits remain: no human accuracy labels; speed targets fail; robustness
completed 29 scans with one timeout; VS Code helper capture previously failed;
Linux container execution and public infrastructure are not verified locally.
The archived r4 benchmark source is separate from this release-preparation
snapshot; never rewrite its measurements as results for changed detector rules.

The first clean-clone suite passed 558 tests but failed the 85% coverage gate at
84.45%. A real stale-file snapshot bug and its regression coverage were fixed;
the final 560-test run and exact counts above supersede that failed gate. No
coverage threshold was lowered. The remaining gaps include live host/network
integration, native editor behavior and interactive operational scripts.

Docker CLI is installed here but its daemon is unavailable. Linux image build,
anonymous GHCR pull and production execution remain unverified until approved CI
publication/hosting. Do not turn any blocked row into a pass based on local mocks.
