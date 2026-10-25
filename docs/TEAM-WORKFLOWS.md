# Team workflows

These commands extend the existing scanner, reports, baselines and safe rule fixes.
Start unfamiliar repositories in report-only mode. Static support is not runtime
verification; real-world precision and maintainer adoption remain unestablished.

## Install and distribute

Python 3.11+ and Git are required. Until the first verified PyPI release, install
from the repository or distribute a built wheel internally:

```sh
uv build
pipx install ./dist/docs_drift-<version>-py3-none-any.whl
docs-drift --version
docs-drift --help
```

Replace the placeholder with the actual wheel filename. After a verified public
release, organizations can use `pipx install docs-drift==<released-version>` or
`uv tool install docs-drift==<released-version>`. Pin a reviewed version in CI.
See [release verification](releasing.md) for TestPyPI, trusted publishing and
the installation matrix. A locally built development wheel is not a PyPI release.

## PR drift review

Fetch the base commit, then compare it with the current checkout:

```sh
docs-drift pr --base origin/main --report-dir ../docs-review
```

Open the generated HTML report in that output directory. Artifacts include documentation lines, source
evidence from the base and head, available attributed commits, ownership,
coverage, Markdown, workflow annotations, JSON and an exact-span `fix.patch`.
The directory must be new so previous review evidence is preserved.

Only newly observed or changed drift is selected. Existing unchanged drift stays
in the full scanner report. A comparison range is not an exact breaking commit;
unavailable commit attribution stays unknown. The current checkout may include
local edits; the reported head SHA identifies the committed revision only.

For the composite Action, check out the PR head with full history and set
`base-ref` to the base SHA. Retain the existing `if: always()` artifact upload:

```yaml
- uses: actions/checkout@v6
  with:
    fetch-depth: 0
- uses: ayushagarwalhere/docs-drift@<reviewed-commit-sha>
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}
    fail-on-drift: 'false'
    # reviews: .docs-drift-reviews.json  # after recording and committing decisions
```

Replace the source placeholder with a reviewed commit. No public moving release
tag is asserted here. Use `permissions: contents: read`; no write token or model
is required. The Action emits line annotations and saves the PR artifacts alongside the full
scan reports, new-finding reports and gate summary. Explicit `reviews` inputs must exist and remain
inside the selected repository. A missing base revision fails visibly.

## Reviewer feedback

Use the exact claim ID from a JSON report:

```sh
docs-drift review record CLAIM_ID --decision false-positive --reason "Source-backed explanation" --reviewer "Reviewer name"
docs-drift review list
docs-drift check --reviews .docs-drift-reviews.json
```

Choices are `stale`, `intentional`, and `false-positive`. `stale` remains actionable.
The other decisions exclude that fingerprint from the explicit review-aware gate
and proposed edits. Full scan findings remain visible. Decisions include a reason,
reviewer and timestamp; names are supplied attribution, not authenticated identity.
Commit the review file so decisions travel with the repository.

Fingerprints bind the claim, documentation context, verdict and supporting source.
Missing-source findings depend on the complete bounded source index. Source or
claim changes reopen decisions conservatively, retaining their history. This is
not a permanent pattern ignore. Existing baseline behavior remains available.

For review-aware PR artifacts, add `--reviews .docs-drift-reviews.json` to `pr`.

## Drift history

```sh
docs-drift timeline CLAIM_ID --max-commits 20
```

This statically scans up to 50 first-parent committed snapshots, without executing
target code. It reports the last statically supported snapshot and the adjacent
first observed drift when found. Missing claims or unverifiable support stop the
search. Null means unknown; shallow history, renames and claim identity changes
can prevent attribution. Archives have time and size limits.

## Ownership and coverage

```sh
docs-drift coverage --important README.md --important docs/api.md
```

Reports show checked documents, extracted claims, unmatched source evidence,
unverifiable claims, and explicitly named important documents that were not
checked. CODEOWNERS routing uses `.github/CODEOWNERS`, then root `CODEOWNERS`,
then `docs/CODEOWNERS`; the last matching supported rule wins. Unsupported
patterns produce warnings. Owners are declared reviewers, not verified identities.
Claim extraction counts do not establish semantic documentation coverage.

## Verified local fix branch

From a clean checkout (ignore local `.docs-drift/` cache files in `.gitignore`
and commit shared review decisions first):

```sh
docs-drift fix-branch CLAIM_ID --branch docs/fix-claim --destination ../docs-fix-claim
```

This creates a new Git worktree outside the source checkout, applies only an
existing certain rule edit, rescans the affected documentation line, and saves a
tool-authored local commit only when its claims verify as supported. It never
pushes or merges. Review the diff, run project-specific checks, and publish a PR
through your normal process. Findings without certain edits require manual review
and do not create a branch. A failed verification leaves an uncommitted worktree
for inspection. No model writes or application-code execution are involved.

This is a single CLI command; a hosted GitHub App with an interactive fix button
and write permissions has not been implemented or claimed.
