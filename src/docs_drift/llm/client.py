"""OpenRouter structured JSON transport with deterministic record/replay.

No repository text enters system instructions. No tools are supplied. Replay
never instantiates a network client or reads an API key. Recordings contain
hashes and validated redacted responses, never credentials or raw requests.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from docs_drift.config import LLMConfig
from docs_drift.llm.pricing import estimate_request, max_output_tokens, usage_cost
from docs_drift.llm.privacy import mask_secrets
from docs_drift.llm.types import (
    MAX_TRIAGE_BATCH,
    CallUsage,
    ModelRequest,
    ModelResponse,
    ModelVerdict,
    StrictModel,
    TriageBatch,
    TriageItem,
    TriageResponse,
)

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 529}


class ClientError(RuntimeError):
    """A safe, actionable error that never embeds a provider response or key."""


class ProviderError(ClientError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"OpenRouter returned HTTP {status}. Check account access or retry later.")


class Transport(Protocol):
    async def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class OpenRouterTransport:
    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx  # noqa: PLC0415 - optional extra must not be imported by rule-only scans.
        except ImportError as exc:
            raise ClientError("Install the model extra with uv sync --extra llm.") from exc
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ClientError("Set OPENROUTER_API_KEY in your environment, or use DOCS_DRIFT_LLM_MODE=replay.")
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
                if response.status_code >= 400:  # noqa: PLR2004 - HTTP error range.
                    raise ProviderError(response.status_code)
                result: dict[str, Any] = response.json()
                return result
        except httpx.HTTPError as exc:
            raise ClientError("OpenRouter connection failed. Check connectivity and retry.") from exc


class Recording(StrictModel):
    request_hash: str
    model: str
    prompt_version: str
    response: ModelResponse
    provenance: Literal["live", "synthetic"] = "live"


class TriageRecording(StrictModel):
    request_hash: str
    model: str
    prompt_version: str
    response: TriageResponse
    provenance: Literal["live", "synthetic"] = "live"


ResponseT = TypeVar("ResponseT", ModelResponse, TriageResponse)


def _result_type(request: ModelRequest) -> type[BaseModel]:
    return TriageBatch if request.purpose == "triage" else ModelVerdict


def _strict_schema(model: type[BaseModel] = ModelVerdict) -> dict[str, Any]:
    schema = model.model_json_schema()
    for node in [schema, *schema.get("$defs", {}).values()]:
        if node.get("type") == "object":
            node["required"] = list(node.get("properties", {}))
            node["additionalProperties"] = False
    return schema


def payload_for(request: ModelRequest) -> dict[str, Any]:
    return {
        "model": request.model,
        "max_tokens": max_output_tokens(request),
        "temperature": 0,
        "provider": {"require_parameters": True, "data_collection": "deny"},
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": request.context, "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": request.claim},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "triage_decisions" if request.purpose == "triage" else "documentation_verdict",
                "strict": True,
                "schema": _strict_schema(_result_type(request)),
            },
        },
    }


def _triage_batch(content: str) -> TriageBatch:
    """Validate items one by one so one malformed item only keeps its own finding."""
    try:
        data = json.loads(content)
        items = data.get("results") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # A model stuck repeating itself hits the token cap mid-item. Salvage complete items;
        # the truncated one simply gets no answer, so its finding is kept.
        decoder = json.JSONDecoder()
        items = []
        for match in re.finditer(r'\{\s*"id"\s*:', content):
            try:
                items.append(decoder.raw_decode(content, match.start())[0])
            except json.JSONDecodeError:
                continue
    if not isinstance(items, list) or not items:
        raise ValueError("Expected a results list")
    valid: list[TriageItem] = []
    for item in items[:MAX_TRIAGE_BATCH]:
        try:
            valid.append(TriageItem.model_validate(item))
        except ValidationError:
            continue
    return TriageBatch(results=valid)


def _parse_response(data: dict[str, Any], model: str, response_type: type[ResponseT]) -> ResponseT:
    try:
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Expected JSON text")
        result: BaseModel = (
            _triage_batch(mask_secrets(content))
            if response_type is TriageResponse
            else ModelVerdict.model_validate_json(mask_secrets(content))
        )
        raw = data.get("usage", {})
        details = raw.get("prompt_tokens_details", {}) or {}
        usage = CallUsage(
            input_tokens=raw.get("prompt_tokens", 0),
            output_tokens=raw.get("completion_tokens", 0),
            cache_read_tokens=details.get("cached_tokens", 0) or 0,
            cache_write_tokens=details.get("cache_write_tokens", 0) or 0,
            cost=raw.get("cost", 0) or 0,
        )
        if not usage.cost:
            usage.cost = usage_cost(usage, model)
        return response_type.model_validate({"result": result, "usage": usage})
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ClientError("OpenRouter returned invalid structured output. The claim remains unverifiable.") from exc


class RecordedClient:
    def __init__(
        self,
        config: LLMConfig,
        *,
        mode: str | None = None,
        recordings: Path | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.mode = mode or os.environ.get("DOCS_DRIFT_LLM_MODE", "live")
        if self.mode not in {"live", "record", "replay"}:
            raise ClientError("Set DOCS_DRIFT_LLM_MODE to live, record, or replay.")
        self.recordings = recordings or Path(os.environ.get("DOCS_DRIFT_LLM_RECORDINGS", "tests/recordings"))
        self.transport = transport

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.purpose == "triage":
            raise ClientError("Use complete_triage for triage requests.")
        return await self._complete(request, ModelResponse)

    async def complete_triage(self, request: ModelRequest) -> TriageResponse:
        if request.purpose != "triage":
            raise ClientError("complete_triage only accepts triage requests.")
        return await self._complete(request, TriageResponse)

    async def _complete(self, request: ModelRequest, response_type: type[ResponseT]) -> ResponseT:
        request = request.model_copy(
            update={"context": mask_secrets(request.context), "claim": mask_secrets(request.claim)}
        )
        digest = request.request_hash()
        path = self.recordings / f"{digest}.json"
        recording_type: type[Recording] | type[TriageRecording] = (
            TriageRecording if response_type is TriageResponse else Recording
        )
        if self.mode == "replay":
            if not path.is_file():
                raise ClientError(f"Replay miss: {digest}. Run scripts/record.py in record mode with an API key.")
            try:
                recording = recording_type.model_validate_json(path.read_text(encoding="utf-8"))
                if recording.request_hash != digest or recording.model != request.model:
                    raise ValueError("Recording hash or model mismatch")
                replayed = response_type.model_validate(recording.response.model_dump())
                return replayed.model_copy(
                    update={"replayed": True, "usage": replayed.usage.model_copy(update={"cost": 0.0})}
                )
            except (OSError, ValueError) as exc:
                raise ClientError(f"Invalid replay recording {digest}. Re-record this request.") from exc
        transport = self.transport or OpenRouterTransport()
        try:
            async with asyncio.timeout(self.config.timeout):
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        response = _parse_response(
                            await transport.send(payload_for(request)), request.model, response_type
                        )
                        if not response.usage.cost and not (
                            response.usage.input_tokens or response.usage.output_tokens
                        ):
                            # Missing provider telemetry must not defeat budget enforcement.
                            response.usage.cost = estimate_request(request)
                        break
                    except ProviderError as exc:
                        if exc.status not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(0.5 * 2**attempt)
                else:
                    raise ClientError("OpenRouter retry limit reached.")
        except TimeoutError as exc:
            raise ClientError("Model request exceeded its total timeout. Reduce context or retry.") from exc
        if self.mode == "record":
            self.recordings.mkdir(parents=True, exist_ok=True)
            record = recording_type.model_validate(
                {
                    "request_hash": digest,
                    "model": request.model,
                    "prompt_version": request.prompt_version,
                    "response": response.model_dump(),
                }
            )
            temporary = path.with_suffix(".tmp")
            temporary.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        return response
