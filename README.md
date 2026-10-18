# docs-drift

## Product brief

`docs-drift` helps maintainers and new contributors discover when repository documentation no longer describes the code: a CLI flag is renamed, an environment variable disappears, an HTTP route moves, or a default changes while the README stays behind. It reads documentation to extract concrete claims, indexes source as text or AST without importing or executing it, and matches those claims to code evidence using deterministic rules. Each claim receives an `OK`, `missing`, `renamed`, `default changed`, `invalid command`, or `unverifiable` verdict with file, line, and supporting text; confirmed drift is enriched with the introducing commit and author when git history supports that attribution. A graph connects doc files, sections, claims, symbols, source files, commits, and authors so maintainers can also ask which docs mention code touched by a PR. An optional LLM may assess prose claims that rules cannot decide, but it can never override a code verdict or turn detected drift into `OK`.

## Run the scaffold

From this repository root:

```console
uv sync
uv run docs-drift --help
uv run docs-drift --version
uv run docs-drift --repo . --format json --no-color scan
```

The version is `0.1.0`, read from installed distribution metadata. Full scans
compare extracted claims with the static code index and report evidence. The
scanner parses docs and fenced snippets as text and never imports or executes
code from the scanned repository:

```console
uv run docs-drift --repo . scan --claims-only
uv run docs-drift --repo tests/fixtures/repos/py-cli scan --claims-only
uv run docs-drift --repo . scan --all
uv run docs-drift --repo . check --fail-on drift
uv run docs-drift --repo . baseline update
```

The claims table includes source location, kind, normalized value, qualifier,
default, extraction source, and confidence. `--show-suppressed` lists inline
suppression comments; `--verbose` prints stage timings. `check` supports
confidence and finding-count thresholds, and `--baseline PATH` limits CI
failures to new drift. `baseline update` refreshes the explicit baseline,
`explain FINDING_ID` prints its evidence chain, and `ignore CLAIM_ID` adds a
stable claim ID to `.docs-drift.toml`. `fix`, `graph`, `init`, and `serve` are
still stubs. Put global options before the command. Help and version work
without loading repository configuration.

### Configuration

Select the first available source in this order: explicit `--config PATH`,
`<repo>/.docs-drift.toml`, `[tool.docs-drift]` in `<repo>/pyproject.toml`, or defaults.
Sources are not merged; omitted fields use schema defaults. An empty selected
file therefore uses defaults. Explicit relative paths use the current working
directory; implicit paths use `--repo`, which defaults to the current directory.
A missing explicit file, malformed TOML, or invalid known field produces an
error with a suggested next step. Unknown keys warn and are ignored, including
keys inside `llm` and `history`.

Example `.docs-drift.toml` (use `[tool.docs-drift]` and prefixed nested table names
when placing the same settings in `pyproject.toml`):

```toml
docs = ["README.md", "docs/**/*.md", "*.md"]
exclude = ["node_modules/**", ".venv/**", "vendor/**", "CHANGELOG.md", "**/CHANGELOG.md"]
languages = "auto" # Or an explicit list, e.g. ["python", "typescript"].
ignore = []
min_confidence = 0.6

[llm]
enabled = false
# model = "your-model-id" # Unset by default; no model is silently selected.

[history]
max_commits = 2000
```

Changelogs are excluded because they intentionally describe past behavior.
Confidence is between 0 and 1; history limits are nonnegative integers.
Configuration loading reads TOML text only and executes no repository code.

### Development and packaging

Use `uv run just setup` to sync all extras and install the local pre-commit hook,
then `uv run just check` for the full quality gate. We use just rather than both
just and Make: recipes run with native PowerShell on Windows and sh on Unix.
`rust-just` is included in the development dependencies, so a separate global
installation is unnecessary. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

```console
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

CI runs on pushes and pull requests with Ubuntu and Windows, each on Python 3.11
and 3.13, using full Git history and all extras. The coverage gate is fixed at
80%; a lower result must be reported, never fixed by lowering the threshold.
`just test` reports coverage for both the package and the staged-file guard.
`just fmt` rewrites formatting; `just check` checks formatting without rewriting.
The `dogfood` CI job and `just demo` currently run CLI help; both will run against
documentation fixtures when scanning is implemented.

Pre-commit checks Ruff, formatting, final newlines, trailing whitespace, and
staged-file safety. `scripts/guard.py` checks the Git index, including binary
files: it blocks files over 1 MB (1,000,000 bytes), `.env` and `.env.*` files,
and token-shaped Anthropic, GitHub, and AWS keys. It reports locations without
printing secret values. Text line endings are normalized to LF by `.gitattributes`.

uv installs the default development dependency group in a checkout. A matching
`dev` extra is available to package users; optional integrations can be installed
with `uv sync --extra graph`, `--extra llm`, or `--extra web`. Dependency metadata
uses lower bounds only. Commit `uv.lock` for reproducible development; it does
not pin dependencies in published package metadata. Hatchling builds a wheel
and source distribution using the existing src layout. The package includes a
`py.typed` marker and is licensed under MIT. Publishing itself is not performed.

If uv cannot write to its usual cache in a restricted environment, set
`UV_CACHE_DIR` to the repository's ignored `.uv-cache` directory before running
these commands (PowerShell: `$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'`).

