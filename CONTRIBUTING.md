# Contributing

Use Python 3.11+ and uv. From the repository root:

```console
uv sync --locked --all-extras
uv run just setup
uv run just check
```

We use a justfile rather than duplicating recipes in a Makefile. `just` is a dev
dependency (`rust-just` on PyPI), and recipes use native PowerShell on Windows.
`setup` installs the pre-commit hook. Other recipes are `test`, `lint`, `fmt`,
`typecheck`, and `demo`. `check` verifies formatting without rewriting files;
run `fmt` explicitly to fix it. `demo` displays CLI help. Use the fixture commands
below to inspect a scan.

Without just, run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest --cov=docs_drift --cov=scripts --cov-report=term-missing`, and
`uv run pre-commit run --all-files`. CI tests Ubuntu and Windows on Python 3.11
and 3.13, with full Git history and all extras installed. Coverage must remain
at least 85% overall and 90% for extraction and matching; report a failure instead of lowering the thresholds.

The guard reads staged Git blobs, so cleaning a working file does not hide a
staged secret. It blocks files over 1,000,000 bytes, `.env` and `.env.*` files
(including examples), and token-shaped Anthropic, GitHub, and AWS keys. Use
safe placeholders in docs and construct fake test tokens from separate parts.
Guard diagnostics identify the file and line without printing a token.

For an indexer, add a typed, pure parser under `src/docs_drift/index/`, retain
source file/line/text evidence, and test it against text-only fixtures, including
Windows paths and unsupported dynamic behavior. Never import or run scanned code.
For a claim kind, update the models, extraction, matching, design taxonomy, and
report examples together; cover confirmed, missing, and unverifiable cases.
Do not invent a match when the available index is incomplete.

Record every metric, including failures, with the exact command and revision
that produced it. Keep fixture answer keys fixed; never tune rules or thresholds
to improve a score. The optional LLM cannot override a code-based verdict.

Fixture tests build fresh Git repositories in temporary directories. For manual
inspection use `uv run python tests/fixtures/build_fixtures.py`; the default output
under the generated `tests/fixtures/repos/` directory is ignored. Never run the generated programs or
commit their `.git` folders. `uv run python scripts/measure.py` validates the
hand-written key; read `tests/fixtures/ANSWER_KEY.md` before changing any fixture.

Use `pathlib` for filesystem paths. `.gitattributes` normalizes text to LF on
every platform. Review the diff and propose a commit message; do not auto-commit.
