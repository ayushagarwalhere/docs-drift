# Optional model assessment and fixes

Rule-only scans are local. Scanned files are untrusted text/AST: the scanner never
imports their modules, executes documentation commands, starts their services,
installs their dependencies, or evaluates executable configuration.

## What leaves the machine

`--llm` or `llm.enabled = true` enables the optional model path. Live/record mode
sends the selected documentation sentence, up to six redacted source snippets of
at most 60 lines each, relative source paths and line ranges, the frozen verifier
instructions, and the response schema through OpenRouter to the configured model
provider. Rewriting additionally sends the original sentence, the authoritative
finding verdict, and its proposed correction. The API key is used only in the
Authorization header, never in prompts, logs, hashes, reports, or recordings.

Set `OPENROUTER_API_KEY` in the process environment. The tool does not read `.env`
files or store keys in its config. A first-use notice appears before the model
layer runs. Replay mode sends nothing and needs no credentials. The client routes
with `provider.data_collection = deny`; provider/OpenRouter policies still govern
live requests and ephemeral prompt caching. Source context is placed before the
claim to reuse cache prefixes, but cache hits and billed usage are reported from
provider telemetry, not assumed.

Before sending, whole secret-bearing lines are masked: recognizable OpenRouter,
Anthropic, GitHub and AWS token shapes; password/API-key/token assignments;
credential-bearing connection strings; and PEM private-key blocks. Patterns use
bounded repetitions and no nested quantifiers; private keys use a line state
machine. Tests include a 200,000-character input. Masking cannot recognize every
possible secret format. `.env` variants and `.pem`/`.key` files are excluded by
default. Configure additional `llm.redact_paths` globs to forbid sensitive paths.
Symlinks resolving outside the scan root are never retrieved.

## Prompt injection and evidence

1. Trusted instructions are loaded from versioned package resources. All repository
   text is JSON-escaped inside delimited data blocks; inner delimiters cannot close
   a block. Repository content never becomes system instructions.
2. The model receives no tools. OpenRouter is asked for strict schema output through
   endpoints supporting the required parameters. Pydantic validates the response
   again locally.
3. Every supported/contradicted response must quote exact source text from a supplied
   snippet ID. Code derives the source path and line range from the actual quote
   offset. Unknown snippets, fabricated quotes, redacted material, instructions,
   and comment-only evidence are rejected. Rejected claims stay `unverifiable`.
4. Instruction-like phrases in source are flagged. They do not prevent retrieval,
   but reduce model confidence and add a note. Independently, code-decided findings
   are excluded from model verification, and the merge function cannot override them.

`tests/fixtures/injection` and a deliberately attacked synthetic recording test a
fake model that follows a hostile comment. Its citation is rejected. This guards
the output boundary; it is not a claim that a language model is immune to injection
or that literal citation alone proves a behavioural interpretation.

## Reproducibility and spend

`DOCS_DRIFT_LLM_MODE` selects `live`, `record`, or `replay`. Recordings contain the
request hash and schema-validated response/usage, not the request or key. Responses
can include short source quotes, so review recordings before sharing them. Checked-in
`provenance: synthetic` fixtures are hand-authored doubles, not live-model results.
Replay misses name the hash and fail with a re-recording hint. Prompt changes require
a new prompt filename and new recordings. `scripts/record.py --prune` only removes
stale live verification JSON for the current prompt version after all current
recordings succeeded. Synthetic attack fixtures, historical prompt versions,
rewrite recordings and unrecognized files are preserved.

Concurrency is bounded; 429/529 responses retry with exponential backoff under a
total timeout. The budget reserves conservative input/output costs before each new
call so concurrent work cannot reserve the same budget. Unknown pricing fails
closed; missing telemetry is charged at the reservation estimate. Reservations can
stop calls earlier than actual billing would require. A local estimated budget is
not a replacement for an account spending cap. Tokens, cache reads/writes, cost,
latency, and replay labels remain visible in the report. Rates and the default model
live in `llm/pricing.py`.

A total timeout can cancel an in-flight request before provider usage arrives.
Token/cost totals then describe completed responses and conservative charges for
reported failures; billing for the cancelled request is unknown. Its reservation
still prevents further calls from using that scan's reserved budget. Check the
provider's account meter for final billing after timeouts.

## Applying fixes

`fix` previews diffs. Only `--write` applies changes, with confirmation per file;
`--yes` explicitly skips confirmation. `--patch` only creates a patch artifact.
Edits are constrained to exact original spans, applied bottom-up, and checked
against the scan's file hash before preview and again before atomic replacement.
A changed file triggers a new scan and review. UTF-8/BOM and CRLF are preserved;
unsupported undecodable files are left for manual review. Missing names without
certain replacements are suggestions only. A removed flag carries a reviewer note
because dropping an option such as `--dry-run` may change the example's meaning.

Model rewrites are limited to one sentence. Unknown new identifiers, missing rename
targets, multiple sentences, and newly extracted non-OK claims reject the rewrite.
Model edits stay separately labelled. Static checks establish only supported code
claims, not runtime success.
