# Drift age and fix-by policy

When Git history identifies the commit that introduced a drift finding, the
finding records how long the documentation has been stale:

| JSON field | Meaning |
| --- | --- |
| `stale_since` | UTC date the introducing commit landed on the branch (its committer date). |
| `age_days` | Whole days from that commit to the HEAD commit. Never negative. |
| `fix_by` | `stale_since` plus `max_drift_age_days`, when the policy sets a maximum. |

Ages are measured against the HEAD commit date, not today's date. Both ends use
the committer date, so a commit that arrives through a rebase merge or a
cherry-pick starts its age on the day it landed, not on the day it was first
written. Scanning the
same commit always gives the same ages and due dates, in CI or on a laptop a
month later. Findings without history attribution, and findings that are not
drift, leave these fields `null`. Reports saved before these fields existed
still load.

Terminal, Markdown and HTML reports show the age next to the introducing commit,
for example `Stale since 2026-10-02 (3 days at HEAD) · fix by 2026-11-01`.

## Policy

Add a `[policy]` table to `.docs-drift.toml` (or `[tool.docs-drift.policy]` in
`pyproject.toml`):

```toml
[policy]
max_drift_age_days = 30
warn_drift_age_days = 14
fail_unknown_age = true
```

| Setting | Effect |
| --- | --- |
| `max_drift_age_days` | `docs-drift check` fails only for drift older than this many days, and every attributed finding shows its fix-by date. |
| `warn_drift_age_days` | Drift older than this is marked `WARNING` in reports. It must not exceed the maximum. |
| `fail_unknown_age` | With a maximum set, whether findings with no known age (no history attribution, a shallow clone, unverifiable claims under `--fail-on any`) still fail the check. |

Both ages are optional and have no value until you set them. Unknown ages fail
the check unless you opt out.

"Older than" is strict: with `max_drift_age_days = 30`, drift becomes overdue on
day 31, the day after its fix-by date.

Without a `[policy]` table, `docs-drift check` behaves exactly as before. The age
policy is applied after the existing filters (`--fail-on`, `--min-confidence`,
`--baseline` and `--reviews`) and before `--max-findings`. It can only remove
failures, never add them.

`docs-drift check --max-drift-age 30` sets the maximum for one run and overrides
the configuration. A configured warning threshold above it is ignored for that
run.

When a policy is active, `check` adds a summary to the report warnings, for
example `Drift age policy: 2 older than 30 days (overdue), 1 within policy, 0
without history attribution (gated as overdue).`

Shallow CI checkouts have no history to attribute, so every finding has an
unknown age. History attribution also covers only the last
`history.max_commits` commits (2000 by default), so in a large repository the
oldest drift is often the drift with an unknown age. Fetch full history in CI
(on GitHub Actions, set `fetch-depth: 0` on the checkout step) and, if needed,
raise `history.max_commits`.

`fail_unknown_age = false` is not a grace period: drift with an unknown age then
never fails `check`, however old it is. Use it only when you accept that
unattributed drift is reported but not gated.

## Insights

`docs-drift insights` includes a `drift_age` object:

- `histogram`: counts in the buckets 0–7, 8–30, 31–90, 91–365 and 366+ days,
  plus `unknown`, in that order;
- `median_age_days` and up to 10 `oldest` findings with their fix-by date and
  policy status;
- `status`: counts of `fresh`, `warning`, `overdue` and `unknown` findings.

To see how drift changes over time, save JSON reports into one folder (for
example one per release, with `docs-drift scan --format json --output
reports/v1.json`) and pass it to `--trend`:

```console
docs-drift insights --trend reports
```

The `trend` list has one entry per report, ordered by HEAD commit time, with the
drift count, the median age and the oldest age. The folder path is relative to
`--repo`. At most 100 reports of up to 25 MB each are read. Other JSON files are
skipped with a warning.
