# Evaluation results

Append dated result blocks. Do not edit historical results; add a new block
when the implementation or measurement procedure changes. Never alter fixture
keys or detector rules to improve a score.

## 2026-10-18

Command: `uv run python scripts/measure.py`

- Extracted claim recall: 13/13 (100.0%).
- Keyed finding verdict recall: 13/13 (100.0%).
- Keyed-case precision: 13/13 (100.0%).
- Clean-repository historical, example, and third-party near-miss leaks: 0.

Precision is limited to the explicit non-finding cases in the answer key; it
does not estimate precision on arbitrary repositories. The fixed answer key was
not modified. The measurement output itself is the authoritative record for
the exact command used.

## 2026-10-18: optional OpenRouter lane and reviewed fixes

Command: `uv run pytest --cov=docs_drift --cov=scripts --cov-report=term-missing -q`

- 405 tests passed on Windows / Python 3.13.
- Total coverage: 85.34%; the threshold remains 80%.
- Model tests used synthetic replay responses, injected clients or a mocked HTTP
  transport. No live-model accuracy, cost or latency benchmark was performed.

Command: `uv run --frozen python scripts/measure.py`

- Rule-only extracted-claim recall: 13/13 (100.0%).
- Rule-only keyed verdict recall: 13/13 (100.0%).
- Rule-only keyed-case precision: 13/13 (100.0%).
- Listed clean-repo near-miss leaks: 0.
- The authorized prose timeout extension is explicitly excluded from these
  rule-only numbers (1 model-required expectation). Its key was written before
  model evaluation; existing expectations and original fixture commits remain
  unchanged. Synthetic replay tests exercise that expectation separately and
  must not be presented as live-model accuracy.

Command: `uv run python scripts/review_fixes.py`

- 11 detected findings received direct rule edits; 3 had no direct edit.
- Every emitted patch passed `git apply --check`, was applied to a temp copy,
  and its edited claims re-scanned as `ok`.
- These are raw diagnostic counts, not canonical answer-key scores: the standalone
  removed `--dry-run` flag is one no-direct-edit finding, while its enclosing
  command is fixed separately and the flag mention disappears with that rewrite.

The fixture CLI diffs can also be regenerated with
`uv run docs-drift --repo PATH fix --patch changes.diff` after building fixtures.
The default fix command previews; it never applies changes silently.

## 2026-10-18: final rewrite validation and recording-prune checks

Command: `uv run pytest --cov=docs_drift --cov=scripts --cov-report=term-missing -q`

- Final run: 406 tests passed; total coverage 85.30%, with the 80% floor unchanged.
- Added regression checks reject invented call names and incorrect default claims
  in rewrites. Recording pruning preserves synthetic attack fixtures.

Commands: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`.
All passed. `uv build --wheel --out-dir .docs-drift/dist` also passed, and inspection
confirmed both versioned prompt resources are packaged.

`uv run pre-commit run --all-files` did not complete its isolated environment
bootstrap in this sandbox and was interrupted. Ruff/format checks ran directly;
the cached upstream end-of-file and trailing-whitespace checks also passed directly.
`uv run pre-commit run --all-files` with those already-checked hooks explicitly
skipped ran the staged-file/secret guard successfully. The full bootstrap is not
being reported as a passing check.
