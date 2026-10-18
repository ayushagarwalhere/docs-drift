# docs-drift

## Judges start here

- **Public demo:** pending deployment approval; no public URL is claimed yet.
- **Video:** pending owner recording and upload; target 1:53 with captions.
- **Source:** [GitHub repository](https://github.com/ayushagarwalhere/docs-drift).
- **Evidence:** [benchmark results](benchmark/RESULTS.md),
  [measured limitations](docs/RESULTS.md), and [submission checklist](SUBMISSION.md).

Three-command local quick start (Python 3.11+, Git and uv installed):

```sh
git clone https://github.com/ayushagarwalhere/docs-drift.git
cd docs-drift
uv run --all-extras docs-drift scan --repo .
```

Release preparation: [owner's publish gates](RELEASING.md). Public installs and
URLs must be verified after approval; local checks do not prove a published release.

**Find the lines in your docs that your code no longer supports, and the commit that broke each one.**

[![CI](https://img.shields.io/badge/CI-configured-blue)](.github/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/docs-drift.svg)](https://pypi.org/project/docs-drift/)
[![Licence](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)

Alpha software: source installation works before the first package is published.
The PyPI page becomes available after the owner approves publication; the hosted
demo and video remain explicitly pending above. Real-world
precision and recall are **pending human labeling**, not inferred from fixtures.

![Actual fixture scan output, eight-second terminal replay](docs/assets/scan-demo.gif)

![HTML evidence graph from the actual py-cli fixture](docs/assets/graph-view.png)

## Why (illustrative fixture)

**PERSONALISE: replace this paragraph with your own experience.** A new contributor
copies the README's first command and it fails. In this synthetic example, the
README still says `--port`, while the code now accepts `--listen`. docs-drift
connects the stale statement, current source and the change that explains it.

The fixture shown above is deliberately synthetic; it is not a public accuracy
measurement. Static matching is partial, so inspect evidence before accepting a fix.

For beginners contributing to documentation, an evidence-backed finding can be a
concrete first contribution. Maintainers can use the same checks to keep docs
aligned with a changing codebase; the suggested patches still need review.

## Quick start

From this source checkout, with Python 3.11+ and Git:

```sh
uv sync --locked --all-extras
uv run docs-drift scan
```

After the first PyPI release, these are the intended package entry points:

```sh
pipx install docs-drift
docs-drift scan
# Alternative after publication:
uvx docs-drift scan
```

Or build the CLI container locally (Docker must be running):

```sh
docker build -t docs-drift:local . && docker run --rm -v "$PWD:/repo" docs-drift:local scan --repo /repo
```

Try it in your browser: the public demo URL is pending hosting approval. Locally,
start `uv run python -m docs_drift.web` and open `http://127.0.0.1:8080`; set
`DOCS_DRIFT_ALLOWED_ORIGIN` to that exact origin before submitting a scan.

Useful commands, exercised against a trusted fixture in CI:

```sh
docs-drift scan --no-color
docs-drift scan --format html --output scan.html
docs-drift insights --format json
docs-drift impact --base HEAD~1 --format json
docs-drift fix
docs-drift check  # documented exit: 1 on the stale py-cli fixture
```

`scan` reports findings and exits 0. `check` exits 1 for findings meeting its
threshold, or 2 for usage/configuration errors. `fix` previews a patch; applying
it requires an explicit write option and confirmation. No target code is run.

## What it checks (illustrative fixtures)

| Claim kind | Example documentation | Static evidence |
| --- | --- | --- |
| CLI flag / command | A synthetic README mentions `--port` | argparse/Click/Typer or Commander declarations |
| Environment variable | A synthetic README mentions `APP_LOG_LEVEL` | literal environment reads or safe config templates |
| Endpoint | A synthetic guide requests `GET /status` | literal FastAPI/Flask/Express routes |
| Setting / default | A synthetic guide says `workers` defaults to 4 | a recognized literal declaration/default |
| Code symbol | A synthetic guide calls `Client.connect()` | a parsed function/class/method declaration |
| File path | A synthetic guide links `config/settings.toml` | a contained repository-relative path |
| Package script / task | A synthetic guide invokes `npm run build` | package manifests and Make/just task declarations |
| Project command | A synthetic guide runs `tool serve --port 8000` | independently identified executable/command/flags |

Arbitrary behavioural prose is not proved by name matching. Optional model
assessment is separately labeled and constrained by source citations.

## Supported ecosystems

<!-- support-table:start -->
| Language | Framework / surface | Support |
| --- | --- | --- |
| Python | AST declarations | partial |
| Python | argparse | partial |
| Python | Click | partial |
| Python | Typer | partial |
| Python | FastAPI | partial |
| Python | Flask | partial |
| Python | Django / DRF | none |
| JavaScript / TypeScript | AST declarations | partial |
| JavaScript / TypeScript | Commander | partial |
| JavaScript / TypeScript | yargs | none |
| JavaScript / TypeScript | Express | partial |
| JavaScript / TypeScript | Next.js / NestJS routes | none |
| JSON / YAML / TOML / INI | literal settings | partial |
| Go / Rust | language-specific configuration | none |
<!-- support-table:end -->

Partial means recognized literal/static patterns, not complete framework support.
No framework is advertised as fully supported. Dynamic registration, signatures,
router composition and generated source may remain unverifiable.

## CI

This ten-line example uses the development Action from this repository:

```yaml
name: Documentation
on: [push, pull_request]
permissions: {contents: read}
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803
      - uses: ./
        with: {repo: '.', fail-on-drift: 'true'}
```

Other projects must replace the local action path with the real repository pinned
to a reviewed commit after publication. Reports include JSON, HTML, SARIF and
escaped Markdown. The Action needs no write token, accepts no model key, and never
posts a PR comment automatically. [Action guide](docs/github-action.md).

![Preview of generated PR Markdown, not a submitted comment](docs/assets/pr-comment-preview.png)

## Editor

The development VS Code extension supplies diagnostics, definitions and safe
quick fixes via the stdio LSP. **Actual squiggle GIF pending a verified editor
capture**; protocol tests are not presented as a native-editor recording.
[Extension guide](docs/vscode.md).

## How it works

```mermaid
flowchart LR
  D[Docs] --> C[Located claims]
  S[Source text / AST] --> I[Static index]
  C --> M[Scoped matching]
  I --> M
  M --> H[Bounded Git history]
  H --> R[Evidence, reports and reviewable fixes]
```

```mermaid
flowchart LR
  Section --> Claim
  Claim --> Symbol
  Symbol --> SourceFile
  SourceFile --> Commit
```

Rules decide literal claims first. Optional model assessment handles selected
anchored prose, cannot override code-decided findings, and must cite supplied
source that passes validation.

## Accuracy and limitations

<!-- accuracy:start -->
Real-world accuracy is **not yet established**. The 12-repository protocol and
blank human labeling sheet are in [the benchmark](benchmark/PROTOCOL.md); its
[dated results](benchmark/RESULTS.md) keep precision/recall pending until the owner
labels them. Fixture scores establish plumbing, not product accuracy. Missing
findings can be intentional examples, third-party tools or indexing limitations;
review every report. [Known limitations](docs/LIMITATIONS.md).
<!-- accuracy:end -->

## Configuration

```toml
# .docs-drift.toml
min_confidence = 0.6
[history]
max_commits = 2000
```

The [full configuration reference](docs/configuration.md) is generated from the
Pydantic model and checked for freshness in CI. Common CLI options work before
or after subcommands. [CLI reference](docs/CLI.md).

## Privacy and security

Rule-only scans parse local text/AST and call Git for evidence. They never import
or execute target modules or fenced commands. Optional model mode sends selected
redacted documentation/source excerpts through OpenRouter to the chosen provider;
replay mode sends nothing. The public demo and composite Action are rule-only.
[Security policy](SECURITY.md) ? [Threat model](docs/SECURITY.md).

## Built with voice

**AUTHOR TO WRITE / PERSONALISE:** describe your real hands-free workflow with
Wispr Flow for the No Hands challenge, your usage card and dictated prompts.
[How I built it ? placeholder](docs/built-with-voice.md). No story or usage figures
have been invented.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md). Run tests, lint and type checks before
opening a change; do not change a frozen benchmark's rules or prefill human labels.
The code is [MIT licensed](LICENSE). [Changelog](CHANGELOG.md).
