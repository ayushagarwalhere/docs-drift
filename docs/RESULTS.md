# Hardening results - 18 October 2026

Measured on Windows-11-10.0.26200-SP0, Python 3.13.7.
The detector used for the frozen 12-repo run is docs-drift 0.1.0, local commit `d6e96eb92bde9632b6f0faf69bcbbb508ef57717`, with uncommitted changes archived in that run.
Actual source SHA-256: `da3599d3d72ac498d33d3da3114ee7dbc3c7050846ab74b9c61fcd1558a08673`.

**Performance targets are not met.** No failures have been rounded into passes. Human accuracy remains unknown: 511 review rows are blank, including 151 precision rows and 360 recall-audit rows.

## Target, actual, command, outcome

| Check | Target | Actual (unrounded) | Command | Outcome |
| --- | --- | --- | --- | --- |
| Warm py-cli | <1 second each | [0.9259763000300154, 1.317558899987489, 1.2037581999902613] | python -m benchmark.performance --part small | FAIL |
| Warm node-tool | <1 second each | [2.0332336999708787, 2.2334456000244245, 1.4522138999891467] | python -m benchmark.performance --part small | FAIL |
| Warm fastapi-svc | <1 second each | [3.668002400023397, 2.415994099981617, 2.7161269999924116] | python -m benchmark.performance --part small | FAIL |
| Warm clean-repo | <1 second each | [1.4964843000052497, 2.2283157000201754, 2.398411499976646] | python -m benchmark.performance --part small | FAIL |
| Flask cold, history included | <20 seconds | 23.668344800011255 | python -m benchmark.performance --part small | FAIL |
| Flask warm, history included | <3 seconds each | [7.747899400012102, 11.020269100030418, 9.021172699984163] | python -m benchmark.performance --part small | FAIL |
| Warm impact, Flask HEAD~1 | <5 seconds each | [11.623999899951741, 10.612182500015479, 8.086736400029622] | python -m benchmark.performance --part small | FAIL |
| Next.js cold; 34271 tracked files | >5,000 files; <90 seconds | 483.7922437999514 | python -m benchmark.performance --part large --large-repo next | FAIL |
| Next.js peak traced Python allocations | <1,000,000,000 bytes | 133127735 | python -m benchmark.probe .docs-drift/repositories/vercel--next.js/repository --docs-only --exclude-examples --profile --memory --out .docs-drift/performance/next-instrumented.json | PASS |
| Coverage: package + scripts | >=85% | 6040/7102 | pytest --cov=docs_drift --cov=scripts; python -m scripts.check_coverage | PASS |
| Coverage: src/docs_drift/docs/ | >=90% | 1144/1261 | pytest --cov=docs_drift --cov=scripts; python -m scripts.check_coverage | PASS |
| Coverage: src/docs_drift/match/ | >=90% | 314/333 | pytest --cov=docs_drift --cov=scripts; python -m scripts.check_coverage | PASS |
| Full Windows suite | all tests pass | 540 passed; 1 dependency deprecation warning | python -m pytest --cov=docs_drift --cov=scripts --cov-report=json:.docs-drift/coverage.json --basetemp .docs-drift/test-final-all | PASS |
| Robustness sweep | 30 completed; no crashes | {'done': 29, 'timeout': 1}; 0 observed scan crashes | python -m benchmark.run sweep --run 2026-10-18-robustness-r2 --timeout-seconds 600 | FAIL (incomplete) |
| Own-repo check | exit 0; no high-confidence drift | 0 | python scripts/dogfood.py | PASS |
| Impact over last 10 commits | 10 commits available | 7 commits; HEAD~10 exits 1; earliest available commit succeeds | python scripts/dogfood.py | UNAVAILABLE |
| Fixture canonical JSON | byte-identical repeated scans | all 4 fixtures passed cold/warm checks | pytest tests/test_hardening.py -k json_byte_identical | PASS |
| Real-repo canonical JSON, Flask | byte-identical fresh processes | 2 files; 1861839 bytes each; PYTHONHASHSEED 1 and 991 | docs-drift scan --repo <pinned Flask> --format json --output <file> --no-color | PASS |
| Module docstrings | none missing | 0 missing / 104 modules | AST docstring audit | PASS |

## Measurement boundaries

