# Contributor mode

`docs-drift contribute` turns drift findings into issue drafts that a new
contributor can pick up. Each draft gives the exact file and line, the current
text next to the corrected text, links to the evidence, the commands to check the
fix locally, a difficulty rating, suggested labels and, when a rule fix is
certain, a ready patch.

Nothing is posted anywhere. You review the drafts, then file the ones you want.

```console
docs-drift contribute                         # all drafts in one Markdown digest
docs-drift contribute --out-dir doc-issues    # one Markdown file per draft
docs-drift contribute --format json           # the same drafts as JSON
docs-drift contribute --summary               # a docs health card for CONTRIBUTING.md
```

| Option | Meaning |
| --- | --- |
| `--out-dir DIR` | Write one Markdown file per draft into this folder, relative to the current directory. With `--format json`, also write `issues.json`. |
| `--summary` | Print the health card instead of drafts. Combine it with `--format json` for the raw counts. |
| `--min-confidence N` | Only use findings at or above this confidence. Without it, the configured `min_confidence` threshold applies. |
| `--limit N` | Keep at most this many drafts (1–500, default 50). The report marks when more were available. |

The global `--output` option writes the digest or health card to a file.
Only the terminal, Markdown and JSON formats are accepted.

## Which findings become drafts

A drift finding (missing, renamed, changed default or invalid command) becomes a
draft when its confidence meets the threshold and at least one of these holds:

- `docs-drift fix` has a certain rule edit for it;
- Git history identified the commit that introduced the drift;
- the scanner matched a current replacement symbol.

Other drift is counted as skipped in the report, so you can see how much was left
out. Unverifiable findings never become drafts.

## Grouping

Related findings share one draft:

- the same renamed name, with the same replacement, anywhere in the docs;
- otherwise, findings in the same document section.

A draft holds at most 20 locations. Larger groups are split into several drafts.

## Difficulty and labels

| Difficulty | When |
| --- | --- |
| good first issue | Every location has a certain rule edit, and the patch changes a single line. |
| easy | Every location has a certain rule edit across several lines. |
| needs investigation | At least one location has no certain edit. The draft says what to check. |

Suggested labels are `documentation`, `docs-drift`, `good first issue` when it
applies, one `drift: <verdict>` label per verdict and `overdue` when the
[drift age policy](drift-age.md) says the drift is past its fix-by date.

## Evidence links

When the `origin` remote is a GitHub repository and HEAD is known, file links
point at `https://github.com/OWNER/REPO/blob/<HEAD sha>/<path>#L<line>` and
commit links at the commit page. Links are pinned to the scanned commit, so they
keep pointing at the evidence after the branch moves. Remotes that are not on
GitHub, or that carry credentials, produce plain relative paths. With
`--docs-repo`, documentation lines are shown as relative paths because they live
in a different checkout.

## Safety

- Drafts are deterministic: the same checkout gives byte-identical output. Draft
  IDs are hashes of their claim IDs, and file names combine a sequence number, a
  title slug and that hash.
- Repository text is untrusted. Markdown escapes HTML, mentions, links, pipes and
  backticks, and puts excerpts in fences that cannot be closed early. Lines that
  look like secrets are replaced with `[REDACTED]`.
- Titles and excerpts are bounded. Each draft holds at most 8 evidence excerpts
  of up to 12 lines.
- `--out-dir` writes only the draft files it names. It never deletes files, so
  remove drafts from earlier runs yourself.

## Docs health card

`docs-drift contribute --summary` prints a short Markdown block for
CONTRIBUTING.md: the health score, counts by verdict, the documents with the most
drift, the oldest attributed drift and how many drafts are ready. Every count
uses the same `--min-confidence` threshold, and the draft count includes drafts
beyond `--limit`. It sits between
`<!-- docs-drift:health-card:start -->` and `<!-- docs-drift:health-card:end -->`
markers, so you can replace it on the next run.

The card names files and lines but never the stale values themselves. Pasting it
into a scanned document cannot create new findings.
