# Changelog

All notable user-facing changes are recorded here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ayushagarwalhere/docs-drift/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ayushagarwalhere/docs-drift/releases/tag/v0.1.0
