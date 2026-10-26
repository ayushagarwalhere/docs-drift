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

## Triage (`--llm-triage`)

Triage is a separate, opt-in pass over rule findings. Enable it with
`--llm-triage` on `scan` or `check`, or with `triage = true` under `[llm]`.
It can only downgrade: for each rule drift finding it sends the flagged name, the
rule reason and a redacted documentation excerpt (about 17 lines, with the nearest
heading, the fence it sits in, and any lines in the document's own code blocks that
define the name) and asks whether the statement is about this project at all. A
finding becomes `unverifiable` with confidence 0.2 only when the model answers
`not_project_claim` with a category (an example the doc defines itself, another
tool's flag or config, a placeholder, a reader's own path, historical text, an OS or
runtime name, program output) and a quote that is found verbatim in the excerpt it
was shown. Invalid, uncertain, truncated, rate-limited or over-budget answers keep
the rule finding unchanged. Triage never creates findings, never upgrades one, and
never touches findings the model already decided; the run fails closed if that
invariant is ever violated.

Downgraded findings stay in every report. JSON keeps the original verdict and
confidence, category, rationale, model, prompt version and the validated quote in
the finding's `triage` field, and `llm_usage` has `triage_*` counters. The terminal
report lists them under "Downgraded by model triage".

```text
docs-drift scan --llm-triage --llm-budget 0.05
docs-drift check --llm-triage
```

The default triage model is `google/gemini-2.5-flash-lite` (override with
`triage_model`), requests are batched (`triage_batch_size`, default 8) at
temperature 0, and `triage_max_findings` (default 200) caps how many findings are
sent, highest confidence first. Triage shares `--llm-budget` with prose
verification. Excerpts that contain instruction-like text are never sent.

Measured effect is small: on agent-labelled findings from 16 public repositories it
removed 13 of 276 round-1 false positives and 1 of 19 round-2 false positives,
and kept every known true positive (17 and 4; 14 are synthetic fixture drift). See
[triage evaluation](llm-triage-eval.md).

Use a spending cap and review prompts, provider privacy terms, logs and recordings
before enabling live mode on private code. See [security](SECURITY.md) for exact
data boundaries and known limits. Public web scans and the composite Action are
rule-only and have no model-enable input.
