"""Evidence validation, budget accounting, and the rules-win merge boundary."""

import asyncio
import re
import time
from dataclasses import dataclass, field

from docs_drift.llm.candidates import select_candidates
from docs_drift.llm.client import ClientError, RecordedClient
from docs_drift.llm.pricing import DEFAULT_MODEL, estimate_request
from docs_drift.llm.privacy import mask_secrets, suspicious_text
from docs_drift.llm.prompts import build_request
from docs_drift.llm.retrieve import retrieve
from docs_drift.llm.types import CallUsage, Candidate, ModelClient, ModelResponse, Snippet
from docs_drift.models import DecidedBy, Evidence, Finding, ScanResult, Verdict


def undecided(candidate: Candidate, reason: str) -> Finding:
    return Finding(
        claim=candidate.claim,
        verdict=Verdict.unverifiable,
        evidence=[Evidence(file=candidate.claim.doc_path, line=candidate.claim.line, text=candidate.claim.raw_text)],
        confidence=0.2,
        reasons=[reason],
        decided_by=DecidedBy.llm,
    )


def validate_evidence(response: ModelResponse, snippets: list[Snippet]) -> list[Evidence]:
    allowed = {snippet.snippet_id: snippet for snippet in snippets}
    evidence: list[Evidence] = []
    for citation in response.result.evidence:
        snippet = allowed.get(citation.snippet_id)
        if snippet is None or citation.quote not in snippet.text or suspicious_text(citation.quote):
            return []
        # Instructions in comments and masked secrets cannot establish runtime behaviour.
        meaningful = [line.strip() for line in citation.quote.splitlines() if line.strip()]
        if not meaningful or all(line.startswith(("#", "//", "/*", "*", "[REDACTED]")) for line in meaningful):
            return []
        if "[REDACTED]" in citation.quote:
            return []
        offset = snippet.text.index(citation.quote)
        start = snippet.start_line + snippet.text[:offset].count("\n")
        end = start + citation.quote.count("\n")
        if end > snippet.end_line:
            return []
        evidence.append(Evidence(file=snippet.file, line=start, end_line=end, text=citation.quote))
    return evidence


def model_finding(candidate: Candidate, response: ModelResponse, snippets: list[Snippet]) -> Finding:
    evidence = validate_evidence(response, snippets)
    if response.result.verdict == "not_found" or not evidence:
        reason = (
            "Model found insufficient context."
            if response.result.verdict == "not_found"
            else "Model evidence rejected."
        )
        finding = undecided(candidate, reason).model_copy(update={"replayed": response.replayed})
        if any(snippet.suspicious for snippet in snippets):
            finding.reasons.append("Instruction-like repository text detected; citation was not accepted as evidence.")
        return finding
    verdict = Verdict.ok
    if response.result.verdict == "contradicted":
        is_default = re.search(
            r"\bdefault\b|\d+\s*(?:seconds?|minutes?|ms|[KMGT]i?B|times)\b", candidate.claim.value, re.I
        )
        verdict = Verdict.default_changed if is_default else Verdict.missing
    injection = any(snippet.suspicious for snippet in snippets)
    reasons = [mask_secrets(response.result.explanation)]
    if injection:
        reasons.append("Instruction-like repository text detected; model confidence reduced.")
    return Finding(
        claim=candidate.claim,
        verdict=verdict,
        suggestion=mask_secrets(response.result.suggested_doc_text) if response.result.suggested_doc_text else None,
        evidence=evidence,
        confidence=0.5 if injection else 0.75,
        reasons=reasons,
        decided_by=DecidedBy.llm,
        replayed=response.replayed,
    )


def merge_findings(rules: list[Finding], model: list[Finding]) -> list[Finding]:
    merged = {item.claim.claim_id: item for item in rules}
    for item in model:
        original = merged.get(item.claim.claim_id)
        if original is not None and original.verdict != Verdict.unverifiable:
            continue
        merged[item.claim.claim_id] = item
    return list(merged.values())


@dataclass
class Budget:
    limit: float
    spent: float = 0
    reserved: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reserve(self, amount: float) -> bool:
        async with self.lock:
            if self.spent + self.reserved + amount > self.limit:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserved: float, actual: float) -> None:
        async with self.lock:
            self.reserved -= reserved
            self.spent += actual


async def verify_prose(result: ScanResult, client: ModelClient | None = None) -> ScanResult:
    config = result.config_used.llm
    candidates = select_candidates(result)
    model = config.model or DEFAULT_MODEL
    selected_client = client or RecordedClient(config)
    budget = Budget(config.budget)
    semaphore = asyncio.Semaphore(config.concurrency)
    started = time.perf_counter()
    protected = {item.claim.claim_id for item in result.findings if item.verdict != Verdict.unverifiable}

    async def evaluate(candidate: Candidate) -> tuple[Finding, CallUsage]:
        if candidate.claim.claim_id in protected:
            return undecided(candidate, "Rules already decided this claim."), CallUsage()
        snippets = retrieve(result.repo_path, candidate, result.symbols, config.redact_paths)
        if not snippets:
            return undecided(candidate, "No permitted source context is available."), CallUsage()
        request = build_request(model, candidate.claim.raw_text, snippets)
        async with semaphore:
            estimate = estimate_request(request)
            if not await budget.reserve(estimate):
                return undecided(candidate, "budget reached"), CallUsage()
            try:
                response = await selected_client.complete(request)
                await budget.settle(estimate, response.usage.cost)
                return model_finding(candidate, response, snippets), response.usage
            except ClientError as exc:
                await budget.settle(estimate, estimate)
                if "Replay miss:" in str(exc) or "OPENROUTER_API_KEY" in str(exc):
                    raise
                return undecided(candidate, str(exc)), CallUsage(cost=estimate)

    tasks = [asyncio.create_task(evaluate(candidate)) for candidate in candidates]
    try:
        done, pending = await asyncio.wait(tasks, timeout=config.timeout) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assessed = [
            task.result() if task in done else (undecided(candidate, "Total model timeout reached."), CallUsage())
            for candidate, task in zip(candidates, tasks, strict=True)
        ]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    findings = merge_findings(result.findings, [finding for finding, _ in assessed])
    usage = {
        "claims": float(len(assessed)),
        "input_tokens": float(sum(call.input_tokens for _, call in assessed)),
        "output_tokens": float(sum(call.output_tokens for _, call in assessed)),
        "cache_read_tokens": float(sum(call.cache_read_tokens for _, call in assessed)),
        "cache_write_tokens": float(sum(call.cache_write_tokens for _, call in assessed)),
        "cost": sum(call.cost for _, call in assessed),
        "seconds": time.perf_counter() - started,
    }
    existing = {claim.claim_id for claim in result.claims}
    return result.model_copy(
        update={
            "findings": findings,
            "claims": [*result.claims, *[item.claim for item in candidates if item.claim.claim_id not in existing]],
            "llm_usage": usage,
        }
    )