Scan timings measure run_scan wall time, including history; they exclude interpreter/import startup and report serialization. Cold clears application index/history caches, not the OS page cache. All three warm observations are shown; a target passes only when every warm observation passes. The desktop also ran read-only validation/sweep work; this is not an isolated hardware benchmark.
Next.js document scope is docs/**/*.md, docs/**/*.mdx and docs/**/*.rst. Source excludes packages, examples, tests, test, node_modules and generated cache directories; remaining root/Turbopack sources are indexed. The repository has 34,271 tracked files.
tracemalloc covers Python allocations during run_scan, excluding imports, native allocator memory and report serialization. The instrumented run also includes cProfile overhead; its runtime is not substituted for the normal cold timing.
FastAPI was an earlier diagnostic candidate, but has only 3,187 tracked files at its pin. Its 416.28653749998193-second cache-cold result and 97,170,786-byte traced peak are retained as diagnostics, not credited as the >5k-file target.
Earlier under-load observations are retained in generated `.docs-drift/performance/small-under-load-r3.json`. Raw observations, JSON hashes and profiles are generated under `.docs-drift/performance/`; the committed summary is `benchmark/performance-results.json`.

## Profiling and corrections

The initial instrumented FastAPI diagnostic took 553.967127299984 seconds. Its top matching path was symbol-name matching (136.621 cumulative seconds; normalize_name 105.204), followed by rename ranking (52.901); per-document project-command discovery also consumed 28.164 seconds. These timings include profiler overhead and precede correctness changes, so they are not an apples-to-apples speedup claim.
Corrections: use the index's normalized name/alias maps; cache rename tokenization and rankings per scan; discover project commands once per scan; reuse parsed document lines; share one repository path lookup across package indexes. The monorepo fix removes repeated expansion of every repository path for each package. Regression tests check the shared scope and existing fixture verdicts. The later Next.js target still fails.
The completed Next.js cProfile hot spots are retained in [the profile output](performance-profile.txt). Repeated Workspace.package_for calls consumed 709.006 cumulative seconds under combined profiling, and documentation discovery consumed 185.367 seconds. These identify future optimization work; the frozen r4 detector has not been changed after measurement.

## Dogfood, Windows and determinism

Own-repo snapshot has 7 commits. scan, insights, impact over all available history, fix preview and check were run against an isolated staged snapshot in a path containing spaces. The user's index was not changed. check --quiet exits 0 and the fix preview is empty. HEAD~10 is unavailable and returns a friendly exit 1; the earliest available commit was used for the supplemental impact result. Company Brain is a separate, unscanned project; no findings are invented for it.
Tests cover CRLF documents, committed blame despite newline conversion, forward-slash report path fields, paths containing spaces and long source paths. Git blame now reads HEAD instead of synthesizing a time-varying uncommitted author. The Flask fresh-process JSON SHA-256 is `1477de71040518fe7ccc0e587c5ab1e5782c94610aefe3f63de195a163762a07`.

## Quality and limits

Strict mypy covers src, scripts, tests and benchmark; Ruff check/format and deptry pass. pip-audit found no known vulnerabilities in the installed environment (editable project skipped). The unused FalkorDB dependency was removed; lsprotocol is now an explicit LSP dependency. No blanket typing ignores were added.
Coverage counts combine the full suite with focused follow-up checks. Coverage scope is the product package and scripts; benchmark statistics/reporting have separate Wilson, weighted-audit, blank-label, attribution and immutable-final-label tests. Under-covered areas include live network/host integration, native editor behavior, LSP notification adapters exercised in child processes, JavaScript export variants and interactive QA scripts. The corpus sweep is a crash diagnostic, not a correctness test or a substitute for human labels.
The 30-repository sweep finished with 29 completed scans, one timeout (n8n), and zero observed scan crashes. This is incomplete, not a 30/30 robustness pass. Initial Next.js and Cherry Studio 600-second timeouts are retained; both completed on retries with 1,800-second allowances. Next.js took 1470.5578281999915 seconds with full source indexing and no history, a different scope from the performance target. n8n timeout cleanup stalled and required manual termination after 1870.967425584793 seconds. Outcomes and warning examples are retained in `benchmark/runs/2026-10-18-robustness-r2/RESULTS.md`. Timeouts are never counted as completed scans.
MkDocs strict build and local browser navigation/overflow checks pass. The source README is rendered on the local docs home page; screenshots are retained in examples/readme-rendered.png and examples/docs-site.png. Actual CLI replay and graph assets are in docs/assets.
Actual VS Code recording remains blocked: the Windows helper returned 'failed to activate captured window' after a refreshed retry. No simulated editor GIF is supplied. Linux production-container execution and public hosting remain pending; Docker was not available locally and the user explicitly requires confirmation before hosting resources are created.
