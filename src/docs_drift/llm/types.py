"""Small validated contracts shared by live, fake, and replay clients."""

import hashlib
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.models import Claim


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Candidate(StrictModel):
    claim: Claim
    source_files: list[str]


class Snippet(StrictModel):
    snippet_id: str
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    suspicious: bool = False


class Citation(StrictModel):
    snippet_id: str
    quote: str = Field(min_length=1, max_length=12000)


class ModelVerdict(StrictModel):
    verdict: Literal["supported", "contradicted", "not_found"]
    evidence: list[Citation] = Field(max_length=6)
    explanation: str = Field(min_length=1, max_length=4000)
    suggested_doc_text: str | None = Field(default=None, max_length=4000)


TriageDecision = Literal["keep", "not_project_claim", "uncertain"]
TriageCategory = Literal[
    "none",
    "doc_defined_example",
    "third_party",
    "placeholder",
    "user_environment",
    "historical",
    "os_or_runtime",
    "output_or_data",
    "not_an_identifier",
]
MAX_TRIAGE_BATCH = 12


class TriageItem(StrictModel):
    id: str = Field(min_length=1, max_length=16)
    decision: TriageDecision
    category: TriageCategory
    quote: str = Field(max_length=2000)
    rationale: str = Field(max_length=2000)


class TriageBatch(StrictModel):
    results: list[TriageItem] = Field(max_length=MAX_TRIAGE_BATCH)


class ModelRequest(StrictModel):
    model: str
    prompt_version: str
    system: str
    context: str
    claim: str
    purpose: Literal["verify", "rewrite", "triage"] = "verify"

    def request_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class CallUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)


class ModelResponse(StrictModel):
    result: ModelVerdict
    usage: CallUsage = Field(default_factory=CallUsage)
    replayed: bool = False


class TriageResponse(StrictModel):
    result: TriageBatch
    usage: CallUsage = Field(default_factory=CallUsage)
    replayed: bool = False


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class TriageClient(Protocol):
    async def complete_triage(self, request: ModelRequest) -> TriageResponse: ...
