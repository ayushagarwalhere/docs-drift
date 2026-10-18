# docs-drift design

## Scope and safety boundary

The scanner consumes repository files as untrusted text. It may parse source into ASTs, but must never import scanned modules, run application startup, load executable configuration, install a scanned project's dependencies, or execute commands found in documentation, scripts, Makefiles, or reports. Static inspection can establish some properties of a command, but cannot establish runtime success; unsupported behavior remains `unverifiable`.

Deterministic checks own all claims they can decide. The optional LLM receives only the evidence needed to assess an otherwise undecidable prose claim. Repository text is data, not instructions. Model output is separately labeled, validated, and cannot modify code-based verdicts. Provider integration and detailed model-assessment policy are deferred.

## Data flow

```text
Discover docs
  -> Parse sections and code blocks
  -> Extract claims with documentation evidence
  -> Index code as text / AST
  -> Match claims using deterministic rules
  -> Enrich confirmed drift with git history
  -> Build graph
  -> Report findings and evidence
```

1. **Discover docs:** select documentation files within the scan scope. Inclusion, exclusion, symlink handling, and size limits need a defined policy before implementation.
2. **Parse sections:** preserve headings, section ownership, code-block language, and original line locations.
3. **Extract claims:** identify concrete assertions and retain their literal text, kind, context, and location. Do not treat every word or identifier mention as a claim.
4. **Index code:** collect declarations, relationships, and statically known defaults from supported source and configuration formats. Preserve source locations and limitations without executing anything.
5. **Match:** compare claims against the relevant index and scope. Use deterministic rules where possible; ambiguous, dynamic, or unsupported cases are `unverifiable`. Only otherwise undecidable prose claims are eligible for optional model assessment.
6. **Enrich with history:** inspect git text, patches, and metadata to identify the commit introducing a confirmed discrepancy. Consider relevant documentation changes as well as source changes; a source change alone does not establish when the docs became wrong. Current-line blame alone does not prove causation. If history is shallow, absent, ambiguous, or otherwise insufficient, report attribution as unavailable with the reason.
7. **Build graph:** connect documented claims to their evidence, resolved symbols, and supported history. Keep optional database persistence behind the graph extra.
8. **Report:** show each claim's verdict, reason, documentation location, supporting source text where available, and attributable history. Keep uncertainty visible and preserve the deterministic finding alongside any optional prose assessment.

## Claim kinds

The examples below are illustrative documentation assertions, not live fixtures or an evaluation answer key.

| Kind | One-line definition | Concrete example |
| --- | --- | --- |
| CLI flag | An assertion that a command accepts a named option. | `serve --port 8000` claims that `serve` accepts `--port`. |
| CLI subcommand | An assertion that a command exposes a named child command. | `tool export` claims that `export` is a subcommand of `tool`. |
| Environment variable | An assertion that code reads a named environment variable. | “Set `DEBUG_SQL=1` to enable SQL debugging.” |
| HTTP endpoint | An assertion that a particular HTTP method and route exist. | “Send `GET /api/users`.” |
| Config key | An assertion that a configuration field exists in a particular scope. | “Set `server.timeout` in the config file.” |
| Function name | An assertion that a named function exists in a stated module or scope. | “Call `client.fetch_users()`.” |
| Class name | An assertion that a named class exists in a stated module or scope. | “Instantiate `client.ApiClient`.” |
| File path | An assertion that a repository-relative file or directory exists. | “Edit `config/settings.toml`.” |
| Package script | An assertion that a package manifest defines a named script. | “Run `npm run build`.” |
| Makefile target | An assertion that a Makefile defines a named target. | “Run `make docs`.” |
| Default value | An assertion that a specific setting has a particular default. | “The timeout defaults to 30 seconds.” |
| Shell command | An assertion that a documented code-block command has a statically valid structure against supported declarations. | `tool export --format json` claims that the subcommand and option combination is accepted. |

A command may yield several related claims. Deduplication and precedence need a defined policy so a single defect does not produce misleading duplicate findings. Defaults must bind to a specific symbol and account for units and supported conditional behavior.

## Verdicts