For pytest under the same restriction, set `PYTEST_DEBUG_TEMPROOT` to an
existing writable directory, for example `.docs-drift/test-temp`. This keeps
test scratch files inside the ignored scanner cache without changing normal
pytest behavior for other environments.

## Working agreement

### Documentation parsing

Discovery applies configured globs and ignores, returning sorted documentation
metadata. Git roots scan tracked files; other directories use a vendor-pruned
walk with nested `.gitignore` support. Files over 2 MB warn and are skipped;
UTF-8 decoding replaces bad bytes. Markdown, `.markdown`, and MDX use static
Markdown parsing. **RST support is basic and opt-in** through configured globs;
it recognizes simple heading adornments, not full Sphinx directives or roles.

```python
from pathlib import Path
from docs_drift.config import Config
from docs_drift.docs.discover import discover_docs, read_doc
from docs_drift.docs.sections import parse_document

root = Path("tests/fixtures/repos/clean-repo")
for doc in discover_docs(root, Config()):
    parsed = parse_document(doc, read_doc(root, doc))
    for section in parsed.sections:
        print(section.heading, section.start_line, section.end_line, section.context)
```

Sections retain heading trails, source line ranges, and historical context.
`is_historical_span(section, line)` also recognizes historical paragraphs inside
current sections. `Section.historical_spans` stores those ranges without global
state. Claim extraction is available as a conservative pipeline step:

```python
from docs_drift.docs.extract_inline import extract_claims

result = extract_claims(parsed)
for claim in result.claims:
    print(claim.kind, claim.value, claim.doc_path, claim.line)
print("historical mentions ignored:", result.historical_skipped)
```

Backticked flags, environment variables, endpoints, paths, commands, and clearly
named symbols are stronger evidence than table entries or explicit default
phrases. Shell fences are inspected as text and known invocations are retained
as command claims; commands are never run. Historical mentions are suppressed
and counted. The integrated `scan --claims-only` pipeline merges duplicate
locations, applies confidence and ignore filters, retains explicitly suppressed
claims, and counts historical mentions. Matching and verdict generation remain
unimplemented. Fixture claim recall can be reproduced with
`uv run python scripts/measure.py`; the command prints its own provenance.

### Shared data contracts

The pipeline shares typed models in `docs_drift.models`. Pure ID helpers in
`docs_drift.ids` use meaningful content instead of line numbers or UUIDs:

```python
from docs_drift.ids import make_claim_id
from docs_drift.models import ClaimKind

before = make_claim_id("docs/usage.md", ["Usage"], ClaimKind.cli_flag, "--port")
after_line_shift = make_claim_id("docs/usage.md", ["Usage"], ClaimKind.cli_flag, "--port")
assert before == after_line_shift
```

