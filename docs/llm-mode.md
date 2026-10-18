# Optional model mode

Rule-only operation makes no model requests. CLI `--llm` explicitly enables
selected prose verification through OpenRouter and the configured model.
Set `OPENROUTER_API_KEY` in the environment; do not put keys in source files.
The scanner never loads dotenv files.

`DOCS_DRIFT_LLM_MODE=replay` uses recordings and sends nothing; missing recordings
fail with a useful message. Live/record mode can send selected documentation,
redacted source excerpts and relative locations to OpenRouter/model providers.
Responses must cite supplied source and pass schema/citation validation. This
reduces prompt-injection risk; it is not proof of model correctness.

Use a spending cap and review prompts, provider privacy terms, logs and recordings
before enabling live mode on private code. See [security](SECURITY.md) for exact
data boundaries and known limits. Public web scans and the composite Action are
rule-only and have no model-enable input.
