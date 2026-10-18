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
