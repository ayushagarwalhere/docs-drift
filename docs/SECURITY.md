# Security review: public demo, scanner and CI

Reviewed 18 October 2026. This is a focused source review with adversarial tests,
not a penetration-test certification. No public host has been deployed yet.

## Findings ranked by severity

1. **High — source file boundary and parser exhaustion. Fixed.** The source walker
   now excludes real `.env` files, external symlinks, invalid/control-character
   paths, files over 1 MB and deeply nested configuration. Documentation parsing
   bounds input/line length and Markdown nesting. The hosted worker checks all
   checked-out files before indexing and has total/clone time, CPU, memory and
   download limits. Safe `.env.example` templates remain available to the CLI;
   the public checkout excludes all `.env*` paths. Source/JSON text is never run.
   Long adversarial fuzz cases also exposed quadratic default-phrase matching
   across repeated opening parentheses. The pattern now stops at nested
   parentheses; the failing 6,000-character input is an explicit regression case.
   Markdown also rejects lines with over 128 opening link/parenthesis delimiters
   before invoking markdown-it, since dense malformed inline markup can exceed
   the per-call CPU budget even within the general line-size limit.
   Packaging metadata readers now apply the same bounded, non-symlink policy;
   they previously read root manifests directly outside the guarded source walk.
   Public jobs bypass all repository-supplied index caches, preventing forged
   cached symbols from overriding parsed evidence. Local cache paths resolving
   outside the repository are neither read nor written. Both have regression tests.
2. **Medium — Git timeout could stall on an inherited pipe. Fixed.** A real
   partial-clone scan exposed a lazy fetch retaining stdout after its parent
   timed out. History output is now file-backed, limited to 8 MB, with a 12-second
   per-call deadline; public workers are disposable and killed on the overall
   deadline. History traversal is explicitly bounded before pickaxe filtering,
   rather than only limiting the number of matching commits.
   Live Windows HTTP scans also revealed read-only Git packfiles left behind by
   ignore-errors cleanup. Scoped cleanup now retries only contained paths with
   writable permissions, has a regression test, and clears owned orphan UUID
   workspaces at startup. Real scan directories were verified empty afterward.
3. **Medium — secret-bearing report/debug text. Fixed.** Every report serializer
   receives a redacted copy; debug tracebacks are scrubbed. Public results also
   remove clone paths and author identities. Reports in all seven formats and
   last-error.log have regression tests. Pattern masking cannot recognize all
   credentials; never submit sensitive repositories or use real secrets in docs.
4. **Medium — Markdown can notify users or load tracking images. Fixed.** Untrusted
   inline text escapes mentions, image/link delimiters, HTML and backticks.
   Evidence is fenced using a delimiter longer than any repository backticks.
   HTML templates autoescape; embedded graph JSON escapes `<`, `>` and `&`.
5. **Medium — public-service abuse boundaries. Implemented before exposure.** Strict
   GitHub-only URL/ref validation, disabled redirects/credentials/hooks/protocols,
   bounded jobs/storage/graph/body, exact Origin/CORS checks, and rate limiting
   form the new endpoint's boundary. No arbitrary clone endpoint was released.
6. **Low — mutable CI action references. Fixed.** Workflow action versions are
   commit-SHA pinned. Dependabot and pip-audit are enabled. PyPI publishing uses
   OIDC; deployment secrets are confined to a main-push-only production job.

Separately, manual example review found missing Click option indexing, which
created false positives. Click flags/aliases/environment declarations are now
indexed statically and regression tested. This is a correctness fix, not a
security vulnerability.

## Threat model

