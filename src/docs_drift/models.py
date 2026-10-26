"""Shared pipeline data contracts. Extraction, matching, and history logic live elsewhere."""

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from docs_drift import __version__
from docs_drift.config import Config
from docs_drift.ids import normalize_repo_path

RepoPath = Annotated[str, BeforeValidator(normalize_repo_path)]
NonemptyText = Annotated[str, Field(min_length=1)]
Line = Annotated[int, Field(ge=1, strict=True)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Seconds = Annotated[float, Field(ge=0)]
GitSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
SectionID = Annotated[str, Field(pattern=r"^section_[0-9a-f]{64}$")]
ClaimID = Annotated[str, Field(pattern=r"^claim_[0-9a-f]{64}$")]
SymbolID = Annotated[str, Field(pattern=r"^symbol_[0-9a-f]{64}$")]


class DocFormat(StrEnum):
    markdown = "markdown"
    mdx = "mdx"
    rst = "rst"
    text = "text"


class SectionContext(StrEnum):
    current = "current"
    historical = "historical"


class ClaimKind(StrEnum):
    cli_flag = "cli_flag"
    cli_command = "cli_command"
    env_var = "env_var"
    endpoint = "endpoint"
    config_key = "config_key"
    symbol = "symbol"
    file_path = "file_path"
    package_script = "package_script"
    make_target = "make_target"
    default_value = "default_value"
    code_command = "code_command"
    prose = "prose"
    dependency = "dependency"
    runtime_version = "runtime_version"


class Extraction(StrEnum):
    inline_code = "inline_code"
    code_block = "code_block"
    table = "table"
    prose_pattern = "prose_pattern"
    link = "link"


class SymbolKind(StrEnum):
    cli_flag = "cli_flag"
    cli_command = "cli_command"
    env_var = "env_var"
    route = "route"
    config_key = "config_key"
    function = "function"
    class_ = "class"
    method = "method"
    file = "file"
    package_script = "package_script"
    make_target = "make_target"
    ci_job = "ci_job"
    cli_argument = "cli_argument"
    attribute = "attribute"
    variable = "variable"
    parameter = "parameter"
    module = "module"


class Verdict(StrEnum):
    ok = "ok"
    missing = "missing"
    renamed = "renamed"
    default_changed = "default_changed"
    invalid_command = "invalid_command"
    unverifiable = "unverifiable"
    version_mismatch = "version_mismatch"
    unknown_extra = "unknown_extra"


class DecidedBy(StrEnum):
    rules = "rules"
    llm = "llm"


class PipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DocFile(PipelineModel):
    path: RepoPath
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    format: DocFormat


class Section(PipelineModel):
    section_id: SectionID
    doc_path: RepoPath
    heading: str
    heading_level: Annotated[int, Field(ge=0, strict=True)]
    heading_path: list[str]
    start_line: Line
    end_line: Line
    context: SectionContext = SectionContext.current
    historical_spans: list[tuple[Line, Line]] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("Section end_line must be at least start_line.")
        return self


class Claim(PipelineModel):
    claim_id: ClaimID
    kind: ClaimKind
    raw_text: NonemptyText
    value: NonemptyText
    qualifier: str | None = None
    expected_default: str | None = None
    doc_path: RepoPath
    line: Line
    column: Line
    section_id: SectionID
    extraction: Extraction
    extraction_confidence: Confidence
    locations: list["ClaimLocation"] = Field(default_factory=list)
    extraction_sources: list[Extraction] = Field(default_factory=list)
    group_key: str | None = None


class ClaimLocation(PipelineModel):
    line: Line
    column: Line
    raw_text: NonemptyText
    extraction: Extraction


class SuppressedClaim(PipelineModel):
    claim: Claim
    reason: NonemptyText


class Symbol(PipelineModel):
    symbol_id: SymbolID
    kind: SymbolKind
    name: NonemptyText
    qualified_name: NonemptyText
    aliases: list[str] = Field(default_factory=list)
    default: str | None = None
    file: RepoPath
    line: Line
    language: NonemptyText
    framework: str | None = None
    parent_id: SymbolID | None = None
    exported: bool = False
    decorators: list[str] = Field(default_factory=list)
    derived: bool = False
    setting_group: str | None = None
    authoritative: bool = True
    raw_path: str | None = None
    handler: str | None = None
    partial_prefix: bool = False
    ts_type: bool = False
    # Positional arity as declared (``...`` marks a pass-through argument: argparse REMAINDER or
    # click ``nargs=-1`` with UNPROCESSED values); later tokens belong to another program.
    nargs: str | None = None
    # A configuration key whose children are free-form (JSON-schema additionalProperties).
    open_map: bool = False


class CommitRef(PipelineModel):
    sha: GitSHA
    short_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{4,64}$")]
    author_name: NonemptyText
    author_email: str | None = None
    date: AwareDatetime
    # Committer date: when the commit landed on the branch. Rebases and cherry-picks keep the
    # author date but set this anew, so drift age is measured from here. Older reports omit it.
    committed: AwareDatetime | None = None
    summary: NonemptyText

    @property
    def landed(self) -> datetime:
        """When the change reached the branch: the committer date, or the author date in older reports."""
        return self.committed or self.date

    @model_validator(mode="after")
    def matching_short_sha(self) -> Self:
        if not self.sha.startswith(self.short_sha):
            raise ValueError("short_sha must be a prefix of sha.")
        return self


class Evidence(PipelineModel):
    file: RepoPath
    line: Line
    text: str
    end_line: Line | None = None


class TriageNote(PipelineModel):
    """Why optional model triage downgraded a rule finding; the original verdict stays visible."""

    decision: Literal["not_project_claim"] = "not_project_claim"
    category: NonemptyText
    quote: NonemptyText
    quote_file: RepoPath
    quote_line: Line
    rationale: NonemptyText
    original_verdict: Verdict
    original_confidence: Confidence
    model: NonemptyText
    prompt_version: NonemptyText
    replayed: bool = False


class Finding(PipelineModel):
    claim: Claim
    matched_symbol: Symbol | None = None
    verdict: Verdict
    suggestion: str | None = None
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    introduced_by: CommitRef | None = None
    confidence: Confidence
    reasons: Annotated[list[NonemptyText], Field(min_length=1)]
    decided_by: DecidedBy = DecidedBy.rules
    replayed: bool = False
    package: str | None = None
    doc_text: str | None = None
    # Drift age, measured against the HEAD commit date so reports are reproducible.
    stale_since: date | None = None
    age_days: Annotated[int, Field(ge=0)] | None = None
    fix_by: date | None = None
    triage: TriageNote | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_triage(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Rule-only output stays byte-identical; triage appears only when it changed the finding.
        data: dict[str, Any] = handler(self)
        if self.triage is None:
            data.pop("triage", None)
        return data

    @property
    def is_drift(self) -> bool:
        return self.verdict in {
            Verdict.missing,
            Verdict.renamed,
            Verdict.default_changed,
            Verdict.invalid_command,
            Verdict.version_mismatch,
            Verdict.unknown_extra,
        }


class DependencyIssueKind(StrEnum):
    """Code-versus-manifest dependency observations; these are not documentation claims."""

    undeclared = "undeclared"
    dev_only = "dev_only"
    undeclared_dev = "undeclared_dev"
    unused = "unused"
    added_undocumented = "added_undocumented"
    removed_documented = "removed_documented"


class DependencyIssue(PipelineModel):
    issue_id: Annotated[str, Field(pattern=r"^dep_[0-9a-f]{64}$")]
    kind: DependencyIssueKind
    ecosystem: Literal["python", "node"]
    name: NonemptyText
    import_name: str | None = None
    manifest: RepoPath | None = None
    message: NonemptyText
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    confidence: Confidence


class Package(PipelineModel):
    name: NonemptyText
    root: str
    language: str
    commands: list[str] = Field(default_factory=list)


class ScanResult(PipelineModel):
    schema_version: Literal[1] = 1
    package_version: str = Field(default_factory=lambda: __version__)
    repo_path: Path
    head_sha: GitSHA
    scan_time: AwareDatetime
    config_used: Config
    docs: list[DocFile] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    symbols: list[Symbol] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    timings: dict[str, Seconds] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    suppressed: list[SuppressedClaim] = Field(default_factory=list)
    historical_ignored: int = Field(default=0, ge=0)
    llm_usage: dict[str, float] = Field(default_factory=dict)
    packages: list[Package] = Field(default_factory=list)
    docs_repo_path: Path | None = None
    remote_url: str | None = None
    baselined: int = Field(default=0, ge=0)
    dependency_issues: list[DependencyIssue] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_empty_dependency_issues(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Emit `dependency_issues` only when there are some, so scans without them keep the earlier v1 shape."""
        data: dict[str, Any] = handler(self)
        if not data.get("dependency_issues"):
            data.pop("dependency_issues", None)
        return data

    def summary(self) -> dict[str, dict[str, int]]:
        """Count findings by verdict and claim kind, including zero-count categories."""
        verdicts = dict.fromkeys((verdict.value for verdict in Verdict), 0)
        kinds = dict.fromkeys((kind.value for kind in ClaimKind), 0)
        deciders = dict.fromkeys((decider.value for decider in DecidedBy), 0)
        for finding in self.findings:
            verdicts[finding.verdict.value] += 1
            kinds[finding.claim.kind.value] += 1
            deciders[finding.decided_by.value] += 1
        return {"by_verdict": verdicts, "by_kind": kinds, "by_decider": deciders}
