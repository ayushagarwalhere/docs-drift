"""Generate the complete config reference from the Pydantic model and its JSON schema."""

import json
from pathlib import Path
from typing import Any

from docs_drift.config import Config


def render_reference() -> str:
    schema = Config.model_json_schema()
    defaults = Config().model_dump(mode="json")
    lines = [
        "# Configuration reference",
        "",
        "Generated from `docs_drift.config.Config`; do not edit field tables by hand.",
        "Run `python -m scripts.generate_reference` after changing the model.",
        "",
        "Precedence: explicit `--config`, repository `.docs-drift.toml`, then `[tool.docs-drift]` in `pyproject.toml`.",
        "Unknown keys warn. Configuration is TOML data; it is never executed.",
        "",
    ]

    def table(properties: dict[str, Any], values: dict[str, Any], prefix: str = "") -> None:
        lines.extend(["| Setting | Type / constraints | Default |", "| --- | --- | --- |"])
        nested = []
        for name, field in properties.items():
            ref = field.get("$ref")
            if ref:
                nested.append((name, schema["$defs"][ref.rsplit("/", 1)[-1]]))
                continue
            constraints = {k: v for k, v in field.items() if k not in {"title", "default", "description"}}
            kind = json.dumps(constraints, sort_keys=True).replace("|", "&#124;")
            default = json.dumps(values.get(name), ensure_ascii=True).replace("|", "&#124;")
            lines.append(f"| `{prefix + name}` | `{kind}` | `{default}` |")
        lines.append("")
        for name, model in nested:
            lines.extend(["## " + prefix + name, "", model.get("description", ""), ""])
            table(model["properties"], values[name], prefix + name + ".")

    table(schema["properties"], defaults)
    lines.extend(
        [
            "## Snapshot and runtime metadata",
            "",
            "Canonical JSON uses relative root paths, the source commit timestamp (epoch for unversioned input),",
            "sorted object keys and no nondeterministic timing values. Actual timings stay in verbose output and",
            "benchmark measurement sidecars. Rule-only scans require no API key.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    Path("docs/configuration.md").write_text(render_reference(), encoding="utf-8")