| Threat | Impact | Mitigation | Test/evidence |
| --- | --- | --- | --- |
| Huge or nested docs/source/JSON | CPU/RAM exhaustion | 1 MB public files, 8192-character lines, depth 64 config preflight, Markdown maxNesting 20, disposable worker, Linux 512 MB address limit | `test_public_security.py`; worker preflight |
| Regex backtracking | Slow scan | Bounded parser input; secret regex repeats bounded; literal identifiers re.escape'd for history; Hypothesis fuzz with 100 ms CPU budget per extractor call | `test_extractor_fuzz`, existing long secret input tests |
| Escaping symlinks/unsafe filenames | Host file disclosure | Root containment, no symlink checkout, 240-character/control-character guards; submodules not checked out | `test_symlink_escape_and_env_exclusion`, `test_sparse_checkout_never_selects_symlinks_env_or_submodules` |
| Git option/config injection | Command execution | Argument lists, conservative refs (reject leading dash), canonical URLs; no shell, global/system Git config/credential helpers disabled; no hooks, LFS smudge, submodules, external diff or textconv | `test_url_normalization_and_ref_injection`; source subprocess review |
| SSRF/redirects/cache aliases | Internal network requests/poisoning | Exact ASCII github.com HTTPS grammar, no credentials/ports/IPs/queries; no HTTP redirects; lowercase owner/repo and resolved SHA cache keys | `test_ssrf_table`, worker cache protocol test |
| Concurrent clones/disk growth | Resource exhaustion | Semaphore 2, queue 32, clone 200 MB/20k paths/60s, scan 120s, memory/disk result limits and 24h TTL | timeout/rate/store tests; real clone measurements |
| Large graph/report | Browser/server memory pressure | Graph at most 1000 nodes; web graph 200; 40 web findings, shortened excerpts; full result download capped at 10 MB | API graph tests and measured page weight |
| XSS/SARIF/PR text | Script execution/viewer tracking | Jinja escaping, JSON data escapes, vendored JS, CSP hashes, Markdown mention/image escaping, SARIF messages are JSON text, safe HTTP GitHub links | web XSS test plus report security tests |
| Model prompt injection | False verdict or data disclosure | Public model path disabled; optional CLI uses delimited data/no tools/schema and exact evidence validation | existing synthetic injection/replay tests |
| Credential/log leakage | Account compromise | No `.env` reads; worker env allowlist excludes keys/proxies/SSH agent; redacted serializers/logs/model requests, no access logs/IP logging | seven-format redaction + log/env tests |
| Fork PR/write token exposure | CI compromise | CI contents:read; no pull_request_target; production job requires successful main push in same repo; checkout credentials not persisted in deployment/release/monitor | reviewed `.github/workflows` |
| Supply chain/publishing | Malicious release | Locked Python deps, SHA actions, Dependabot, pip-audit, protected OIDC publishing environment | local pip-audit: no known vulnerabilities |

All scanner/indexer subprocess calls were reviewed: they invoke Git with argument
lists for metadata/text, never documentation commands. A search found no
`shell=True` or `os.system` in `src`. The custom GitHub Action implementation is
not present in this checkout, so its masking/fork behavior cannot be certified;
only the workflows supplied here were reviewed. Do not claim Action key masking
has been tested before that Action exists.

## Residual risks and launch checks

- Regex review/fuzzing is sampled, not a mathematical guarantee for every pattern.
  Some extractors contain broad searches; public size/time limits are defense in
  depth. Fuzzing currently covers Markdown/inline/default extraction; additional
  framework-specific grammars remain useful future coverage.
- No application sandbox is a substitute for a patched Git/parser/runtime and
  a container/VM memory/disk limit. Linux process limits need verification in the
  production image; the local Docker daemon was unavailable. Windows lacks the
  Unix address-space limit and relies on process deadlines during development.
- Partial clones can fetch historic blobs during history queries. Date-window
  fetch bounds and result limits do not guarantee exact network bytes over the
  whole scan; a watchdog enforces the repository disk cap through history as
  well as cloning, but sampling is not a filesystem quota. Use a constrained
  host disk/container and monitor usage. Two jobs can briefly consume roughly
  400 MB plus caches/output. A dedicated disk quota would be stronger isolation.
- Rate limiting is per process and IP, not an identity or distributed DDoS
  defense. Configure trusted proxy addresses correctly. The single-process
  deployment is intentional; an internet attacker with many IPs can still fill
  the bounded queue. Health/examples remain available when cloning is paused.
- GitHub HTTPS is the sole clone destination; DNS/TLS trust still depends on the
  host resolver/CA store. Git redirects are disabled, including legitimate repo
  renames. Use the canonical repository URL.
- Static missing findings are hypotheses, especially at low confidence; the
  manual example notes identify exactly which statements were verified. The
  historical example is already fixed upstream. No broader claim of repository
  correctness or safety is implied.
- CI workflow definitions have not run on GitHub yet. No GitHub Action package,
  PyPI trusted publisher, hosting credential or production environment was created.
  The owner must enable private vulnerability reporting and environment policies.

## What leaves your machine

Rule-only CLI scans make local Git calls. Public web scans send a public GitHub
URL/ref to the server, which contacts GitHub using anonymous HTTPS Git and renders
source evidence to anyone holding the random permalink for up to 24 hours. Public
repository text and metadata may be cached on the server. Baked examples are
committed assets and do not expire. Public workers receive no model/API token and
make no model requests. Structured logs contain public repo URL, resolved SHA,
duration, verdict counts and a failure category; they omit IPs/source/tracebacks.

See the root [SECURITY.md](security-policy.md) for private reporting instructions.

## Optional model assessment and fixes

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
live in `src/docs_drift/llm/pricing.py`.

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
