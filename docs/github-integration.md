# GitHub integration: PR reviews and tracking issues

docs-drift can publish its findings to GitHub in three ways. All of them share one
core (`docs_drift.github`), the same gate and the same escaping:

| Path | Who it is for | What it posts |
| --- | --- | --- |
| Composite Action, `mode: pr-review` | Most repositories; no App to install | Sticky PR summary, inline suggested changes, optional Check Run |
| Composite Action, `mode: issue` | Scheduled scans of the default branch | One tracking issue, updated in place, closed at zero |
| [Hosted GitHub App](github-app.md) | Organizations that want zero workflow YAML | Check Run, sticky summary, inline suggested changes |

The default Action mode is still `check`: gate, annotations and artifacts only, no
write token. Publishing is strictly opt-in.

## What a PR review looks like

- **One sticky summary comment.** It carries a hidden marker
  (`<!-- docs-drift:summary -->`) and is edited in place on every push, never
  duplicated. It shows the gate verdict, a verdict table, the findings with
  permalinks pinned to the scanned commit, and a link to the workflow run that
  holds the full HTML/JSON/SARIF report. When drift is fixed the comment collapses
  to "✅ no new drift"; a PR that never had drift gets no comment at all.
- **Inline review comments with GitHub suggested changes** for findings on lines
  that are part of the PR diff. When the fix is certain (a confirmed rename, a
  changed default, a removed flag; the same exact-span edits used by
  `docs-drift fix`), the comment contains a `suggestion` block that reviewers can
  commit with one click. GitHub rejects review comments outside the diff, so those
  findings are listed in the summary instead.
- **No repeated comments.** Each inline comment carries a marker with a key
  derived from its claim IDs and a content hash. Re-runs skip identical comments;
  a changed finding gets a new comment and the old one is edited to say it was
  superseded; a fixed finding's comment is edited to say it is resolved.
- **Optional Check Run** (`check-run: 'true'`) with annotations and a conclusion
  derived from the existing gate: `failed` → failure, `passed` → success,
  report-only → neutral. Only GitHub App tokens can write Check Runs: the
  Actions `GITHUB_TOKEN` works with `checks: write`, personal access tokens never
  do. If the Check Run cannot be created, a warning is logged and the review and
  summary are still published.

Comments are only reused, updated or marked resolved when they were written by
the publishing identity (`github-actions[bot]`, the App's bot, or the token's
user); copied markers in other people's comments are ignored. An inline comment
whose line became outdated is posted again on the current line.

When the Action's `repo:` input points at a subdirectory, comment paths, permalinks
and annotations are still relative to the repository root.

