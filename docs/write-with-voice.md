# Write with voice

When you dictate documentation you write from memory, and memory goes stale. You
say the flag you remember, the environment variable from last quarter, the default
before someone changed it. docs-drift checks each of those statements against the
code while you are still writing, before the text reaches a commit.

Dictation goes into docs-drift in three places.

| Where you speak | What checks it | Setup |
| --- | --- | --- |
| The **Write with voice** page of the web demo (`/write`) | The saved static index of a scanned repository | None: open the page and hold your dictation key |
| Your AI coding agent | The MCP `check_text` tool | [MCP guide](mcp.md) |
| A Markdown file in VS Code | The language server, as diagnostics on each change | [VS Code guide](vscode.md) |

Any dictation tool works, because each of these is an ordinary text field:
Wispr Flow, macOS Dictation and Windows Voice Typing all type wherever the cursor
is. Typing works too.

## The /write page

1. Choose what to check against: a saved example (the default is the `py-cli`
   fixture) or a repository you scanned in the same browser session.
2. Click into the draft and dictate. The page re-checks the text about 600 ms
   after the last change, which suits speech that arrives in bursts.
   <kbd>Ctrl</kbd>+<kbd>Enter</kbd> (<kbd>⌘</kbd>+<kbd>Enter</kbd> on macOS)
   checks immediately.
3. Each claim appears in the findings panel with its verdict, the evidence in the
   code (`file:line`) and, when one is known, a suggestion. **Apply suggestion**
   rewrites only that span of the draft, and the browser's undo reverts it.
4. **Copy as Markdown** copies the draft for your README or docs site.

<!-- docs-drift-ignore: these names belong to the bundled example repositories, not docs-drift. -->
The default sample shows every verdict on the `py-cli` fixture: `serve` is ok,
`--port` was renamed to `--listen`, the `--workers` default changed from 4 to 8,
and `APP_DEBUG` is missing (the code now reads `APP_LOG_LEVEL`).

Findings use the same verdicts as every other docs-drift surface
([claims and verdicts](claims.md)). Two kinds of suggestion are shown:

<!-- docs-drift-ignore: the names below belong to the bundled example repositories, not docs-drift. -->
- **Apply suggestion** (lime) is an edit the conservative rule fixer is certain
  of, the same edit `docs-drift fix` and the language server's quick fix make.
- **Use …** (white) is a name the code declares that is tied to the claim by the
  saved scan's history or a close spelling (for example `--line-number` and
  `--line-numbers`). It is not a confirmed rename; review it before keeping it.

## How a check works without the clone

Live scans in the web demo delete the cloned repository as soon as the scan
finishes. To check text later, the scan keeps a compact copy of its static index:
symbols, source file paths, package roots and the three packaging metadata files
(pyproject.toml, package.json and setup.cfg). No other source text is kept, and
the copy is scrubbed for secrets exactly like the public report.

A check rebuilds a skeleton tree from that copy, with metadata files and empty
placeholders only, and runs the same in-memory document path as the MCP
`check_text` tool and the language server. Extraction, matching and the rule fixer
are reused unchanged.

The bundled examples ship the same index files in
`src/docs_drift/web/examples/indexes/`, generated through the production worker by
`scripts/build_write_indexes.py` (fixtures are rebuilt deterministically; public
examples are fetched at their pinned commit).

### What it cannot know

Git history is not available for text you write, so the page does not run the
history search that the CLI uses to confirm renames. A rename or default change is
reported only when the static index shows it (an exact default mismatch, or an
index near-miss), or when the saved scan of the same revision traced the identical
claim through Git history. In that case the finding says so and names the commit.
Otherwise a missing name stays **missing**, and the closest declared name is only
offered as a "did you mean".

Indexes larger than 4 MB, or repositories with more than 20,000 source files, are
not kept. The page then explains why and points to the CLI and the VS Code
extension, which check the full checkout.

## Limits and safety

- Text is limited to 20 KB per check. The checked path must be a relative
  documentation path (`.md`, `.mdx`, `.markdown`, `.rst` or `.txt`).
- Checks have their own rate limit, 60 per minute per client by default
  (`DOCS_DRIFT_WRITE_RATE_LIMIT`), kept in the same salted, in-memory bucket store
  as scans and separate from the scan allowance.
- Requests from another origin are rejected, as for scans. No repository code is
  run, nothing is written to a repository, and findings are rendered as text.
- At most two checks run at once, each with a 15 second limit.

## In your editor and agent

The same check runs where you already dictate:

- **VS Code**: with the extension installed, dictate into a Markdown file and the
  language server marks stale flags, commands and settings as you speak, with quick
  fixes for certain edits. See the [VS Code guide](vscode.md).
- **AI coding agents**: dictate a request such as "check this paragraph against the
  code before you add it to the README" and the agent calls the MCP `check_text`
  tool with the paragraph. See the [MCP guide](mcp.md).
