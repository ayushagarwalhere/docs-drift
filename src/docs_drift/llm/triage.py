"""Optional model triage that can only downgrade rule drift, never create or upgrade it.

A rule finding says a documented name is absent from the code. Triage asks a model,
from documentation context alone, whether the statement is about this project at
all (an example the doc defines itself, another tool's flag, a placeholder, a user
path, historical text, an OS variable). A finding is downgraded to unverifiable only
when the model answers ``not_project_claim`` with a quote that is a verbatim
substring of the excerpt it was shown. Anything else, including invalid output,
errors, budget exhaustion and instruction-like text, keeps the rule finding as is.
"""

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from docs_drift.llm.client import ClientError, RecordedClient
from docs_drift.llm.pricing import TRIAGE_DEFAULT_MODEL, estimate_request
from docs_drift.llm.privacy import excluded_path, mask_secrets, suspicious_text
from docs_drift.llm.prompts import data_block
from docs_drift.llm.types import CallUsage, ModelRequest, TriageClient, TriageItem, TriageResponse
from docs_drift.llm.verify import Budget
from docs_drift.models import ClaimKind, DecidedBy, Evidence, Finding, ScanResult, TriageNote, Verdict

PROMPT_VERSION = "triage_v4.md"
# Below every sensible --min-confidence gate, like other model-undecided findings.
TRIAGED_CONFIDENCE = 0.2
MAX_EXCERPT_LINES = 40
MAX_LINE_CHARS = 400
MAX_DEFINITIONS = 3
MIN_QUOTE_CHARS = 8
MAX_DOC_BYTES = 2 * 1024 * 1024
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+.:_])\1{2,}\s*$")
_RST_BLOCK = re.compile(r"^(\s*)\.\.\s+(?:code-block|code|sourcecode|literalinclude)::\s*([\w+#.-]*)")


@dataclass(frozen=True)
class Block:
    """One contiguous piece of documentation text shown to the model."""

    label: str
    start_line: int
    text: str


@dataclass(frozen=True)
class DocView:
    path: str
    lines: list[str]
    fences: list[tuple[int, int, str]]
    headings: list[tuple[int, str]]

    def fence_at(self, line: int) -> tuple[int, int, str] | None:
        return next((fence for fence in self.fences if fence[0] <= line <= fence[1]), None)

    def heading_before(self, line: int) -> tuple[int, str] | None:
        found = None
        for number, text in self.headings:
            if number > line:
                break
            found = (number, text)
        return found

    def text(self, start: int, end: int) -> str:
        return "\n".join(item[:MAX_LINE_CHARS] for item in self.lines[start - 1 : end])


@dataclass
class Target:
    """A rule finding prepared for one triage item."""

    ref: str
    finding: Finding
    blocks: list[Block]
    payload: dict[str, object]


@dataclass
class TriageOutcome:
    findings: list[Finding]
    usage: dict[str, float]
    warnings: list[str] = field(default_factory=list)


def parse_doc(path: str, text: str) -> DocView:
    """Find fenced/directive code blocks and headings with a linear line scan."""
    lines = mask_secrets(text).splitlines()
    fences: list[tuple[int, int, str]] = []
    headings: list[tuple[int, str]] = []
    open_fence: tuple[int, str, str] | None = None
    rst_block: tuple[int, int, str] | None = None
    for number, line in enumerate(lines, start=1):
        if open_fence is not None:
            if line.strip().startswith(open_fence[1]) and not line.strip().strip(open_fence[1][0]):
                fences.append((open_fence[0], number, open_fence[2]))
                open_fence = None
            continue
        if rst_block is not None:
            start, indent, info = rst_block
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                fences.append((start, number - 1, info))
                rst_block = None
            else:
                continue
        fence = _FENCE.match(line)
        if fence:
            open_fence = (number, fence.group(1), fence.group(2))
            continue
        directive = _RST_BLOCK.match(line)
        if directive:
            rst_block = (number, len(directive.group(1)), directive.group(2))
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            headings.append((number, heading.group(2)))
        elif (
            number > 1
            and _RST_UNDERLINE.match(line)
            and lines[number - 2].strip()
            and len(line.strip()) >= len(lines[number - 2].strip())
        ):
            headings.append((number - 1, lines[number - 2].strip()))
    if open_fence is not None:
        fences.append((open_fence[0], len(lines), open_fence[2]))
    if rst_block is not None:
        fences.append((rst_block[0], len(lines), rst_block[2]))
    return DocView(path, lines, fences, headings)


