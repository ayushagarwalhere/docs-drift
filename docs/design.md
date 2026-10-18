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
