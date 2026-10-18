# Replay fixtures

Checked-in responses with `provenance: synthetic` are hand-authored test doubles,
not outputs from a live model. They exercise transport replay, evidence validation,
the independently specified prose timeout expectation, and an attacked response
that cites a malicious code comment. They cannot establish live-model accuracy.

The request hash covers model, prompt filename, frozen instructions, source
context, claim, and purpose. Prompt or fixture changes invalidate recordings.
Use `uv run python scripts/record.py` with `OPENROUTER_API_KEY` to record genuine
OpenRouter responses. `--dry-run` sends nothing; `--prune` removes stale live
verification recordings for the current prompt version only after all current
requests were recorded successfully. Synthetic, historical and rewrite fixtures
are retained. To record sentence rewrites too, run the preview-only `fix --llm`
command with `DOCS_DRIFT_LLM_MODE=record`. Review recordings for
privacy before committing them. Responses never contain the API key or raw request.