def _token(finding: Finding) -> str:
    value = finding.claim.value.strip()
    if finding.claim.kind == ClaimKind.cli_flag:
        return value.split("=", 1)[0]
    if finding.claim.kind in {ClaimKind.symbol, ClaimKind.config_key}:
        return re.split(r"[.:]", value.rstrip("()"))[-1] or value
    return value.split()[-1] if finding.claim.kind == ClaimKind.cli_command else value


def _definition_patterns(finding: Finding, token: str) -> list[re.Pattern[str]]:
    name = re.escape(token)
    kind = finding.claim.kind
    if kind == ClaimKind.cli_flag:
        bare = re.escape(token.lstrip("-"))
        return [
            re.compile(rf"""(["'])-{{1,2}}{bare}\1"""),
            re.compile(rf"\bdef\s+\w+\([^)]*\b{re.escape(token.lstrip('-').replace('-', '_'))}\b"),
        ]
    if kind == ClaimKind.env_var:
        return [
            re.compile(rf"""(?:environ(?:\.get)?\s*[\[(]|getenv\s*\()\s*["']{name}["']"""),
            re.compile(rf"""envvar\s*=\s*\[?\s*["']{name}["']"""),
        ]
    if kind == ClaimKind.cli_command:
        return [
            re.compile(rf"""(?:command|group|add_parser|add_typer)\s*\(\s*(?:name\s*=\s*)?["']{name}["']"""),
            re.compile(rf"\bdef\s+{re.escape(token.replace('-', '_'))}\s*\("),
        ]
    if kind in {ClaimKind.symbol, ClaimKind.endpoint}:
        return [
            re.compile(rf"\b(?:def|class|function|const|let|var)\s+{name}\b"),
            re.compile(rf"^\s*{name}\s*[:=]", re.M),
            re.compile(rf"""(?:route|get|post|put|delete|patch)\s*\(\s*["']{name}["']"""),
        ]
    if kind == ClaimKind.config_key:
        return [re.compile(rf"^\s*{name}\s*[:=]", re.M), re.compile(rf"""["']{name}["']\s*:""")]
    return []


def definitions_in_doc(view: DocView, finding: Finding) -> list[Block]:
    """Lines inside the document's own code blocks that appear to define the token."""
    token = _token(finding)
    patterns = _definition_patterns(finding, token) if len(token.strip("-")) >= 2 else []  # noqa: PLR2004
    found: list[Block] = []
    for start, end, _info in view.fences:
        for number in range(start + 1, end + 1):
            line = view.lines[number - 1]
            if token in line and any(pattern.search(line) for pattern in patterns):
                found.append(Block(f"defined_in_doc_code line {number}", number, line[:MAX_LINE_CHARS]))
                if len(found) >= MAX_DEFINITIONS:
                    return found
    return found


