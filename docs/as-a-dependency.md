# Use docs-drift as a dependency

Add docs-drift to a project's development dependencies and documentation drift
fails the same way a broken test does: in `pytest`, in a pre-commit hook, from
Python code, or in CI. Every integration below runs the same static scan as
`docs-drift check`. It reads your `.docs-drift.toml` (or `[tool.docs-drift]` in
`pyproject.toml`), applies your `min_confidence`, and never imports or executes
your code.

```text
pip install docs-drift          # or: uv add --dev docs-drift
```

## pytest plugin

Installing docs-drift registers a pytest plugin. It does nothing until you turn
it on:

```text
pytest --docs-drift
```

Each documentation file becomes one test item, named like
`docs/guide.md::docs-drift`. An item fails when that file has drift that fails
the check. The failure message is a small diff: the current documentation line,
the line with the suggested fix applied (when the fix is unambiguous), the source
evidence and the commit that introduced the drift.

```text
docs-drift: 1 finding in README.md

README.md:4: renamed cli_flag `--port` (confidence 0.92)
  - - `serve` accepts `--port` (default 8000).
  + - `serve` accepts `--listen` (default 8000).
    why: No indexed cli_flag named --port was found. ...
    evidence: README.md:4 --port
    introduced by: 450a93d Rename port to listen (2026-10-02)
    claim id: claim_8ed4...
```

Use `pytest --docs-drift-strict` to also fail on claims the static index could
not decide (unverifiable claims), whatever their confidence.

To enable it permanently, set it in your pytest configuration:

```toml
[tool.pytest.ini_options]
docs_drift = true
```

| pytest setting | Default | Meaning |
| --- | --- | --- |
| docs_drift | false | Same as the command-line switch. |
| docs_drift_strict | false | Same as the strict switch. |
| docs_drift_repo | the rootdir | Repository to check, relative to the rootdir. |
| docs_drift_baseline | the default baseline file when present | Baseline JSON, relative to the repository. |
| docs_drift_history | true | Inspect Git history for attribution and history-proven renames. |
| docs_drift_min_confidence | from your docs-drift configuration | Override the confidence threshold. |

**Baselines.** If the repository has the file written by
`docs-drift baseline update`, the plugin uses it automatically, so only new drift
fails. Point the baseline setting elsewhere to use a different file.

**Speed.** The plugin runs one scan per session, however many items there are.
The result is cached in pytest's cache directory, keyed by the docs-drift
version, your options and configuration, the Git HEAD, and the bytes of every
documentation and source file. An unchanged repository is not rescanned; any
edit invalidates the cache. `pytest --cache-clear` drops it. Set
`docs_drift_history = false` for quicker local runs (see the trade-off under
[pre-commit](#pre-commit)).

**Selection and xdist.** Items are ordinary tests: `-k "not docs-drift"` skips
them, and `-k README` selects one file. They are collected in a fixed order, and
cache writes are atomic, so the plugin is safe with pytest-xdist. Each worker
that runs an item reuses the cached result when it exists. With
`--dist loadgroup` all documentation items share one xdist group and run on one
worker.

### Inside your own tests

`assert_no_drift` fails the calling test with the same readable report:

```python
from docs_drift.testing import assert_no_drift


def test_docs_match_code():
    assert_no_drift(".")
```

It accepts the same keyword arguments as `docs_drift.check()` below and returns
the result when the check passes.

## Python API

`docs_drift.check()` is the stable library entry point. It mirrors
`docs-drift check` but returns a result instead of exiting:

```python
import docs_drift

result = docs_drift.check(".", history=True)
if not result.passed:
    print(result.report())
for finding in result.findings:
    print(finding.claim.doc_path, finding.claim.line, finding.verdict, finding.suggestion)
```

| Argument | Default | Meaning |
| --- | --- | --- |
| path | current directory | Repository root. |
| docs | None | Glob or list of globs replacing the configured documentation globs. |
| min_confidence | None | Threshold for failing drift; None uses the configuration. |
| history | True | Inspect Git history (attribution and history-proven renames). |
| baseline | None | Baseline JSON relative to the repository; its known drift does not fail. |
| config | None | Explicit TOML configuration file. |
| strict | False | Also fail on unverifiable claims. |

The return value is an immutable `CheckResult`:

- `passed`: whether the check passed.
- `findings`: drift that fails the check (new relative to the baseline and at or
  above the threshold), ordered by file and line. Each item is the same
  `Finding` model used in the JSON report.
- `unverifiable`: claims the static index could not decide.
- `failures`: `findings`, plus `unverifiable` in strict mode.
- `summary`: counts (`claims`, `drift`, `baselined`, `below_threshold`,
  `failing`, `unverifiable`), `docs_scanned`, `head_sha`, `by_verdict` and scan
  `warnings`.
- `report()` and `for_doc(path)`: the readable report and per-file failures.

Invalid options, configuration or baselines raise `ValueError`; a repository that
cannot be scanned raises `docs_drift.errors.DocsDriftError`. For the full
versioned scan, use [`run_scan`](python-api.md).

## pre-commit

The repository ships two pre-commit hooks:

| Hook id | Runs | Use it for |
| --- | --- | --- |
| docs-drift | `docs-drift check --quiet --auto-baseline` | The full check, the same as CI. |
| docs-drift-fast | `docs-drift check --quiet --auto-baseline --no-history` | Quicker local commits. |

Both scan the whole repository, because drift spans files, and run only when a
commit touches documentation, source or configuration files. They print nothing
unless the check fails.

`--auto-baseline` applies the generated `.docs-drift-baseline.json` when it exists, the
same rule the pytest plugin follows, so drift you accepted with
`docs-drift baseline update` fails neither. Without that file it does nothing.
The CI, tox and nox commands below pass it too.

!!! note
    These hooks first ship in **v0.1.1**. Until that release is tagged, the
    snippet below fails to resolve the revision.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/ayushagarwalhere/docs-drift
    rev: v0.1.1
    hooks:
      - id: docs-drift
```

Use `docs-drift-fast` as the hook id for faster commits. Skipping Git history has
a cost: a rename that only history can prove is reported as "missing" at a lower
confidence, which can fall below your threshold. Keep the full check in CI.

## CI

Any CI with Python and Git can run the check. Fetch full history so attribution
works:

```yaml
- uses: actions/checkout@v4
  with: {fetch-depth: 0}
- run: pipx run docs-drift check --auto-baseline
```

On GitHub, the [GitHub Action](github-action.md) also gates only on drift a pull
request introduces. If your CI already runs pytest, `pytest --docs-drift` is
enough.

## tox and nox

tox:

```ini
[testenv:docs-drift]
deps = docs-drift
commands = docs-drift check --auto-baseline
```

nox:

```python
import nox


@nox.session
def docs_drift(session: nox.Session) -> None:
    session.install("docs-drift")
    session.run("docs-drift", "check", "--auto-baseline")
```

Both also work with the plugin: install docs-drift alongside your test
dependencies and run `pytest --docs-drift`.

## Command line

`python -m docs_drift` is equivalent to the `docs-drift` command, which helps
where console scripts are not on the executable search path.