| Verdict | One-line definition | Concrete example |
| --- | --- | --- |
| `OK` | Supported deterministic evidence confirms the concrete claim within the scan scope. | Docs claim `GET /users`; a supported route declaration defines `GET /users`. |
| `missing` | A supported, sufficiently complete index establishes that the claimed item is absent in its relevant scope. | Docs claim `DEBUG_SQL`, but the fully supported environment-variable lookup scope has no such variable. |
| `renamed` | Evidence establishes a mapping from the documented item to its replacement. | History shows `--port` replaced by `--listen` for the same option. |
| `default changed` | A statically resolved default differs from the documented default for the same setting. | Docs say timeout is 30 seconds; source declares 60 seconds. |
| `invalid command` | Static evidence proves a documented command conflicts with supported command declarations. | Docs invoke `tool export --bogus`; a fully supported parser declaration rejects unknown options and defines no `--bogus`. |
| `unverifiable` | Evidence is insufficient, ambiguous, dynamic, or outside the supported analysis scope. | A runtime plugin generates routes that cannot be resolved statically. |

Absence from a partial index does not establish `missing`. Similar names alone do not establish `renamed`. A shell command is never run to choose its verdict. `OK` confirms only the stated static claim, not successful runtime behavior.

## Evidence contract

Every finding retains the original documentation path, line, and exact text, the claim kind and scope, the verdict, and the reason for that verdict. Source-based findings also retain each supporting source path, line, and exact text. A negative finding records which scope was searched and why the relevant index is complete enough to support absence; it must not invent a source line for a missing item.

History enrichment retains the commit identifier, author, timestamp, and patch evidence supporting the attribution. Attribution failures are explicit and do not erase a supported drift verdict. Optional model assessments retain their provenance and evidence separately. Concrete schemas, stable identifiers, serialization formats, and privacy/retention settings will be specified before implementing the affected modules.

## Graph model

| Node | Relationships |
| --- | --- |
| Doc file | Contains sections. |
| Section | Contains claims. |
| Claim | Refers to symbols and retains evidence; may be affected by a commit. |
| Symbol | Is declared in a source file. |
| Source file | Is changed by commits. |
| Commit | Is authored by an author; may introduce a supported discrepancy. |
| Author | Authors commits. |

Unresolved claims remain in the graph without fabricated symbol links. A PR impact query starts from changed files or symbols and traverses resolved claim relationships back to sections and doc files. File-level and symbol-level impact must be distinguished so a broad file match is not presented as an exact dependency.

## Honest measurement

Fixture reports have a fixed answer key established independently of the implementation. Do not change rules, thresholds, or that key to improve an evaluation score. Report every measured value with its exact producing command, the relevant revision and fixture identity, and failures or limitations. Select metric definitions and denominators before evaluating; no performance or accuracy numbers are claimed in this foundation phase.

## Non-goals

- Spell checking, lint checking of scanned repositories, and code style advice.
- Executing scanned code, examples, commands, scripts, or report contents.
- Proving runtime behavior, resolving arbitrary dynamic code, or guaranteeing that a documented command succeeds in an arbitrary environment.
- Automatic documentation edits or automatic commits.
- Guessing renames, introducing commits, or authors when evidence is incomplete.
- Letting an LLM overrule deterministic findings or optimizing the tool against its answer key.

Ruff remains a development check for docs-drift itself; lint checking other repositories is outside the product's scope.

## Implemented foundation

The `docs_drift` package uses Hatchling metadata and a Typer entry point named
`docs-drift`. Its configuration loader validates TOML with Pydantic before command
dispatch. Source priority is explicit `--config`, repository `.docs-drift.toml`,
repository `[tool.docs-drift]`, then defaults; only the selected source is used.
Unknown keys warn and are ignored; invalid known fields and unreadable selected
files produce actionable errors. Global options are carried in invocation-local
context. All command bodies remain stubs that print `not built yet` and exit 2.
No discovery, indexing, matching, history, graph, LLM, or web behavior exists yet.

## Shared models and identity contract

`src/docs_drift/models.py` defines the shared Pydantic models, enums, and evidence
structure. `Section.historical_spans` additionally stores inclusive paragraph
line ranges, so historical-span lookup works without global parser state and
survives JSON round trips. Other fields follow the requested contract.
`Evidence` simply gives `{file, line, text}` a reusable type. Unknown model fields
are rejected to expose pipeline mistakes; configuration retains its existing
warn-and-ignore behavior for unknown keys.

