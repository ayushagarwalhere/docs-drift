# Configuration reference

Generated from `docs_drift.config.Config`; do not edit field tables by hand.
Run `python -m scripts.generate_reference` after changing the model.

Precedence: explicit `--config`, repository `.docs-drift.toml`, then `[tool.docs-drift]` in `pyproject.toml`.
Unknown keys warn. Configuration is TOML data; it is never executed.

| Setting | Type / constraints | Default |
| --- | --- | --- |
| `docs` | `{"items": {"type": "string"}, "type": "array"}` | `["README.md", "docs/**/*.md", "*.md"]` |
| `exclude` | `{"items": {"type": "string"}, "type": "array"}` | `["node_modules/**", ".venv/**", "vendor/**", "CHANGELOG.md", "**/CHANGELOG.md"]` |
| `languages` | `{"anyOf": [{"const": "auto", "type": "string"}, {"items": {"type": "string"}, "type": "array"}]}` | `"auto"` |
| `ignore` | `{"items": {"type": "string"}, "type": "array"}` | `[]` |
| `min_confidence` | `{"maximum": 1.0, "minimum": 0.0, "type": "number"}` | `0.6` |

## llm

Optional prose assessment settings.

| Setting | Type / constraints | Default |
| --- | --- | --- |
| `llm.enabled` | `{"type": "boolean"}` | `false` |
| `llm.model` | `{"anyOf": [{"type": "string"}, {"type": "null"}]}` | `null` |
| `llm.max_claims` | `{"maximum": 1000, "minimum": 0, "type": "integer"}` | `40` |
| `llm.concurrency` | `{"maximum": 16, "minimum": 1, "type": "integer"}` | `4` |
| `llm.timeout` | `{"exclusiveMinimum": 0, "maximum": 600, "type": "number"}` | `60.0` |
| `llm.budget` | `{"minimum": 0, "type": "number"}` | `0.5` |
| `llm.redact_paths` | `{"items": {"type": "string"}, "type": "array"}` | `[]` |

## history

Limits for static git history inspection.

| Setting | Type / constraints | Default |
| --- | --- | --- |
| `history.max_commits` | `{"minimum": 0, "type": "integer"}` | `2000` |
| `history.show_emails` | `{"type": "boolean"}` | `false` |

## Snapshot and runtime metadata

Canonical JSON uses relative root paths, the source commit timestamp (epoch for unversioned input),
sorted object keys and no nondeterministic timing values. Actual timings stay in verbose output and
benchmark measurement sidecars. Rule-only scans require no API key.
