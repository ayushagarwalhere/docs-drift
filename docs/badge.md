# Docs health badge

A badge shows how many documentation statements your code no longer supports:
`0 stale` in green, or `N stale` in yellow (1–3), orange (4–9) or red (10 or more).
A statement counts as stale exactly when `docs-drift check` would fail on it with the
default `--fail-on drift`: a drift verdict (missing, renamed, changed default or
invalid command) at or above the confidence threshold, and not accepted in a baseline.

There are two ways to get one:

- **From CI** with `docs-drift badge` (recommended). The badge stays current with
  every push and never expires.
- **From the public demo** at `/badge/<owner>/<repo>.svg`. It shows the most recent
  scan the demo still holds for the repository's default branch, so it turns grey
  ("not scanned") once that scan expires, after 24 hours by default.

## The command

`docs-drift badge` runs a fresh scan, using the repository's configuration, and
prints a [shields.io endpoint](https://shields.io/badges/endpoint-badge) payload:

```console
$ docs-drift badge
{"schemaVersion": 1, "label": "docs drift", "message": "3 stale", "color": "yellow"}
```

Write the payload, a static SVG, or both:

```console
docs-drift badge --json badge.json --svg badge.svg
docs-drift badge --baseline .docs-drift-baseline.json --min-confidence 0.8
docs-drift badge --label "docs health" --json badge.json
```

| Option | Meaning |
| --- | --- |
| `--json PATH` | Write the shields.io endpoint JSON. |
| `--svg PATH` | Write a self-contained flat SVG, in the same style as shields' `flat` badges. |
| `--label TEXT` | Left-hand text, 1–40 printable characters (default `docs drift`). |
| `--min-confidence` | Override `min_confidence` from the configuration. |
| `--baseline PATH` | Count only drift that is absent from this baseline. |

Without `--json` or `--svg` the payload is printed. The command exits 0 whatever
the count; gate builds with `docs-drift check`.

## Publish it from GitHub Actions

shields.io needs a public URL for the JSON. This workflow commits the badge files
to a `gh-pages` branch on every push to `main` and weekly, so a README can read them
through `raw.githubusercontent.com`. GitHub Pages does not need to be enabled.

```yaml
name: docs badge
on:
  push:
    branches: [main]
  schedule:
    - cron: '0 6 * * 1'
permissions:
  contents: write
jobs:
  badge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: astral-sh/setup-uv@v6
      - name: Build the badge
        run: uvx --from "git+https://github.com/ayushagarwalhere/docs-drift" docs-drift badge --json "$RUNNER_TEMP/docs-drift.json" --svg "$RUNNER_TEMP/docs-drift.svg"
      - name: Publish to the gh-pages branch
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          if git fetch --depth=1 origin gh-pages; then
            git worktree add "$RUNNER_TEMP/pages" FETCH_HEAD
          else
            git worktree add --orphan -b gh-pages "$RUNNER_TEMP/pages"
          fi
          cp "$RUNNER_TEMP/docs-drift.json" "$RUNNER_TEMP/docs-drift.svg" "$RUNNER_TEMP/pages/"
          cd "$RUNNER_TEMP/pages"
          git add docs-drift.json docs-drift.svg
          git diff --cached --quiet || git commit -m "Update docs-drift badge"
          git push origin HEAD:gh-pages
```

Pin the Git source to a reviewed commit by appending `@<sha>` to its URL; once the
command is in a PyPI release, `uvx docs-drift badge` works too. The scan
reads your repository as data and never runs its code.

Then add the badge to your README, replacing `OWNER/REPO`:

```markdown
[![docs drift](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FOWNER%2FREPO%2Fgh-pages%2Fdocs-drift.json)](https://github.com/ayushagarwalhere/docs-drift)
```

or use the static SVG directly, without shields.io:

```markdown
![docs drift](https://raw.githubusercontent.com/OWNER/REPO/gh-pages/docs-drift.svg)
```

Workflow artifacts (`actions/upload-artifact`) are useful for inspecting the files,
but they are not public URLs, so shields.io cannot read them.

## From the public demo

Every finished live scan of a default branch updates the demo's badge for that
repository. The report page for that scan has a **Get a badge** snippet:

```markdown
[![docs drift](https://DEMO-HOST/badge/OWNER/REPO.svg)](https://DEMO-HOST/)
```

`/badge/<owner>/<repo>.json` serves the same count as a shields.io endpoint. Badge
requests only read a small in-memory summary: they never start a scan, so they are
cheap, cannot be used to make the demo fetch anything, and are safe to embed.
Owner and repository names are validated like scan URLs, responses carry
`Cache-Control: public` and an `ETag`, and an unknown or expired repository gets a
grey `not scanned` badge. Scans of a branch or tag do not change the badge.
