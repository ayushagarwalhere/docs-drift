<!--
Written by Codex (GPT-6), from the user's scripted fixture requirements, on 2026-10-18 (Asia/Calcutta).
This key was written before any detector existed. No detector output was used.
Never edit this key to match detector output. Only correct it if a scripted change
itself was wrong, and record the reason and affected expectations in this file.
-->

# Fixed fixture answer key

`answer_key.json` is hand-authored from `build_fixtures.py`'s documented changes.
Full SHA-1 identifiers were transcribed from the generated Git histories, not
from analysis or a detector. The builder never reads or rewrites the key.
The tests validate locations and history against the key; they cannot regenerate it.

Each entry identifies a canonical claim using repository, relative POSIX doc
path, one-based line, kind, and literal text contained on that line. Findings
include a verdict, replacement name/value (or null), and the introducing commit.
Route replacements are paths; environment replacements are variable names,
without a `process.env.` prefix. Non-findings are assertions that must not
produce drift warnings, including historical and third-party examples.

Count independent facts separately: the timeout field rename and changed default
are distinct claims from one commit. The workers flag still exists even though
its documented default is wrong. The node deployment command is invalid, while
its `deploy` subcommand still exists. Duplicate warnings for constituent tokens
must not inflate future precision/recall counts; a scoring/mapping policy must
be defined before the measurement stub computes metrics.

`APP_DEBUG` is deliberately `missing`, not a rename: the debug toggle was removed
and replaced by a setting with different semantics, `APP_LOG_LEVEL`. No replacement
is asserted for the removed toggle. Historical examples in the clean repository
must remain quiet, even though its real history contains an option rename.

## Building safely

```console
uv run python tests/fixtures/build_fixtures.py
uv run python scripts/measure.py
uv run pytest tests/test_fixtures_build.py tests/test_measure.py -q
```

The manual builder defaults to ignored `tests/fixtures/repos/`. Tests pass fresh
temporary output directories instead. Repositories already present are never
overwritten or deleted: use `--output PATH` with a fresh directory for another
manual build. Nested Git directories must never be committed. The builder creates
only fixture commits; it does not commit changes to the docs-drift repository.
All fixture programs, commands, Makefile recipes, and reports are text only:
never import, install, or execute them.

Reproducibility fixes file bytes to UTF-8/LF, file modes to non-executable,
object format to SHA-1, initial branch to main, author and committer identities,
dates with explicit UTC offsets, messages, parents, and tree contents. Inherited
Git configuration and repository environment variables are isolated. Empty
templates and `commit-tree` avoid invoking hooks or signing code. No randomness,
wall-clock time, detector output, or absolute file path enters commit contents.

## Corrections

None. Future corrections must identify the incorrect scripted change, the reason,
date, author, affected key entries, and resulting history changes. A detector's
failure to match the fixed expectations is not grounds for a correction.
