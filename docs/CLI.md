# CLI and reports

Start with `docs-drift init` or `docs-drift init --yes`. Setup detects languages,
frameworks, package READMEs, and docs sites, writes a commented `.docs-drift.toml`,
and offers a baseline, a GitHub workflow, and a local pre-commit entry. Existing
configuration and workflow files are preserved. Run `pre-commit install` to enable
the hook; install docs-drift into that environment first. Review the generated
workflow's installation command for your package registry or source checkout.

From a source checkout, first build the trusted demo fixtures with `python -m tests.fixtures.build_fixtures` (use a fresh output directory).
Common options work before or after the command, including nested commands:

```console
docs-drift scan --repo tests/fixtures/repos/py-cli --no-color -v
docs-drift cache clear --repo tests/fixtures/repos/py-cli -q
docs-drift scan --repo tests/fixtures/repos/py-cli --format html --output report.html
docs-drift scan --repo tests/fixtures/repos/py-cli --format terminal --output-json scan.json --output-sarif scan.sarif
docs-drift explain claim_7aee9594e82a --repo tests/fixtures/repos/py-cli --format json
```

`-q` suppresses routine stdout; explicit report destinations are still written.
`-v` adds confidence and stage timings. `--no-color` disables ANSI styling and
hyperlinks. Piped terminal output never contains escape sequences. Interactive
scans show docs, index, match, and history progress on stderr. Unexpected failures
write the generated debug log `.docs-drift/last-error.log`; only `--debug` displays a traceback. Review logs
before attaching them to an issue because they contain local paths.

## Formats

`portfolio --manifest organization.toml --report-dir reports --repo WORKSPACE`
scans explicitly listed local checkouts into one static dashboard. Add
`--report-only` to collect findings without enforcing drift budgets. Scan errors
still exit 2. Each checkout uses its own configuration and optional reviewed
baseline. See [organization adoption](ORGANIZATIONS.md) for the manifest and
operating limits. Portfolio artifacts use `--report-dir`, not the single-report
`--output` destination.

`scan`, `check`, and `graph` use one `render(scan_result, format, options) -> str`
interface. `explain --format FORMAT` renders the selected finding. `index --format
json` exports the index, and `fix --format json` exports the exact edit plan.
Mutation commands retain concise status messages. Human reports wrap with the
terminal width, and structured formats always include low-confidence findings.

| Format | Purpose |
| --- | --- |
| terminal | File groups, highlighted claims, suggestions, commits, and health |
| json | Full versioned ScanResult; findings sorted by path, line, and ID |
| sarif | SARIF 2.1.0 locations, rule help, fingerprints, confidence, and decider |
| markdown | Escaped summary and evidence, capped at 60,000 UTF-8 bytes |
| junit | A case for every checked claim, with drift failures and unverifiable skips |
| github | Escaped workflow annotations, including file, line, and column |
| html | An offline report with filters, evidence, graph, and insights |

Markdown normally uses diff blocks; `RenderOptions(pr_mode=True)` enables GitHub
suggestion blocks for a PR integration. The truncation note links to
`RenderOptions.artifact_url`, defaulting to the generated report `scan.html`; upload that artifact with
the Markdown report. The HTML report hides author names unless `--show-authors`
is supplied, uses a dark initial theme, remembers the chosen theme, and supports a theme toggle, search and j/k/Enter.
Graph and evidence data are escaped, and no network requests are needed.

Cytoscape.js **3.30.4** is vendored under MIT with its upstream license. Its
373,734 bytes account for most of the roughly 400 KB py-cli report. We use the
built-in cose layout with repulsion and edge spacing suited to the small drift
subgraph; it needs no build step, npm, layout extension, or CDN. If the graph
library is unavailable, the findings remain usable with an inline fallback.

The initial JSON contract is [scan-result.v1.json](schema/scan-result.v1.json).
Generate it from Pydantic with `python -m scripts.build_examples`. Review contract
changes, bump `schema_version`, write a new schema filename, and note the change
in `CHANGELOG.md`. The tests detect accidental schema/model drift. The official
SARIF schema is vendored unmodified with its OASIS notices and repository terms.

## Workspaces and separate docs

Workspace discovery recognizes npm/yarn/pnpm manifests, uv and Cargo workspace
members, multiple Go modules, Nx project manifests, and Turbo package manifests.
Docs belong to their nearest package root. Matching tries that package first;
an exact repository-wide fallback reduces confidence by 0.08. One repository
cache serves all packages. JSON findings include package names; terminal groups
and HTML insights show package scope and health.

```console
docs-drift scan --repo tests/fixtures/repos/py-cli --package py-cli
docs-drift scan --repo tests/fixtures/repos/py-cli --docs-repo tests/fixtures/repos/py-cli --format html --output report.html
```

In separate-repository mode, document paths and terminal links refer to the docs
checkout; source symbols, HEAD, remote links, and introducing commits refer to
the code checkout. Extraction reads command metadata from the code checkout.
The package fallback is the code repository root when docs paths have no
corresponding package path. Supply docs globs in the code checkout's config;
they are evaluated relative to the docs checkout. Scanning never executes either
repository's code. Rust and Go workspace discovery does not imply a Rust or Go
symbol indexer; unsupported constructs remain unverifiable.

Docusaurus, MkDocs, VitePress, Nextra/Next docs, Mintlify, and Sphinx content is
included automatically with default docs globs. Explicit configured globs win.
MDX wrappers/imports are stripped without removing fenced examples inside Tabs
or CodeGroup. RST headings, inline literals, code-block directives, and explicit
`:option:`/`:envvar:` roles are supported.

## Watch and editors

```console
uv sync --locked --all-extras
docs-drift watch --repo tests/fixtures/repos/py-cli
docs-drift lsp --repo tests/fixtures/repos/py-cli
```

watchfiles is an optional `[watch]` extra: its native wheel is unnecessary for
one-shot and CI scans. Events are debounced for 300 ms. Doc edits re-extract only
that document; the per-repository cache re-parses changed source files. Candidate
adjacency limits matching to affected claims, including defaults and new symbols.
Only changed findings are printed; introducing history is looked up for new
findings in one background worker, with generation checks rejecting stale results.

pygls is isolated in `[lsp]`. The server keeps its index in memory, handles unsaved
document changes, publishes claim-span diagnostics, reuses conservative rule fixes,
and provides hover and definition links. Client watched-file notifications update
source candidates. Protocol positions use UTF-16, including text before a claim.

For VS Code, open `editors/vscode`, run `npm ci`, set `docsDrift.executable`
to your installed executable if it is not on PATH, and press F5. The included
launch configuration opens the py-cli fixture. Open README.md, place the cursor
on `--port`, and choose the `--listen` quick fix. See
[verification results](RESULTS.md) for the checks actually performed.

## Completion and reproducible demos

Typer supplies shell completion:

```console
docs-drift --install-completion
docs-drift --show-completion
```

Run these in your desired shell (Bash, Zsh, Fish, or PowerShell), then restart the
shell if requested. Every command's `--help` includes an example.

From a development checkout:

```console
python -m tests.fixtures.build_fixtures
python -m scripts.build_examples
python -m scripts.record_demo
python -m scripts.bench_scan
python -m scripts.check_html
```

The fixture builder requires a fresh output directory. The recording command
captures actual CLI output as `examples/py-cli-demo.cast`, without applying fixes.
Play it with `asciinema play examples/py-cli-demo.cast`. Benchmark output is saved
in `examples/monorepo-benchmark.json`; fixture generation is outside the measured
scan. The browser check uses installed Edge through Playwright and saves all four
screenshots, checking for JavaScript errors and horizontal overflow.