def _excerpt_range(view: DocView, line: int, radius: int) -> tuple[int, int]:
    start, end = max(1, line - radius), min(len(view.lines), line + radius)
    fence = view.fence_at(line)
    if fence is not None:
        # Show the fence opening (its language) and as much of the block as fits.
        start = max(min(start, fence[0]), line - MAX_EXCERPT_LINES // 2)
        end = min(max(end, fence[1]), line + MAX_EXCERPT_LINES // 2)
    return start, max(start, min(end, start + MAX_EXCERPT_LINES - 1))


def build_target(ref: str, finding: Finding, view: DocView, radius: int) -> Target | None:
    """Prepare one finding; None means it is kept without asking the model."""
    line = finding.claim.line
    if line > len(view.lines):
        return None
    start, end = _excerpt_range(view, line, radius)
    blocks = [Block("excerpt", start, view.text(start, end))]
    heading = view.heading_before(line)
    if heading is not None and heading[0] < start:
        blocks.append(Block("heading", heading[0], view.lines[heading[0] - 1][:MAX_LINE_CHARS]))
    definitions = definitions_in_doc(view, finding)
    blocks.extend(block for block in definitions if not start <= block.start_line <= end)
    if any(suspicious_text(block.text) for block in blocks):
        return None
    fence = view.fence_at(line)
    payload: dict[str, object] = {
        "id": ref,
        "kind": finding.claim.kind.value,
        "name": finding.claim.value,
        "rule_verdict": finding.verdict.value,
        "doc": finding.claim.doc_path,
        "line": line,
        "in_code_block": (fence[2] or "unlabelled") if fence else None,
        "heading": heading[1] if heading else None,
        "excerpt_start_line": start,
        "excerpt": blocks[0].text,
        "defined_in_doc_code": [{"line": item.start_line, "text": item.text} for item in definitions],
        "rule_reason": mask_secrets(finding.reasons[0])[:MAX_LINE_CHARS],
        "closest_in_code": finding.matched_symbol.name if finding.matched_symbol else None,
    }
    if heading is not None and heading[0] < start:
        payload["heading_line"] = heading[0]
    return Target(ref, finding, blocks, payload)


def validate_quote(quote: str, target: Target) -> tuple[int, int, str] | None:
    """Return the quote's (start, end) lines when it is a verbatim substring of a shown block.

    Only runs of whitespace may differ (models re-wrap lines); every other character must
    match. The bare name alone is not evidence of anything.
    """
    text = quote.strip()
    if len(text) < MIN_QUOTE_CHARS or text.strip("`'\" ") == target.finding.claim.value.strip():
        return None
    if "[REDACTED]" in text or suspicious_text(text):
        return None
    pattern = re.compile(r"\s+".join(re.escape(part) for part in text.split()))
    for block in target.blocks:
        match = pattern.search(block.text)
        if match:
            start = block.start_line + block.text[: match.start()].count("\n")
            return start, start + match.group().count("\n"), match.group()
    return None


def apply_item(target: Target, item: TriageItem | None, *, model: str, prompt_version: str, replayed: bool) -> Finding:
    """Downgrade only for a valid not_project_claim answer; otherwise return the finding unchanged."""
    finding = target.finding
    if (
        item is None
        or item.decision != "not_project_claim"
        or item.category == "none"
        or not item.rationale.strip()
        or not finding.is_drift
        or finding.decided_by != DecidedBy.rules
        or finding.triage is not None
    ):
        return finding
    matched = validate_quote(item.quote, target)
    if matched is None:
        return finding
    first, last, quote = matched
    rationale = mask_secrets(item.rationale.strip())[:MAX_LINE_CHARS]
    note = TriageNote(
        category=item.category,
        quote=quote,
        quote_file=finding.claim.doc_path,
        quote_line=first,
        rationale=rationale,
        original_verdict=finding.verdict,
        original_confidence=finding.confidence,
        model=model,
        prompt_version=prompt_version,
        replayed=replayed,
    )
    reason = (
        f"Model triage: likely not a claim about this project ({item.category}): {rationale} "
        f"Rule verdict was {finding.verdict.value}."
    )
    return finding.model_copy(
        update={
            "verdict": Verdict.unverifiable,
            "confidence": min(finding.confidence, TRIAGED_CONFIDENCE),
            "reasons": [reason, *finding.reasons],
            "evidence": [
                *finding.evidence,
                Evidence(
                    file=finding.claim.doc_path,
                    line=first,
                    end_line=last if last != first else None,
                    text=quote,
                ),
            ],
            "suggestion": None,
            "decided_by": DecidedBy.llm,
            "replayed": replayed,
            "triage": note,
        }
    )


def apply_response(
    targets: Sequence[Target], response: TriageResponse, *, model: str, prompt_version: str
) -> list[Finding]:
    """Match answers by id; missing, duplicate and unknown ids keep their findings."""
    answers: dict[str, list[TriageItem]] = {}
    for item in response.result.results:
        answers.setdefault(item.id, []).append(item)
    return [
        apply_item(
            target,
            answers[target.ref][0] if len(answers.get(target.ref, [])) == 1 else None,
            model=model,
            prompt_version=prompt_version,
            replayed=response.replayed,
        )
        for target in targets
    ]


def enforce_downgrade_only(before: Sequence[Finding], after: Sequence[Finding]) -> None:
    """Fail closed if triage ever adds, removes, reorders or strengthens a finding."""
    if len(before) != len(after):
        raise RuntimeError("Triage changed the number of findings.")
    for old, new in zip(before, after, strict=True):
        if old.claim.claim_id != new.claim.claim_id:
            raise RuntimeError("Triage reordered or replaced a finding.")
        if new == old:
            continue
        if not (
            old.is_drift
            and old.decided_by == DecidedBy.rules
            and new.verdict == Verdict.unverifiable
            and new.confidence <= old.confidence
            and new.triage is not None
            and new.triage.original_verdict == old.verdict
        ):
            raise RuntimeError("Triage may only downgrade rule drift to unverifiable.")


def project_identity(result: ScanResult) -> dict[str, object]:
    """Tell the model which project is being checked, so its own commands are never "third party"."""
    name = result.repo_path.resolve().name
    if result.remote_url:
        tail = result.remote_url.rstrip("/").removesuffix(".git")
        name = "/".join(tail.replace(":", "/").split("/")[-2:])
    return {
        "name": name,
        "packages": sorted({package.name for package in result.packages}),
        "commands": sorted({command for package in result.packages for command in package.commands}),
    }


def build_triage_request(
    model: str,
    targets: Sequence[Target],
    project: dict[str, object],
    *,
    prompt_version: str = PROMPT_VERSION,
    system: str | None = None,
) -> ModelRequest:
    text = (
        system
        if system is not None
        else files("docs_drift.llm").joinpath("prompts", prompt_version).read_text(encoding="utf-8")
    )
    return ModelRequest(
        model=model,
        prompt_version=prompt_version,
        system=text,
        context="\n".join(
            [data_block("project", project), data_block("findings", [target.payload for target in targets])]
        ),
        claim=data_block("task", {"finding_ids": [target.ref for target in targets]}),
        purpose="triage",
    )


def _load_view(root: Path, path: str, cache: dict[str, DocView | None], redact: list[str]) -> DocView | None:
    if path not in cache:
        cache[path] = None
        candidate = (root / path).resolve()
        if not excluded_path(path, redact) and candidate.is_relative_to(root.resolve()):
            try:
                if candidate.stat().st_size <= MAX_DOC_BYTES:
                    cache[path] = parse_doc(path, candidate.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                pass
    return cache[path]


def select_targets(result: ScanResult) -> list[Finding]:
    """Rule drift only; the highest-confidence findings (those that gate CI) go first under the cap."""
    config = result.config_used.llm
    eligible = [
        item for item in result.findings if item.is_drift and item.decided_by == DecidedBy.rules and item.triage is None
    ]
    eligible.sort(key=lambda item: (-item.confidence, item.claim.doc_path, item.claim.line, item.claim.claim_id))
    chosen = eligible[: config.triage_max_findings]
    return sorted(chosen, key=lambda item: (item.claim.doc_path, item.claim.line, item.claim.claim_id))


async def triage_findings(  # noqa: PLR0915 - one bounded orchestration loop.
    result: ScanResult,
    client: TriageClient | None = None,
    *,
    budget: Budget | None = None,
    prompt_version: str = PROMPT_VERSION,
    system: str | None = None,
) -> TriageOutcome:
    """Ask the model about rule drift in batches and apply only valid downgrades."""
    config = result.config_used.llm
    model = config.triage_model or TRIAGE_DEFAULT_MODEL
    selected_client: TriageClient = client or RecordedClient(config)
    budget = budget or Budget(config.budget)
    started = time.perf_counter()
    root = result.docs_repo_path or result.repo_path
    views: dict[str, DocView | None] = {}
    targets: list[Target] = []
    for finding in select_targets(result):
        view = _load_view(root, finding.claim.doc_path, views, config.redact_paths)
        target = build_target(f"f{len(targets) + 1}", finding, view, config.triage_context_lines) if view else None
        if target is not None:
            targets.append(target)
    batches = [targets[i : i + config.triage_batch_size] for i in range(0, len(targets), config.triage_batch_size)]
    project = project_identity(result)
    semaphore = asyncio.Semaphore(config.concurrency)
    warnings: list[str] = []

    async def run(batch: list[Target]) -> tuple[list[Finding], CallUsage, str | None]:
        # Refs restart per batch so a request's content does not depend on its position in the run.
        local = [
            Target(f"f{index + 1}", item.finding, item.blocks, {**item.payload, "id": f"f{index + 1}"})
            for index, item in enumerate(batch)
        ]
        request = build_triage_request(model, local, project, prompt_version=prompt_version, system=system)
        kept = [item.finding for item in batch]
        async with semaphore:
            estimate = estimate_request(request)
            if not await budget.reserve(estimate):
                return kept, CallUsage(), "budget"
            try:
                response = await selected_client.complete_triage(request)
            except ClientError as exc:
                await budget.settle(estimate, estimate)
                if "Replay miss:" in str(exc) or "OPENROUTER_API_KEY" in str(exc):
                    raise
                return kept, CallUsage(cost=estimate), str(exc)
            await budget.settle(estimate, response.usage.cost)
            return apply_response(local, response, model=model, prompt_version=prompt_version), response.usage, None

    tasks = [asyncio.create_task(run(batch)) for batch in batches]
    try:
        done, pending = await asyncio.wait(tasks, timeout=config.timeout) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        outcomes = [
            task.result() if task in done else ([item.finding for item in batch], CallUsage(), "timeout")
            for batch, task in zip(batches, tasks, strict=True)
        ]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Grouped claims can share a claim_id across lines, so map by object identity.
    replaced = {
        id(old.finding): new
        for batch, (updated, _, _) in zip(batches, outcomes, strict=True)
        for old, new in zip(batch, updated, strict=True)
    }
    findings = [replaced.get(id(item), item) for item in result.findings]
    enforce_downgrade_only(result.findings, findings)
    problems = [problem for _, _, problem in outcomes if problem]
    skipped = sum(len(batch) for batch, (_, _, problem) in zip(batches, outcomes, strict=True) if problem)
    if "budget" in problems:
        warnings.append(f"LLM triage budget reached; {skipped} findings were kept without triage.")
    elif problems:
        warnings.append(f"LLM triage could not assess {skipped} findings ({problems[0]}); they were kept.")
    calls = [usage for _, usage, problem in outcomes if not problem]
    usage = {
        "triage_findings": float(len(targets)),
        "triage_batches": float(len(calls)),
        "triage_downgraded": float(
            sum(item.triage is not None for item in findings) - sum(item.triage is not None for item in result.findings)
        ),
        "triage_input_tokens": float(sum(call.input_tokens for call in calls)),
        "triage_output_tokens": float(sum(call.output_tokens for call in calls)),
        "triage_cost": sum(usage.cost for _, usage, _ in outcomes),
        "triage_seconds": time.perf_counter() - started,
    }
    return TriageOutcome(findings, usage, warnings)


def format_triage_usage(usage: dict[str, float]) -> str:
    return (
        f"LLM triage: {int(usage.get('triage_downgraded', 0))} of {int(usage.get('triage_findings', 0))} "
        f"rule findings downgraded as not project claims, {int(usage.get('triage_batches', 0))} requests, "
        f"${usage.get('triage_cost', 0):.4f}, {usage.get('triage_seconds', 0):.1f} s"
    )


async def triage_result(result: ScanResult, client: TriageClient | None = None, *, spent: float = 0) -> ScanResult:
    """Apply triage to a scan result, sharing one budget with earlier model calls."""
    config = result.config_used.llm
    budget = Budget(max(0.0, config.budget - spent))
    outcome = await triage_findings(result, client, budget=budget)
    return result.model_copy(
        update={
            "findings": outcome.findings,
            "warnings": [*result.warnings, *outcome.warnings],
            "llm_usage": {**result.llm_usage, **outcome.usage},
        }
    )
