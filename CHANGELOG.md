# Changelog

All notable user-facing changes are recorded here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-10-27

### Added

- Opt-in model triage (`--llm-triage` on `scan` and `check`, or `[llm] triage = true`).
  A small model sees a redacted documentation excerpt for each rule drift finding.
  It can only downgrade a finding to `unverifiable`, and only when it cites
  verbatim excerpt text showing the name is not about this project (a
  doc-defined example, another tool, a placeholder, and so on). The original verdict
  and the model's reason stay in the finding's new `triage` field. Measured gain is
  small; see `docs/llm-triage-eval.md`.
- Web demo: a "Turn this into a PR" panel on every report. While the clone still
  exists, a live scan plans the certain edits `docs-drift fix` would make and serves
  them as a `git apply`-able `fix.patch` pinned to the scanned commit, a suggested PR
  title and description with evidence links to that commit, and contributor-friendly
  issue drafts (`/api/scans/<id>/fix.patch`, `pr.md`, `issues.md`). Each finding
  shows whether it is in the patch. Nothing is reworded or guessed, and the drafts
  leave verification to the person opening the PR.
- `docs-drift badge`: a docs-health badge (`0 stale` / `N stale`) as a shields.io
  endpoint JSON and/or a flat SVG, from a fresh scan that respects configuration,
  `--min-confidence` and `--baseline`. The web demo serves `/badge/<owner>/<repo>.svg`
  and `.json` from its latest cached default-branch scan without starting new scans.
  See [the badge guide](docs/badge.md) for a GitHub Actions workflow.

- Go and Rust CLI indexing (partial): Cobra, spf13/pflag, the standard `flag`
  package, urfave/cli and bep/simplecobra in Go; clap (derive and builder),
  structopt and argh in Rust. Literal flags, short and long aliases,
  subcommands, literal defaults and environment variables (`os.Getenv`,
  `std::env::var`, urfave `EnvVars`, clap `env`, viper `BindEnv`, `env:` tags)
  are indexed so documented flags, commands and variables can be matched;
  dynamic environment prefixes (viper `SetEnvPrefix`/`AutomaticEnv`, `envy`)
  make names under them unverifiable instead of missing. Executables come from
  `package main` directories, `go.mod` and `Cargo.toml`. Adds the
  `tree-sitter-go` and `tree-sitter-rust` grammar wheels. See `docs/go-rust.md`.
- `docs-drift mcp`: a Model Context Protocol server, available with the new `mcp`
  extra, for AI coding agents. It checks the repository, single documents and unsaved
  text, explains findings, previews fixes and applies them only on an explicit,
  destructive-annotated call. Paths are confined to the repository root, output and
  time are bounded, and scans are cached until files change.
- GitHub publishing (`docs-drift github pr-review` / `issue`, `github` extra): a
  sticky PR summary comment, inline review comments with GitHub suggested changes
  on diff lines, deduplicated reruns, an optional Check Run, and one scheduled
  tracking issue that closes at zero drift and reopens if it returns.
- Action inputs `mode`, `comment`, `github-token`, `check-run`, `suggestions` and
  `issue-label` (default behaviour unchanged); the write token reaches only a
  separate publish step. Example PR and scheduled workflows in `examples/workflows`.
- A self-hostable GitHub App (`github-app` extra): signed, replay-protected
  webhooks, a coalescing job queue, credential-scoped PR fetches, and a manifest
  helper that registers the App in two clicks and prints the `/etc/docs-drift.env`
  lines for the EC2 host without saving secrets.
- Dependency drift. Runtime minimums (in prose, tables and badges) and install
  commands in docs are checked against `pyproject.toml`, `setup.cfg`, `setup.py`,
  requirements files, `Pipfile` and `package.json`. Two new verdicts are added:
  `version_mismatch` (DD006) and `unknown_extra` (DD007). These checks are on by
  default under `[deps] enabled`.
- `docs-drift deps` compares source imports with declared dependencies and
  reports undeclared, development-only and unused packages. `--base` shows only
  issues the branch introduced, plus new dependencies the docs never name.
  Baselines and terminal, JSON and Markdown output are supported. Scan JSON
  gains a `dependency_issues` array when `[deps] usage = true` finds issues
  (the key is omitted when empty).
- Use docs-drift as a dependency: a pytest plugin (`pytest --docs-drift`, one
  test per documentation file, cached and xdist-safe), `assert_no_drift` for
  your own tests, a stable `docs_drift.check()` Python API, pre-commit hooks
  (`docs-drift` and `docs-drift-fast`), `python -m docs_drift`, and
  `docs-drift check --no-history` and `--auto-baseline` (apply
  `.docs-drift-baseline.json` when present, as the plugin does). See
  docs/as-a-dependency.md.
- `docs-drift contribute` turns well-evidenced drift into issue drafts for
  contributors: file and line, current and corrected text, evidence links pinned
  to HEAD, local verification commands, difficulty, suggested labels and a ready
  patch. Related findings share one draft. Output is a Markdown digest, one file
  per draft (`--out-dir`) or JSON. `--summary` prints a docs health card for
  CONTRIBUTING.md.
