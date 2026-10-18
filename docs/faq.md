# FAQ

## Why not just grep?

Grep finds strings. docs-drift locates documented claims, checks scoped static
declarations/defaults and attaches Git evidence where available. It remains a
partial detector, not a proof that documentation is correct.

## Does it run my code?

No. It parses target text/AST and invokes Git with argument lists. It does not
import target modules, install target dependencies, or run fenced commands.
The self-documentation command tests execute only this project's trusted CLI.

## Will it nag me about my changelog?

No. Default discovery excludes changelogs and known historical sections. Explicit
inline suppressions are available for documented examples outside this project's
scope, but should have a reason and never hide real product documentation drift.

## What about private repos?

Use the CLI on a local checkout. The public web demo accepts only public GitHub
repositories. Rule-only mode sends no model requests; optional model mode sends
selected redacted snippets externally as documented in [security](SECURITY.md).

## Is a missing result definitely a defect?

No. An index miss, unsupported framework, sample path or third-party tool can
explain it. Human review and the pending public benchmark are essential.
