"""Frozen instructions plus escaped, delimited source data; stable prefix first."""

import json
from importlib.resources import files

from docs_drift.llm.privacy import mask_secrets
from docs_drift.llm.types import ModelRequest, Snippet

PROMPT_VERSION = "verify_v1.md"


def data_block(name: str, value: object) -> str:
    escaped = json.dumps(value, ensure_ascii=True, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<data name="{name}">\n{escaped}\n</data>'


def build_request(model: str, claim: str, snippets: list[Snippet], *, rewrite: bool = False) -> ModelRequest:
    version = "rewrite_v1.md" if rewrite else PROMPT_VERSION
    system = files("docs_drift.llm").joinpath("prompts", version).read_text(encoding="utf-8")
    context = data_block("source-snippets", [snippet.model_dump() for snippet in snippets])
    return ModelRequest(
        model=model,
        prompt_version=version,
        system=system,
        context=context,
        claim=data_block("rewrite-sentence" if rewrite else "claim", mask_secrets(claim)),
        purpose="rewrite" if rewrite else "verify",
    )
