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

The version is `0.1.0`, read from installed distribution metadata. All seven
commands (`scan`, `check`, `explain`, `fix`, `graph`, `init`, `serve`) currently
print `not built yet` and exit with code 2. Format options reserve the future
report interface; they do not format the stub message. Put global options before
the command. Help and version work without loading repository configuration.

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

### Non-negotiable rules

1. Never import or execute code from a repository being scanned. Parse it only as text or AST. Never execute documented shell commands, package scripts, Makefile targets, or generated scan reports as code.
2. Rules first, model second: every check that code and deterministic rules can decide must be decided by those rules.
3. The optional LLM only assesses prose claims rules cannot decide. It cannot replace a deterministic verdict, suppress detected drift, or turn code-detected drift into `OK`. Label model assessments and preserve their evidence separately.
4. Keep a fixed answer key for fixture reports. Never tune rules, thresholds, or answer keys to improve a score. Correctness changes require independently justified evidence and a visible explanation.
5. Report every metric with the exact command that produced it, including bad results. Do not invent measurements or omit failures. Record enough context to reproduce a reported result.
6. Every finding carries its evidence: file path, line, and exact supporting text. Include documentation and source evidence where available; if no source exists, record the search scope and relevant rule rather than fabricating a source location.
7. Prefer false negatives to false positives. An uncertain match or unsupported history attribution is `unverifiable`, not an asserted defect.
8. Use small, pure functions, type hints everywhere, and no global mutable state. Errors use plain English and say what to do next.
9. After each prompt, run the applicable checks, read the diff, and prepare a commit-message suggestion for dictation. Never create a commit automatically.

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

## Hackathon delivery agreement

- Provide a working project link that opens without sign-in, or supply a test login and sample input.
- Provide a demo video no longer than two minutes.
- Include a screenshot of the Wispr usage card.
- Include a short description of the problem, intended users, and how Wispr Flow was used.
- Record actual Flow use honestly; do not claim that text passed through Flow without evidence.
- Per the supplied brief, judging emphasizes writing through Flow and Wispr verifies winners' counts; no other weights were supplied. Scores and comments are public, with additional prizes described for builder time, usage, and weekly highlights. These details have not been independently verified.
- The dictated word-count window, “16 to 13 October,” is ambiguous. Confirm the official dates and timezone before using them in submission materials.

See [docs/design.md](docs/design.md) for the data flow, claim taxonomy, verdict definitions, and non-goals.