`ScanResult.model_dump_json()` writes a versioned report and
`ScanResult.model_validate_json(text)` restores it. `summary()` counts findings
by verdict and claim kind; `Finding.is_drift` identifies actionable drift.
Historical-section classification and matching stay in the pipeline. See
[the model contract](docs/design.md#shared-models-and-identity-contract) for path,
location, identity, timestamp, and default-value conventions.

### Fixed evaluation fixtures

The fixture specification builds `py-cli`, `fastapi-svc`, `node-tool`, and
`clean-repo` with real, deterministic Git histories. The first three preserve
stale READMEs across source changes; the clean repository covers historical
sections, excluded changelogs, example values, and third-party flags.

```console
uv run python tests/fixtures/build_fixtures.py
uv run python scripts/measure.py
uv run pytest tests/test_fixtures_build.py tests/test_measure.py -q
```

Manual generation writes ignored `tests/fixtures/repos/`; tests use fresh temp
directories and compare independent builds. The builder refuses existing repo
destinations; use `--output PATH` to select a fresh directory. Never commit the
nested `.git` folders or import, install, or run generated fixture programs.
See the hand-authored [answer key and provenance](tests/fixtures/ANSWER_KEY.md).
The measurement stub only validates the JSON key; it reports no precision or
recall until scoring exists. Fix detector behavior against these expectations,
never the expectations against detector output.

### Fixed identity and stack

- PyPI distribution: `docs-drift`; CLI: `docs-drift`; import: `import docs_drift`.
- Python 3.11+, managed with uv, using a `src/docs_drift/` layout.
- Typer for the CLI, Rich for terminal output, and Pydantic v2 for models.
- pytest, Ruff, and strict mypy for checks.
- Optional extras named `graph`, `llm`, and `web`; basic rule-based scanning must work without them.
- The optional integrations target FalkorDB (`graph`), Anthropic (`llm`), and FastAPI, Uvicorn, and Jinja2 (`web`), as specified in the scaffold requirements.
- The current scaffold provides packaging, configuration loading, and CLI stubs only. Scanning and optional integrations are not implemented. Never create automatic commits.

### Definition of done for each change

- The agreed pytest, Ruff, and strict mypy checks pass once the tooling exists. Report exact commands, results, and any checks that could not run; unavailable tooling is not a passing check.
- Review the diff for scope, correctness, and accidental changes.
- Update the README when behavior changes and show a representative input/output example.
- Keep the design and evidence contract consistent with the behavior.
- Preserve the scan safety boundary and the fixed fixture answer key.
- Provide a proposed commit message; leave committing to the user.

## Planned module map

The package boundary, configuration loader, and CLI skeleton exist; the remaining modules are planned.

| Path | Responsibility |
| --- | --- |
| `src/docs_drift/__init__.py` | Package boundary. |
| `src/docs_drift/config.py` | Pydantic configuration schema and TOML source selection. |
| `src/docs_drift/models.py` | Typed claims, evidence, symbols, verdicts, and report models. |
| `src/docs_drift/docs/` | Discover docs, parse sections and code blocks, extract claims. |
| `src/docs_drift/index/` | Build source indexes using text and AST only. |
| `src/docs_drift/match/` | Apply deterministic matching and verdict rules. |
| `src/docs_drift/history.py` | Attribute confirmed drift using git evidence. |
| `src/docs_drift/graph/` | Build relationships and support impact queries. |
| `src/docs_drift/llm/` | Optional, separately labeled prose assessment. |
| `src/docs_drift/report/` | Render findings and their evidence. |
| `src/docs_drift/cli.py` | Typer entry point and Rich output. |
| `src/docs_drift/web/` | Optional FastAPI interface. |

## Illustrative example, not implemented behavior

Documentation at `README.md:42` says `serve --port 8000`. Source evidence and git history establish that `--port` became `--listen`.

```text
renamed: README.md:42 documents --port; source now declares --listen.
Doc evidence: README.md:42  serve --port 8000
Source evidence: src/example/cli.py:18  parser.add_argument("--listen")
History: a1b2c3d, Priya, 3 September [illustrative attribution]
```

A missing `--port` declaration alone does not prove a rename. Without a supported mapping, the tool must not invent one.
# Static code index

`docs-drift index` parses tracked source files as text or AST; it never imports or executes a scan repository. Use `--no-cache` to rebuild, `--list KIND` to inspect one symbol type, `--find NAME` for fuzzy lookup, `--tree` to print CLI commands and flags, and `cache clear` to remove the local index cache. The cache is a per-file atomic JSON document under `.docs-drift/cache/`.

The first indexers support Python declarations and argparse/Click/Typer patterns, JavaScript/TypeScript declarations and common Commander/Express patterns, literal Python/JS routes, package scripts, common config formats, Docker snippets, CI jobs, and common task files. Coverage is partial: dynamic plugin registration, generated declarations, complex router composition, and unsupported ecosystems may remain unverifiable. RST parsing is basic. Rust and Go configuration indexing are not implemented.

| Source language or surface | Current index coverage |
| --- | --- |
| Python | AST symbols, argparse, partial Click/Typer, FastAPI/Flask patterns, settings defaults |
| JavaScript/TypeScript | Tree-sitter-backed symbols, partial Commander patterns, Express routes, package scripts and env examples |
| JSON, YAML, TOML, INI, Docker, CI and task files | Literal keys, jobs, scripts and targets; format-specific coverage is partial |
| Django/DRF, Next.js, NestJS, OpenAPI and Rust/Go config | Not implemented or only partial; never infer missing solely from absent symbols |