Only drift that the PR introduces is counted: a finding is new when it did not
exist at the base revision or its verdict changed, exactly like `docs-drift pr`.
Lines are compared against the merge base, like GitHub's own PR diff, so commits
that land on the base branch later are not attributed to the PR. Existing drift on
lines the PR edits is shown separately ("existing drift on lines
this PR edits") with suggestions, but it never fails the gate.

## Action: comment on pull requests

Copy [examples/workflows/docs-drift-pr.yml](https://github.com/ayushagarwalhere/docs-drift/blob/main/examples/workflows/docs-drift-pr.yml):

```yaml
name: Documentation drift
on:
  pull_request:
permissions:
  contents: read
  pull-requests: write   # sticky summary + inline suggestions
  checks: write          # only when check-run is 'true'
jobs:
  docs-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
      - uses: ayushagarwalhere/docs-drift@<reviewed-commit-sha>
        id: drift
        with:
          base-ref: ${{ github.event.pull_request.base.sha }}
          comment: 'true'
          check-run: 'false'
```

Check out the PR **head** commit (not the default merge commit) so inline comment
line numbers match the lines reviewers see, and fetch full history so the base
commit and attribution are available.

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `mode` | `check` | `check`, `pr-review`, `issue`, or `auto` (pr-review on `pull_request`, issue on `schedule`/`workflow_dispatch`) |
| `comment` | `'false'` | `'true'` turns `check` into `auto`; existing workflows are unaffected |
| `github-token` | `${{ github.token }}` | Token for publishing only |
| `check-run` | `'false'` | Also create a Check Run in pr-review mode |
| `suggestions` | `'true'` | Attach suggested changes when the edit is certain |
| `issue-label` | `docs-drift` | Label that identifies the tracking issue |

The new output `bundle-path` points at the JSON bundle the publish step consumed.
All earlier inputs and outputs are unchanged.

### Permissions

| Mode | Required `permissions:` |
| --- | --- |
| `check` | `contents: read` |
| `pr-review` | `contents: read`, `pull-requests: write` (+ `checks: write` with `check-run: 'true'`) |
| `issue` | `contents: read`, `issues: write` |

### Token isolation

The Action runs as two steps. The **scan** step parses the checked-out repository
and never receives a token. The **publish** step receives `github-token` and only
the bundle file written by the scan step, then calls the CLI below. A publishing
failure is reported as a workflow warning and never changes the gate result.

### Fork pull requests

`pull_request` workflows triggered from forks receive a read-only `GITHUB_TOKEN`
by GitHub design. docs-drift detects this, logs
"fork pull request detected … skipping comments", and still runs the gate and
annotations, so fork PRs are checked but not commented on. A 403 from any other
read-only token (for example Dependabot) is handled the same way.

Do not switch to `pull_request_target` to get a write token: that event runs with
write permissions and secrets, and checking out the fork's code in it is the
classic "pwn request" pattern. If you need comments on fork PRs, use the
[GitHub App](github-app.md) (it never executes repository code and fetches the PR
with its own short-lived token), or a two-workflow setup where the unprivileged
`pull_request` run uploads the bundle as an artifact and a separate `workflow_run`
workflow (which never checks out PR code) downloads it and runs
`docs-drift github pr-review --bundle <file> --pr <number>`. Treat the downloaded
bundle as untrusted data: every repository-derived string in it is escaped again
when rendered.

## Action: scheduled tracking issue

Copy [examples/workflows/docs-drift-scheduled.yml](https://github.com/ayushagarwalhere/docs-drift/blob/main/examples/workflows/docs-drift-scheduled.yml):

```yaml
name: Documentation drift report
on:
  schedule:
    - cron: '17 6 * * 1'   # weekly, Monday 06:17 UTC
  workflow_dispatch:
permissions:
  contents: read
  issues: write
jobs:
  docs-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: ayushagarwalhere/docs-drift@<reviewed-commit-sha>
        with:
          mode: issue
          fail-on-drift: 'false'
```

There is at most one tracking issue per repository, found by its label and hidden
marker. It lists the findings grouped by document with permalinks pinned to the
scanned SHA, is edited in place on every run, is closed with a comment when drift
reaches zero, and is reopened with a comment if drift returns. A run never opens a
second issue while one exists, and an unchanged report makes no API writes.

## CLI

The Action's publish step calls these commands; they work locally too. They read
the token from `GITHUB_TOKEN` (or `GH_TOKEN`) only, never from an argument, and
`--dry-run` prints exactly what would be posted without writing anything (with no
token it renders the bodies offline).

```text
docs-drift github pr-review --base origin/main --pr 123 --github-repository owner/name --dry-run
docs-drift github pr-review --bundle github-bundle.json --check-run
docs-drift github issue --label docs-drift --dry-run
```

`pr-review` also accepts `--head-sha`, `--inline/--no-inline`,
`--suggestions/--no-suggestions`, `--artifact-url`, `--reviews`, `--baseline`,
`--min-confidence` and `--max-findings`, with the same meaning as the Action
inputs. Inside GitHub Actions the repository, PR number, base and head commits are
read from the event payload.

## Reliability and limits

- The REST client sends `X-GitHub-Api-Version: 2022-11-28` and follows pagination
  only on the API host. It retries rate limits (honouring `Retry-After` and
  `x-ratelimit-reset`, waiting a minute for secondary limits without them, and
  failing fast when the wait would exceed 90 seconds), connection failures, and,
  for idempotent requests only, 5xx responses and timeouts. A POST that may have
  reached GitHub is never repeated; the next run reconciles by marker instead of
  duplicating comments.
- Comments are capped below GitHub's 65,536-character limit; long reports are
  truncated with a note pointing at the full report artifact.
- At most 30 inline comments are posted per run; the rest stay in the summary.
- GitHub's REST API cannot resolve review threads, so resolved findings are marked
  by editing the earlier comment; reviewers can collapse the thread.
- Static support is not runtime verification, and a passing gate does not prove
  the documentation is correct.
