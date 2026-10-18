# Verification history ? 18 October 2026

## Automated checks

- Full regression run: **425 passed**, **82.72% coverage**, exceeding the 80% gate.
  Command: `.venv/Scripts/python.exe -m pytest -q --cov=docs_drift --cov=scripts --basetemp=.pytest-tmp-release`.
- Subsequent focused checks after the final CLI/editor changes: **44 passed**.
  These cover setup, formats, schemas, snapshots, separate docs, workspace scopes,
  watch dispatch, and the actual pygls stdio exchange. Ruff and strict mypy pass.
- A wheel was built and checked for the HTML template, vendored Cytoscape, and
  license, so installed-package reports retain their offline assets.
- A real piped CLI scan exited 0, decoded as UTF-8, and contained no escape bytes.
- HTML rendered for all four fixtures. Installed Edge, driven through Playwright,
  opened py-cli locally in light and dark at 1440×1050 and 390×844. All four views
  had no horizontal overflow or JavaScript errors. The graph, verdict filters,
  j/k/Enter navigation, insights switching, and theme toggle were exercised.

Screenshots are in `examples/py-cli-{desktop,phone}-{light,dark}.png`.
Reproduce them with `python -m scripts.build_examples` followed by
`python -m scripts.check_html`. The py-cli HTML is about 400 KB, including
373,734 bytes of vendored Cytoscape.js 3.30.4.

## Performance

`python -m scripts.bench_scan` creates a fresh synthetic 20-package workspace
with 2,000 Python source files and package-local README claims. It times full
scans, excluding fixture creation, and uses one cache at the repository root.

| Run | Cold | Warm |
| --- | ---: | ---: |
| Before optimization | 117.312 s | 85.586 s |
| Final measurement | **21.277 s** | **6.825 s** |

Full stage measurements are saved in `examples/monorepo-benchmark.json`, with
the initial run in `examples/monorepo-benchmark-before.json`. Repeated pathlib
package comparisons were replaced by normalized path-prefix checks and one
package-index map. Python source/AST snapshots are shared across indexers during
each build and cleared at build boundaries. Measurements are local Windows
wall-clock times and will vary with disk activity.

## Recordings and the manual editor limitation

`python -m scripts.record_demo` captured actual scan → explain → fix output into
`examples/py-cli-demo.cast`. The approximately 8–12 second recording previews
patches and does not modify the fixture. Replay with
`asciinema play examples/py-cli-demo.cast`.

An isolated VS Code extension development host was launched on a copy of the
py-cli fixture. Computer Use returned **“Computer Use app approval timed out”**
before the editor could be inspected or edited. I therefore did **not** observe
the squiggle, a UI quick fix, its disappearance, or UI latency, and no editor
screen recording was captured. Those manual checks remain outstanding.

The automated pygls transport test did observe a DD002 diagnostic for `--port`,
the “Replace --port with --listen” action, a definition location in cli.py, and
removal of the stale-flag diagnostic after an unsaved didChange notification.
This verifies the protocol behavior, but is not a substitute for the manual
VS Code visual check.

The SARIF help URI namespace in generated reports is `https://docs-drift.dev/docs/rules/`.
The corresponding rule text is included under `docs/rules/`; publishing those
docs to that namespace is a separate hosting step.

Proposed commit: `Polish CLI reports and add workspace and editor feedback`.
