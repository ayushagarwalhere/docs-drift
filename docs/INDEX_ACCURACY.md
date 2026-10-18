# Index accuracy report

Date: 2026-10-18
Indexing implementation commit: `cf574a9`

## Controlled fixtures

Command: `uv run python scripts/verify_index.py`

All four fixture repositories were indexed statically. Runtime CLI `--help`, npm installation, and FastAPI `app.routes` were skipped under the project-wide rule that scan-repository code must never be executed. Consequently, true/false positive and negative runtime counts are not yet measured. The static output included 8 symbols for `py-cli`, 5 for `fastapi-svc`, 6 for `node-tool`, and 6 for `clean-repo`; these are symbol counts, not accuracy scores.

## Public README spot-check

Command: `uv run python scripts/spotcheck_index.py`

| Repository | Indexed flags | Documented flags | Exact-name overlap |
|---|---:|---:|---:|
| `httpie/cli` | 15 | 75 | 7 |
| `pallets/click` | 0 | 2 | 0 |
| `vercel/serve` | 0 | 2 | 0 |

The public check scans README/usage Markdown for `--name` tokens and intersects them with statically indexed flags. It is a coarse coverage spot-check, not precision/recall: README options can refer to dependencies or examples, and framework registration patterns are incomplete. The low coverage on Click and serve shows that these results are not ready to support missing verdicts outside recognized patterns.

## Performance

Command: `uv run python scripts/bench_index.py`

On Windows/Python 3.13.7, a cold/warm run indexed Flask in 5.144s/0.355s (1,297 symbols) and Hono in 15.552s/0.799s (1,452 symbols). Both cold runs were below the 60-second target. Timings vary by machine and checkout state.

The main known misses are dynamic CLI registration, incomplete Click/Commander framework coverage, and runtime-built or cross-module route prefixes. No fixture-specific matching rules were introduced. See [LIMITATIONS.md](LIMITATIONS.md) for their effect on users.