- Drift findings with history attribution now record `stale_since`, `age_days`
  (measured against the HEAD commit date) and, under a policy, `fix_by`. Reports
  show them. Ages start when the introducing commit landed on the branch (its
  committer date, recorded as `introduced_by.committed`), so rebased or
  cherry-picked drift is not overdue on arrival. The JSON schema gains these
  optional fields; older reports still load.
- An optional `[policy]` table (`max_drift_age_days`, `warn_drift_age_days`,
  `fail_unknown_age`) and `docs-drift check --max-drift-age`: when set, the check
  fails only for drift older than the maximum age. Default behaviour is unchanged.
- `docs-drift insights` reports a drift age histogram, the oldest drift and
  policy status, and a trend across saved JSON reports with `--trend`.

### Changed

- scan-result v1 gains claim kinds/verdicts; strict validators pinned to 0.1.0's
  copy must update. The `scan-result.v1.json` schema is extended additively
  (claim kinds `dependency` and `runtime_version`, verdicts `version_mismatch`
  and `unknown_extra`, the optional `dependency_issues` array and the optional
  drift age fields) and `schema_version` stays `1`. Use the copy shipped with the installed package
  or `docs/schema/scan-result.v1.json`.
- The terminal report shows drift age relative to HEAD instead of days before
  the current date, so repeated runs on the same commit print the same text.
- Web demo: a **Write with voice** page (`/write`). Dictate or type documentation
  and every flag, command, env var and setting is checked against a saved static
  index about 600 ms after you pause, with verdicts, `file:line` evidence,
  one-click suggestions and Copy as Markdown. Live scans now keep a scrubbed,
  size-capped copy of their static index after the clone is deleted, and the
  bundled examples ship one (`scripts/build_write_indexes.py`). Checks reuse the
  MCP `check_text` document path, are limited to 20 KB, origin-checked and have
  their own rate limit (`DOCS_DRIFT_WRITE_RATE_LIMIT`). See
  [Write with voice](docs/write-with-voice.md).

### Fixed

- A command run from a checkout script path (`script/run-tests groups`) is no
  longer reported as an unknown subcommand because some other indexed CLI has
  subcommands; only its flags are checked.
- PR comparisons no longer report history-confirmed renames as new drift when the
  base snapshot (scanned without history) already had the same claim missing.
- Accuracy on real repositories: a `missing` finding now passes the default threshold
  only with evidence that the documentation names one specific declaration (an explicit
  `{class}`/`:func:` role, a near-miss of one declared flag or path, a wrong-dash flag,
  a `make` target absent from a static Makefile, or "`--long` or `-s`" pairs that the
  code assigns to different options). Many false positives are gone: `None` code defaults,
  free-form schema maps, quoted option values and pass-through arguments, defaults bound
  to the wrong setting, hedged, placeholder, gitignored and other repositories' paths,
  and renames to an ancestor key or another naming convention. A tracked `build/`
  package is now indexed, and a locked index cache no longer fails the scan.

## [0.1.0] - 2026-10-24

### Added

- Static checks for commands, flags, defaults, environment variables, routes,
  symbols, files, package scripts and Make targets, with partial ecosystem coverage.
- Evidence chains connecting documentation to source and the Git commit that
  introduced drift, with explicit limits for shallow or truncated history.
- Terminal reports with stale-token carets; JSON, SARIF, Markdown, JUnit, GitHub
  annotations and offline HTML reports with an interactive evidence graph.
- Explanations, conservative fix previews, CI checks, baselines, suppressions,
  workspace scoping, change impact and repository insights.
- Optional watch mode, language server and VS Code client; optional model
  verification with redaction, budgets and explicit opt-in.
- A local web demo with cached examples and bounded public-repository scanning.
- Reproducible public-repository benchmark samples for human review, plus docs
  and configuration reference. Real-world precision and recall remain unmeasured.
- Package version in CLI and JSON reports, separate from JSON schema version 1.
- PR comparisons with documentation lines, source evidence, available commit
  attribution, workflow annotations and reviewable documentation patches.
- Source-scoped human feedback with reasons and conservative reopening when
  supporting source changes; full findings remain available for review.
- Bounded static support history, CODEOWNERS routing, documentation coverage
  reports and verified local fix branches that leave final approval to a person.

### Changed

- Static Typer checks respect default and explicitly configured help options.
- Markdown explanations are presented as review text; applicable changes use
  exact-span patches rather than token-only whole-line suggestions.


Changelogs are ignored by default. Historical examples such as `--port` may stay
in this file without being treated as current usage instructions.

[Unreleased]: https://github.com/ayushagarwalhere/docs-drift/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ayushagarwalhere/docs-drift/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ayushagarwalhere/docs-drift/releases/tag/v0.1.0
