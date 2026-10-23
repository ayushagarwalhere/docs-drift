# First release rehearsal: 23 October 2026

This records local preparation, not a public release. The owner requested a pause
before every publishing action and confirmed the trusted publishers are not configured.
No source push, tag push, package upload, container push or GitHub Release was performed.
The RC tag exists only in an isolated local validation repository with no origin remote.

## Validated locally

- A clean isolated source snapshot was committed and tagged `v0.1.0-rc.1` solely
  for local validation. `uv build` produced `0.1.0rc1` wheel and sdist from this
  actual Git tag, with all version-override environment variables removed.
- Archive content checks and `twine check --strict` passed for both distributions.
- The wheel and all test/runtime extras were installed into a fresh Windows
  Python 3.13 environment. The import path was asserted to be in site-packages.
- All **685 tests** passed in a source-free staged test tree (four groups:
  140, 119, 188 and 238). The staging directory contains no `src` directory.
  The GitHub workflow stages these inputs under runner temporary storage,
  outside its source checkout. These local tests are not a successful GitHub run.
- A separate local `pipx install` of the RC wheel succeeded, followed by
  `docs-drift --version` and a synthetic fixture scan. CLI and JSON both reported
  package version `0.1.0rc1`; the JSON schema version stayed `1`.
- A synthetic example HTML report and release-notes preview were generated from
  the installed wheel. Neither is presented as a customer incident or public demo.
- Ruff, formatting, strict mypy on Windows/Linux, strict MkDocs, actionlint,
  dependency audit, full existing Git-history secret scan, and the isolated
  snapshot secret scan passed. The dependency audit found no known vulnerabilities.
- The own-repository gate passed at confidence 0.6 after an ambiguous filename
  reference in the setup guide was corrected. The final guide correction and this
  verification note are documentation-only changes after the RC artifact snapshot.

## Release changes

The first stable `v0.1.0` still requires a successful RC release workflow at the
same real repository commit. Later stable versions can start from one version tag,
retaining build/tests and owner approval gates. No duplicate version file was added.
The 0.1.0 changelog entry summarizes user-facing features; its date remains
Unreleased until the real release preparation is finalized.

Two tests that depended on source-tree asset paths now use installed package
resources. This preserves the checks in wheel-only execution instead of skipping
them. The documentation generator also preserves owner-authored voice evidence.
The supplied release requirement is saved with wording/recognition errors intact,
normalized transport whitespace, and a SHA256 digest; Wispr input is owner-reported,
not independently audio/video verified.

## Evidence and remaining steps

Local artifacts and hashes: `.docs-drift/first-rc-dist/`, including `rehearsal.json`,
`SHA256SUMS`, wheel, sdist, `release-notes.md`, and `example-report.html`.
Full installed-wheel commands/logs: `.docs-drift/rc-wheel-verification/`.
Dependency and secret-scan reports remain under `.docs-drift/rc-*`.

The owner must configure the PyPI/TestPyPI trusted publishers and GitHub environment
reviewers. Then review and finalize the real source commit and release date before
separately confirming its source/tag push. Never push the isolated rehearsal history.
Subsequent confirmations cover TestPyPI, GHCR and the GitHub prerelease; after the
RC's real installation matrix succeeds, stable PyPI publication is confirmed separately.
Public `pipx install docs-drift` is not yet verified. See [the walkthrough](releasing.md).
