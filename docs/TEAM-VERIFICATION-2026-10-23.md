# Team workflow verification, 23 October 2026

Scope: local changes after `4a0a6b0`, reviewed as an uncommitted working tree.
This is implementation and synthetic verification evidence, not an adoption or
real-world precision result. No public release or project dictation is claimed.

## Implementation

- `pr`: compare a fetched base with the current checkout, preserve full reports
  in the Action, annotate new/changed drift, and export HTML, Markdown, JSON,
  annotations, coverage, decisions and exact-span rule patches.
- `review record/list`: source/context fingerprints, required reasons and
  reviewers, retained history, conservative reopening. `stale` stays actionable;
  intentional/false-positive decisions affect only explicitly review-aware gates.
- `timeline`: bounded first-parent archived static scans with unknown attribution
  left null. This is not a complete history or runtime behavior measurement.
- `coverage`: checked and explicitly important unchecked documents, unmatched
  claims, unverifiable claims and declared CODEOWNERS routing.
- `fix-branch`: new local worktree, certain rule edit, rescan, then a tool-authored
  proposal commit. No push or merge. A hosted interactive fix button is not shipped.

Git conversion filters are disabled when creating archives, inspecting proposal
status and committing proposals. Direct byte fingerprints replace filter-capable
Git modified-file checks. Worktrees are populated from bounded archives; patches
are staged through raw blobs, without checkout or clean filters. Existing exact
edit spans and stale-document checks remain enforced.

## Automated verification

All 42 test files ran in four disjoint subprocess groups, totaling **680 passed**:
190, 131, 172 and 187. Per-group coverage gates were disabled only so coverage
could be combined; the unchanged aggregate gates were then enforced.

Local commands and logs are under `.docs-drift/team-full-verification/`:

```sh
python -m coverage combine --keep --data-file=.docs-drift/team-full-verification/.coverage .docs-drift/team-full-verification
python -m coverage json --data-file=.docs-drift/team-full-verification/.coverage -o .docs-drift/coverage.json
python -m scripts.check_coverage
```

| Coverage scope | Covered / statements | Result |
| --- | ---: | --- |
| Overall | 7270 / 8322 (87.36%) | Pass, minimum 85% |
| Extraction | 1219 / 1306 (93.34%) | Pass, minimum 90% |
| Matching | 335 / 352 (95.17%) | Pass, minimum 90% |

Ruff checks and formatting pass; strict mypy passes for all 166 checked files.
MkDocs strict builds pass. The repository's own check at confidence 0.6 passed:
397 findings retained, zero qualifying drift. Sandbox ownership required a
process-local `git -c safe.directory=<this exact repository>` wrapper; no global
Git trust setting was changed.

`tests/test_team_workflows.py` exercises real temporary Git histories, adjacent
support boundaries and unknown history, scoped decision reopening, retained full
reports, Action inputs and artifacts, ownership precedence, unchecked important
documents, HTML escaping, uncertain-fix refusal, clean-checkout protection, and
verified isolated fixes. A synthetic filter marker confirms filter commands do
not execute. Existing CLI, policy, report, index and release tests also pass.

Initial targeted runs exposed Windows line-ending differences, and the filter
regression test exposed Git archive smudge execution. Both were fixed before the
full passing run. Markdown token/explanation text is no longer emitted as an
applicable GitHub whole-line suggestion.

## Distribution and review artifacts

Wheel and source distributions build from a source snapshot, using
`SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0.dev0` for local development artifacts only.
That override is not used for publishing. Archive validation checks include the
new modules and HTML template. Twine strict metadata checks pass for both archives.

A fresh Windows Python 3.13 environment installed the wheel and optional runtime
extras offline. An import-path assertion confirmed `site-packages` was used.
Outside the source tree, the installed commands passed scan, PR report, timeline,
fix-branch verification, feedback record/list, coverage and review-aware check.
The smoke repository initially lacked a cache ignore rule; its normal scan made
the checkout unclean, and fix-branch correctly refused it. Adding a local ignore
rule to that synthetic fixture allowed the intended clean-checkout test.

Local evidence was generated in ignored output directories; these are local run
outputs, not files shipped in a fresh source checkout:

```text
.docs-drift/team-wheel-smoke/verification.json
.docs-drift/team-wheel-smoke/review-final/pr.html
.docs-drift/team-wheel-smoke/pr-desktop-verified.png
.docs-drift/team-wheel-smoke/pr-mobile-verified.png
```

The JSON labels the synthetic history and proposal commit. The HTML is the
installed-wheel PR report. Screenshots record local Edge rendering at 1280px and
375px; the mobile layout uses a scrollable coverage table without page overflow.

The last changes after the full suite are documentation and responsive HTML/CSS;
the affected workflow/report tests and rendering checks are repeated separately.

## External evidence still required

Both PyPI and TestPyPI returned HTTP 404 for `docs-drift` during this session.
No publishing token was present in the checked environment variables, and no
`.pypirc` or netrc configuration file was present. Browser discovery returned no
available browser sessions. No authenticated publishing access was established.
The existing trusted-publisher release workflow remains the path to a public
release, including its real tag and installation matrix. Local checks do not
prove that GitHub-hosted release jobs or public installation succeeded.

The supplied Flow account cards retain their existing provenance. This session's
requirement input method is unverified. No dictated transcript, project-only
usage total, video, maintainer endorsement or CI adoption result was fabricated.
