# AI coding agents (MCP)

docs-drift ships a [Model Context Protocol](https://modelcontextprotocol.io) server so
AI coding agents such as Claude Code, Cursor, VS Code Copilot and Windsurf can check
documentation while they edit. Ask the agent things like:

- "Check docs drift."
- "Is my README still accurate after this rename?"
- "Check this paragraph before you add it to the docs."

The server runs the same static pipeline as the command line. It never imports or
executes code from your repository, and only `apply_fix` writes files.

## Install

The server needs the optional `mcp` extra (the official MCP Python SDK, version 2.3 or
newer). The examples below run it with [uv](https://docs.astral.sh/uv/), so nothing has
to be installed globally:

```text
uvx --from "docs-drift[mcp]" docs-drift mcp
```

Or install it into an environment and run the console script directly:

```text
pip install "docs-drift[mcp]"
docs-drift mcp --repo /path/to/repo
```

The MCP command first ships in docs-drift 0.1.1. Until that release is on PyPI, point
uvx at the Git repository:
`uvx --from "docs-drift[mcp] @ git+https://github.com/ayushagarwalhere/docs-drift" docs-drift mcp`.

The server uses stdio by default. The repository root is `--repo`. Without it, the
server uses `CLAUDE_PROJECT_DIR` when that environment variable is set, and otherwise the
directory the agent starts the server in.

## Set up your agent

### Claude Code

Run this once in your project:

```text
claude mcp add docs-drift -- uvx --from "docs-drift[mcp]" docs-drift mcp
```

Claude Code sets `CLAUDE_PROJECT_DIR` to the project root, and the server uses it when
`--repo` is omitted. Other launchers use the working directory, so pass `--repo` when it
may differ from the repository. Add
`--scope project` before `docs-drift` to share the server with your team through a
checked-in project configuration file.

### Cursor

Create `.cursor/mcp.json` in the repository:

```json
{
  "mcpServers": {
    "docs-drift": {
      "command": "uvx",
      "args": ["--from", "docs-drift[mcp]", "docs-drift", "mcp", "--repo", "${workspaceFolder}"]
    }
  }
}
```

### VS Code (Copilot agent mode)

Create `.vscode/mcp.json` in the repository:

```json
{
  "servers": {
    "docs-drift": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "docs-drift[mcp]", "docs-drift", "mcp", "--repo", "${workspaceFolder}"]
    }
  }
}
```

### Windsurf and Claude Desktop

<!-- docs-drift-ignore: client configuration lives outside the repository. -->
Both use the `mcpServers` format. Edit `~/.codeium/windsurf/mcp_config.json` (Windsurf)
or `claude_desktop_config.json` (Claude Desktop) and use an absolute repository path:

```json
{
  "mcpServers": {
    "docs-drift": {
      "command": "uvx",
      "args": ["--from", "docs-drift[mcp]", "docs-drift", "mcp", "--repo", "/absolute/path/to/repo"]
    }
  }
}
```

### Other clients and HTTP

Any MCP client that can launch a stdio command works with the same command line. For
clients that only speak HTTP, start a local Streamable HTTP endpoint at
`http://127.0.0.1:8765/mcp`:

```text
docs-drift mcp --transport streamable-http --port 8765
```

The HTTP transport has no authentication. Keep `--host` at the default `127.0.0.1`.

## Tools

| Tool | Writes? | What it does |
| --- | --- | --- |
| `check_docs` | no | Scans every configured document and returns drift findings with locations, suggestions, evidence, confidence and the commit that made each one stale. Optional filters: document globs, minimum confidence, include ok or unverifiable claims, maximum findings, and a switch to skip Git history. |
| `check_document` | no | Checks one saved document. It is faster than a full scan and returns certain suggested edits. |
| `check_text` | no | Checks unsaved Markdown or reStructuredText as if it lived at a given path, without writing to disk. Use it while drafting. |
| `explain_finding` | no | Returns full evidence for one finding id: reasons, matched source symbol, rule code, commit and whether a certain fix exists. |
| `preview_fix` | no | Returns a unified diff of certain, exact-span edits for the given finding ids, a `plan_token` for `apply_fix`, and notes for findings that need a human rewrite. Works for configured documents and for saved files checked with `check_document`. |
| `apply_fix` | **yes** | Writes the edits that `preview_fix` showed. Pass the preview's `plan_token` to write exactly that diff or nothing. It is marked destructive, so clients ask before running it, and it reports which files it wrote and skipped. |
| `coverage` | no | Lists documents with their status, claim and drift counts and declared CODEOWNERS. |
| `supported_surfaces` | no | Lists claim kinds, verdict meanings and per-language support levels. |

Every tool returns structured JSON that matches its declared output schema. Finding
ids are short prefixes such as `claim_8ed4cec49e`; any unique prefix works.

Resources:

- `docs-drift://report/latest` returns the current drift findings as JSON.
- `docs-drift://supported-surfaces` returns the support table as Markdown.

Prompts:

- `review-docs-after-change` finds docs made stale by a change, explains them,
  previews fixes and asks you before applying them.
- `write-docs-safely` drafts documentation and checks it with `check_text` until no
  drift remains.

## Command options

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo PATH` | current directory | Repository root. Tools cannot read outside it. |
| `--allow-root PATH` | not set | Another root that tools may inspect through their `repo` argument. You can repeat it. |
| `--timeout SECONDS` | 120 | Time limit for each tool call. |
| `--transport` | stdio | `stdio` or `streamable-http`. |
| `--host`, `--port` | 127.0.0.1, 8765 | Address for `streamable-http`. |
| `--config PATH` | auto | Explicit configuration file, as for every other command. |
| `--verbose` | off | Log tool activity to stderr. |

## Safety

- **Static only.** The server parses source text and reads Git history through the same
  hardened Git calls as the CLI. It never imports, builds or runs project code.
- **Read-only by default.** Only `apply_fix` writes files. It has the MCP annotations
  `readOnlyHint: false` and `destructiveHint: true`; every other tool is marked
  `readOnlyHint: true`. Writes use the same fixer as the fix command: exact spans only,
  a content-hash check against the scanned file, and an atomic replace. With the
  preview's `plan_token`, `apply_fix` writes exactly the previewed diff or nothing. Every
  file is verified before the first write, a stale plan fails closed (unless the caller
  omits the token and opts in with `rescan_if_stale`), and source code is never edited.
- **Cancellation is safe.** `apply_fix` is never abandoned at its deadline: the worker stops
  before its next write and the response says exactly what was written. Read-only scans
  stop at their next checkpoint when they time out, so a retry is not blocked for long;
  a call that cannot start because an earlier one is still finishing says so.
- **Confined paths.** Document paths are resolved and must stay inside the repository
  root. Traversal such as `../`, symbolic links that point outside the root, `.git`
  internals and non-documentation suffixes are rejected. The `repo` argument must be
  the root or a directory inside a root allowed by `--allow-root`.
- **Bounded output.** A response lists at most 50 findings unless the agent asks for more, up to 200. Evidence is
  truncated, and lines that look like secrets (API keys, passwords, connection
  strings) are masked before they reach the model.
- **Bounded time.** Each call runs in a worker thread with a timeout. Full scans send
  MCP progress notifications when the client asks for them.
- **Clean protocol channel.** stdout carries only MCP messages; logs go to stderr.

## Caching

The first full scan builds the static index; it is cached in `.docs-drift/cache` as for
the CLI. The server also keeps the latest scan in memory and reuses it until a source
file, document, untracked file, configuration file or the Git HEAD changes. Later checks of an unchanged
repository return in well under a second, and `cached: true` shows this. Single-document
checks reuse the in-memory index, so `check_text` stays fast while you type.

## Troubleshooting

- **"MCP support is not installed".** Install the extra with
  `pip install "docs-drift[mcp]"`, or use the uvx command above. docs-drift needs the
  MCP SDK 2.3 or newer, where FastMCP was renamed to MCPServer.
- **The agent sees no tools.** Run the configured command in a terminal. It should
  wait silently for input; press Ctrl+C to stop it. Errors print to stderr. Check that
  `uvx` is on the PATH the client uses, or use an absolute path to it.
- **"outside the repository root".** Pass paths relative to the repository, or start the
  server with the right `--repo` (or an extra `--allow-root`).
- **"did not finish within ...".** Large repositories with long histories can be slow on
  the first scan. Pass `history: false`, narrow `check_docs` with `paths`, or raise
  `--timeout`.
- **Few or no findings.** Findings below the repository's `min_confidence` are hidden.
  Check `summary.hidden_below_confidence` and lower `min_confidence` to review them.
  Unsupported frameworks produce `unverifiable` findings, which are not errors; see
  `supported_surfaces`.
