# docs-drift

**Find the lines in your docs that your code no longer supports, with the source line and commit that prove it.**

[![PyPI](https://img.shields.io/pypi/v/docs-drift)](https://pypi.org/project/docs-drift/)
[![Python](https://img.shields.io/pypi/pyversions/docs-drift)](https://pypi.org/project/docs-drift/)
[![CI](https://github.com/ayushagarwalhere/docs-drift/actions/workflows/ci.yml/badge.svg)](https://github.com/ayushagarwalhere/docs-drift/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/badge/licence-MIT-blue)](license.md)

A README says `--port`. The code was renamed to `--listen` three weeks ago. A new
contributor copies the command, it fails, and they decide your docs can't be
trusted. docs-drift catches that before they do. It reads your documentation,
indexes your source, and reports every flag, endpoint, environment variable,
setting and file path that no longer matches, with the commit that changed it.

![Terminal scan of a fixture repository](assets/scan-demo.gif)

## Install

```sh
pipx install docs-drift
docs-drift scan
```

Or run it once without installing: `uvx docs-drift scan`. Python 3.11+ and Git required.

## A real catch

rich-cli's README told users to run `rich README.md --line-number`. That fails
with `No such option '--line-number'` (exit 2): the Click declaration is
`--line-numbers`. docs-drift points to
[README.md:61](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/README.md#L61)
and the [declaration](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/src/rich_cli/__main__.py#L358).
[Full investigation, including the false positives in that scan](REAL-CASE.md).

## What it checks

| Claim in your docs | Checked against |
| --- | --- |
| CLI flags and commands (`--port`, `tool serve`) | argparse, Click, Typer and Commander declarations |
| Environment variables (`APP_LOG_LEVEL`) | literal environment reads and config templates |
| HTTP endpoints (`GET /status`) | FastAPI, Flask and Express routes |
| Settings and defaults ("`workers` defaults to 4") | literal declarations and defaults |
| Code symbols (`Client.connect()`) | parsed functions, classes and methods |
| File paths (`config/settings.toml`) | files in the repository |
| Package scripts (`npm run build`) | package manifests, Make and just tasks |
| Runtimes and installs ("Python 3.11+", `pip install "pkg[extra]"`) | pyproject, setup, requirements and package.json manifests |

Every finding carries a verdict (`missing`, `renamed`, `default_changed`,
`invalid_command`, `version_mismatch`, `unknown_extra`, `unverifiable`), a
confidence score, the source evidence and, where Git history allows, the commit
that introduced the drift. `docs-drift deps` also compares your imports with your
declared dependencies to find stale and undeclared packages
([dependency drift](dependencies.md)).

## Commands

```sh
docs-drift scan                             # report findings, exit 0
docs-drift check                            # exit 1 when findings meet your threshold
docs-drift fix                              # preview a patch; --write applies it
docs-drift insights                         # findings by verdict, document, package and age
docs-drift contribute                       # issue drafts with patches for contributors
docs-drift contribute --summary             # docs health card for contributing.md
docs-drift impact --base HEAD~1             # findings touched by recent changes
docs-drift pr --base HEAD~1 --report-dir pr  # review artifacts for drift a branch introduces
docs-drift baseline update                  # accept today's drift, gate only new drift
docs-drift deps                             # dependency drift: docs, manifests and imports
docs-drift badge --json badge.json          # "N stale" shields.io badge (or --svg)
docs-drift scan --format html --output report.html
```

`docs-drift explain <finding-id>` shows the docs, source, reasons and history
behind one finding.

Each drift finding records when it went stale. Set `max_drift_age_days` in a
`[policy]` table, or pass `--max-drift-age` to `check`, to fail only for drift
older than that and print a fix-by date per finding.
[Drift age policy](drift-age.md) · [Contributor mode](contribute.md).

Reports: terminal, JSON, HTML (with an evidence graph), Markdown, SARIF, JUnit
and GitHub annotations. [Full CLI reference](CLI.md). Publish a docs-health
badge from CI with the [badge guide](badge.md).

![HTML report with the evidence graph](assets/graph-view.png)

## CI

```yaml
name: docs
on: [pull_request]
permissions: {contents: read}
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: ayushagarwalhere/docs-drift@v0.1.0
        with:
          base-ref: origin/${{ github.base_ref }}
          fail-on-drift: 'true'
```

With `base-ref`, the gate fails only on drift the pull request introduces.
Reports are written as JSON, HTML, SARIF and Markdown. Add `comment: 'true'` for a sticky PR
summary with inline suggested changes, or use a scheduled tracking issue or the self-hosted GitHub App.
[Action guide](github-action.md) · [GitHub integration](github-integration.md) ·
[GitHub App](github-app.md).

## Use it as a dependency

Add docs-drift to your dev dependencies and drift fails like a test:
`pytest --docs-drift` adds one test per documentation file, `assert_no_drift()`
works inside any test, `docs_drift.check()` returns a typed result, and
pre-commit hooks are included. [Guide](as-a-dependency.md).

## Editor

The VS Code extension shows findings as diagnostics, jumps to the source
declaration, and offers quick fixes, backed by `docs-drift lsp`.
[Extension guide](vscode.md).

## Use it from your AI coding agent

<!-- mcp-name: io.github.ayushagarwalhere/docs-drift -->

An MCP server lets Claude Code, Cursor, VS Code Copilot, Windsurf and other agents
check docs while they edit: ask "check docs drift" or "is my README still accurate
after this rename?", or have the agent check a paragraph before writing it. Tools
are read-only except an explicit `apply_fix`, and paths are confined to the
repository. Claude Code setup:

```text
claude mcp add docs-drift -- uvx --from "docs-drift[mcp]" docs-drift mcp
```

Until docs-drift 0.1.1 is on PyPI, use
`--from "docs-drift[mcp] @ git+https://github.com/ayushagarwalhere/docs-drift"` instead.
[MCP guide](mcp.md): Cursor, VS Code, Windsurf and Claude Desktop setup, plus the tool reference.

## How it works

```mermaid
flowchart LR
  D[Docs] --> C[Claims]
  S[Source AST] --> I[Static index]
  C --> M[Matching]
  I --> M
  M --> H[Git history]
  H --> R[Findings, reports, fixes]
```

1. Markdown, RST and MDX are parsed into located claims.
2. Source is parsed with Python's `ast` and tree-sitter (JS/TS) into an index of
   declarations. Your code is never imported or executed.
3. Each claim is matched to the index; renames are detected by similarity and history.
4. Git history finds the commit where a symbol disappeared or changed.

An optional model mode can assess prose claims, but it cannot override a finding
the rules already decided, and every answer must cite source that passes validation.
[Design notes](how-it-works.md).

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

"Partial" means static, literal patterns. Dynamic registration and generated
code can be reported as unverifiable rather than guessed.

## Accuracy

<!-- accuracy:start -->
Real-world accuracy is **not yet established**. The 12-repository protocol and
blank human labeling sheet are in [the benchmark](benchmark.md); its
[dated results](benchmark.md) keep precision/recall pending until the owner
labels them. Fixture scores establish plumbing, not product accuracy. Missing
findings can be intentional examples, third-party tools or indexing limitations;
review every report. [Known limitations](LIMITATIONS.md).
<!-- accuracy:end -->

## Configuration

```toml
# .docs-drift.toml
min_confidence = 0.6

[history]
max_commits = 2000
```

[Configuration reference](configuration.md), generated from the settings model.

## Privacy and security

Rule-based scans parse text and syntax trees locally and call Git for history.
They never import or execute the scanned code, and make no network calls. Model
mode is opt-in, sends only redacted excerpts, and can be replayed offline.
[Security policy](SECURITY.md) · [Threat model](SECURITY.md).

## Try it in the browser

The web app scans a public GitHub repository without sign-in, and includes saved
reports you can open instantly. Each report can be turned into a pull request: a
patch with only the exact edits docs-drift is certain about, a suggested PR
description with evidence pinned to the scanned commit, and issue drafts for the
rest. Review every change before opening a PR. Run it locally:

```sh
uv run --all-extras python -m docs_drift.web   # http://127.0.0.1:8080
```

## How Wispr Flow is used

Dictated docs are written from memory, and memory goes stale. Wispr Flow types into
any focused text field, so dictation goes straight into docs-drift:

- **Write with voice**, a page of the web app: hold your dictation key and
  describe how to use a project. About 600 ms after you pause, every flag, command,
  env var and setting you said is checked against the repository's static index,
  with evidence and one-click suggestions.
- **Your AI coding agent**: dictate "check this paragraph against the code" and the
  agent calls the MCP `check_text` tool before it edits the README.
- **VS Code**: dictate into a Markdown file and the language server flags stale
  flags and settings as you speak, with quick fixes.

[Write with voice guide](write-with-voice.md).

## Built with voice

docs-drift was built for the WeMakeDevs × Wispr Flow "No Hands" challenge, with
requirements, prompts and docs dictated through Wispr Flow.
[Flow usage evidence](built-with-voice.md).

## Contributing

See [contributing.md](contributing.md). Run tests, lint and type checks before
opening a change. MIT licensed. [Changelog](CHANGELOG.md).