Repository-relative paths are stored as forward-slash strings on every platform.
Absolute paths, drive-relative paths, and parent traversal are rejected; the
pipeline must make them relative to the repository first. Path normalization
does not read the filesystem or change case. `ScanResult.repo_path` is a host-native
`Path` for the scan root, rather than a repository-relative evidence path.

Locations are one-based; section bounds are inclusive. Heading level zero and
an empty heading trail describe a document's introductory, unheaded section.
The heading trail includes the current heading, e.g. `['Usage', 'Server options']`.
`context` is `current` or `historical`; the docs pipeline classifies changelogs,
migration, deprecated, legacy, and past-version sections and suppresses their
claims before matching. Models do not classify headings or decide verdicts.

`ids.py` provides pure `make_section_id`, `make_claim_id`, and `make_symbol_id`
helpers. IDs are required model inputs, not random values or hidden defaults.
The pipeline already owns heading context, so `Claim` needs no extra heading-path
or occurrence fields. Helpers hash compact UTF-8 JSON arrays with SHA-256, a
namespace, and identity version one. JSON boundaries avoid concatenation
collisions. Section IDs hash document path and heading trail. Claim IDs hash
document path, heading trail, kind, and normalized value. Symbol IDs hash kind,
qualified name, and file. Lines, columns, defaults, raw spans, and confidence do
not enter the IDs. A zero-based occurrence counter is appended only for
duplicates; the extractor assigns counters within each section's repeated
kind/value group. Duplicate heading trails can likewise receive section counters.
Counters are explicit inputs, never global state. Their scope and ordering must
remain consistent when extraction is implemented.

Content hashes use lowercase SHA-256. Commit hashes support Git SHA-1 or SHA-256;
short hashes must be hexadecimal prefixes of their full hashes, at least four
characters long. Commit dates and `scan_time` are timezone-aware timestamps;
`timings` maps stage names to finite, nonnegative elapsed seconds. Timestamps
are supplied by callers, never populated from the clock by the models.

Both default fields use optional strings, preserving literal forms such as
`'008'` or expressions without evaluating source. Qualifiers, frameworks,
parent links, matched symbols, suggestions, and introducing commits may be
absent rather than guessed. Every finding requires nonempty evidence and reasons.
Confidence fields are finite values from zero to one. `decided_by` records
provenance; enforcing rule precedence and LLM restrictions remains pipeline work.

`schema_version: 1` belongs on the serialized `ScanResult` envelope; nested models
do not repeat it. All models support Pydantic's `model_dump_json()` and
`model_validate_json()`. `Finding.is_drift` is a derived property and is not an
extra serialized field. `ScanResult.summary()` returns `by_verdict` and `by_kind`
counts for findings, including zero categories; it does not count unjudged claims
or compute evaluation scores.

## Documentation discovery and section parsing

`docs.discover.discover_docs(repo, config)` returns sorted `DocFile` metadata.
Configured include/exclude globs are anchored at the scan root, use forward
slashes, and support `**/` matching zero or more directories. In Git roots,
`git ls-files` supplies tracked paths; untracked files are not scanned. Tracked
paths that match Git's standard ignore rules are also excluded deliberately.
Git queries disable fsmonitor commands, hooks, pagers, and optional index writes.
Deleted, unreadable, oversized, and external/symlink files are not scanned.
Unreadable or oversized candidates warn rather than terminating discovery.

Without Git, or when Git listing fails, discovery walks the selected root,
honors root and nested `.gitignore` rules (including negations), and prunes
common VCS, environment, vendor, and scanner-cache directories. The `pathspec`
core dependency supplies Git ignore semantics. A broken Git listing warns;
ordinary non-Git directories simply use the walk. A directory inside a parent
checkout is treated as a standalone root unless it has its own `.git` marker.

Documents are limited to 2,000,000 bytes; content hashes cover the original
bytes. `read_doc(repo, doc)` decodes UTF-8 with replacement and checks that the
hash still matches discovery. If a file changes between those stages, rediscover
it rather than reporting evidence against a stale snapshot. Only configured
paths with supported extensions are read. `.md` and `.markdown` use Markdown;
`.mdx` retains the MDX format label but follows the same static Markdown parser,
without evaluating JSX or imports. `.rst` is opt-in through configured globs.
`.txt` and `.text` receive plain paragraph parsing when configured.

