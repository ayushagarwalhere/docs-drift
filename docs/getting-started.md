# Getting started

This is an unreleased development checkout. PyPI publication and a public demo
URL are pending; do not assume a package or hosted link has already been published.

```sh
uv sync --locked --all-extras
uv run docs-drift scan --repo . --no-color
```

`scan` exits 0 after producing a report, even when drift is found. `check` exits
1 for findings at the configured confidence threshold and 2 for usage/configuration
errors. It never imports target modules, installs target dependencies, or runs
target documentation commands. Install Git for attribution.

After publication, use `pipx install docs-drift` or `uvx docs-drift scan`.
The local web demo uses `uv run python -m docs_drift.web`; see [deployment](deploy.md).
