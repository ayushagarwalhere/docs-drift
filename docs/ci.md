# Pre-commit and other CI

`docs-drift init` offers a local pre-commit hook and workflow. Review generated
files before enabling them; existing files are preserved. Install the scanner
in the hook environment, then run `pre-commit install`.

```yaml
repos:
  - repo: local
    hooks:
      - id: docs-drift
        name: Check documentation drift
        entry: docs-drift check
        language: system
        pass_filenames: false
```

Use `check` in any CI that provides Python and Git. Full history improves
attribution; shallow/truncated history is reported. No write token is required.
Baselines are available for incremental adoption, but are not evidence that old
findings were correct. This project's own docs check must not use a baseline to
hide undocumented behavior.