`docs.sections.parse_document(doc, text)` returns `ParsedDoc`: metadata, original
source text, tokens, sections, and an O(1) `line_to_section` mapping. Keeping the
original text lets later extractors recover exact spans and columns; tokens
alone can normalize away Markdown syntax. ParsedDoc is transient parser state,
not an additional serialized report envelope.

Markdown uses markdown-it-py's CommonMark parser with built-in tables enabled
and HTML rendering disabled. Fences are never executed or interpreted as
headings. ATX and setext headings use token maps for original locations. Closed
YAML front matter at the top is blanked, preserving line numbers, and receives
no section mapping. Unclosed front matter is treated as ordinary Markdown.
Content before the first heading receives `(top)`, level zero, and an empty
heading trail. Empty or whitespace-only input has no sections.

Section ranges are inclusive and partition the source at every heading, even
subheadings: a parent section ends before its next child heading. Descendants
retain their full heading trail, so historical context is inherited without
overlapping ranges. Duplicate heading trails receive deterministic section
occurrence counters. Paragraph maps with historical prose markers are saved
as `historical_spans`; the whole paragraph is suppressed, favoring false
negatives over flags on correctly documented migrations. Fenced code and code
values themselves do not trigger prose markers. `is_historical_span(section,
line)` checks section context or these paragraph ranges, within section bounds.

RST support is basic: repeated punctuation underlines and optional matching
overlines establish heading levels in first-seen adornment order. Paragraphs
and historical context are retained. This is not full reStructuredText/Sphinx
support: directives, roles, substitutions, includes, and executable extensions
are not resolved or executed.

### Claim extraction

`docs.extract_inline.extract_claims(parsed)` returns `ExtractionResult`, with
claims and a count of candidate mentions skipped because their section or
paragraph is historical. Inline code has the strongest confidence. It recognizes
flags (stripping an inline `=example` value), environment-variable spellings,
HTTP routes with normalized path parameters, file paths, known command wrappers,
and symbols only when call syntax or surrounding/API-reference context supports
that reading. A closed stoplist prevents common all-caps prose such as `HTTP`
and `API` from being mistaken for environment variables. URLs are excluded from
file-path detection. Single-dash flags receive lower confidence because they
are easier to confuse with examples.

Tables are considered only when a header names a checkable field; that header
selects the kind of the first-column value, and a `default` column attaches an
expected default. Prose rules cover explicit default phrases and explicit
backticked assignments, keeping example values separate from names. Known
commands in shell-labeled fences are parsed as text, never executed; only known
project-command shapes are emitted, which leaves third-party commands such as
`git commit --amend` and `pip install -e .` outside the project's claim set.
Explicit command labels in a sentence or subcommand heading qualify flags.
Each heuristic uses fixed lexical evidence and confidence; there is no answer-key
feedback or threshold tuning in extraction.

### Claims-only scan pipeline

`docs.pipeline.scan_claims(repo, config)` discovers the configured documents,
then parses and extracts each file in isolation. Inline/table/prose claims and
fenced-block claims are combined before explicit defaults are linked to nearby
subjects. Duplicate claims collapse only within the same document section and
retain each source location and extraction source; the stable group key omits
the section so reports can group matching concepts across docs. Confidence
below `0.3`, exact claim IDs, and `kind:glob` ignore rules are filtered after
merge. Historical mentions are omitted and counted. HTML ignore comments keep
the suppressed claim and its reason in the result for optional display.

`docs-drift scan --claims-only` renders the result grouped by document and
section order, with a per-kind count footer. `--show-suppressed` adds a second
table, and `--verbose` shows elapsed seconds for discovery, parsing, inline,
block, defaults, and merge/filter stages. A document exception becomes a named
warning and processing continues. This command reports claims only: it does
not index source code, decide verdicts, or run repository commands.

The fixture measurement command is `uv run python scripts/measure.py`. It
builds throwaway repositories from the independent fixture script, runs the
claims-only pipeline, and checks answer-key findings by document, kind, and
normalized subject. It reports recall only; it does not report precision or
change the fixed answer key. Clean-repo historical, example-value, and
third-party near-misses are checked for extraction leakage separately.
