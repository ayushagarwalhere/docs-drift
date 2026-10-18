"""Optional minimal sentence rewrites, accepted only after static self-checks."""

import asyncio
import hashlib
import re
import time

from docs_drift.docs.defaults import extract_defaults
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.sections import parse_document
from docs_drift.fix.edits import FixPlan, TextEdit, decode, safe_path
from docs_drift.index.base import CodeIndex
from docs_drift.llm.client import ClientError, RecordedClient
from docs_drift.llm.pricing import DEFAULT_MODEL, estimate_request
from docs_drift.llm.prompts import build_request
from docs_drift.llm.retrieve import retrieve
from docs_drift.llm.types import Candidate, ModelClient
from docs_drift.llm.verify import Budget, validate_evidence
from docs_drift.match import match_claims
from docs_drift.models import DecidedBy, DocFile, DocFormat, Finding, ScanResult, Verdict


def sentence_span(line: str, column: int) -> tuple[int, str]:
    prefix = re.match(r"\s*(?:(?:[-*+>]|\d+[.)])\s+)?", line)
    offset = prefix.end() if prefix else 0
    for match in re.finditer(r".+?(?:[.!?](?=\s|$)|$)", line[offset:]):
        start = offset + match.start()
        end = offset + match.end()
        if start <= column - 1 < end:
            sentence = match.group().strip()
            return start + len(match.group()) - len(match.group().lstrip()) + 1, sentence
    return offset + 1, line[offset:].strip()


def valid_rewrite(original: str, rewritten: str, finding: Finding, index: CodeIndex, result: ScanResult) -> bool:
    if not rewritten.strip() or "\n" in rewritten or "\r" in rewritten:
        return False
    if len(re.split(r"(?<=[.!?])\s+", rewritten)) > 1:
        return False
    if finding.verdict == Verdict.renamed and finding.suggestion and finding.suggestion not in rewritten:
        return False
    known = {name for symbol in index.symbols for name in (symbol.name, symbol.qualified_name, *symbol.aliases)}
    identifiers = re.compile(
        r"\b[A-Za-z]\w*_[A-Za-z]\w*\b|\b[A-Za-z]\w*(?:\.[A-Za-z]\w*)+\b|"
        r"\b[A-Za-z]\w*(?=\s*\()|\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b"
    )
    introduced = set(identifiers.findall(rewritten)) - set(identifiers.findall(original))
    if any(name not in known for name in introduced):
        return False
    doc = DocFile(
        path=finding.claim.doc_path,
        content_hash=hashlib.sha256(rewritten.encode()).hexdigest(),
        format=DocFormat.markdown,
    )
    parsed = parse_document(doc, rewritten)
    extracted = extract_claims(parsed).claims
    subjects = [claim for claim in extracted if claim.kind.value != "default_value"]
    defaults = extract_defaults(parsed, subjects).claims
    checked = match_claims([*subjects, *defaults], index, result.repo_path)
    # New uncertain references are rejected as well as proven drift: a rewrite must earn acceptance.
    return all(item.verdict == Verdict.ok for item in checked)


async def add_model_fixes(
    result: ScanResult, index: CodeIndex, plan: FixPlan, client: ModelClient | None = None
) -> FixPlan:
    config = result.config_used.llm
    selected_client = client or RecordedClient(config)
    budget = Budget(max(0, config.budget - result.llm_usage.get("cost", 0)))
    semaphore = asyncio.Semaphore(config.concurrency)
    fixed = {edit.claim_id for edit in plan.edits}
    occupied = {(edit.file, edit.line) for edit in plan.edits}
    remaining = [
        item
        for item in result.findings
        if item.is_drift and item.claim.claim_id not in fixed and (item.claim.doc_path, item.claim.line) not in occupied
    ][: config.max_claims]

    async def propose(finding: Finding) -> TextEdit | None:
        sources = {item.file for item in finding.evidence if item.file in index.by_file}
        if finding.matched_symbol:
            sources.add(finding.matched_symbol.file)
        if not sources:
            return None
        lines = decode(safe_path(result.repo_path, finding.claim.doc_path).read_bytes())[0].splitlines()
        column, original = sentence_span(lines[finding.claim.line - 1], finding.claim.column)
        candidate = Candidate(claim=finding.claim, source_files=sorted(sources))
        snippets = retrieve(result.repo_path, candidate, result.symbols, config.redact_paths)
        if not snippets:
            return None
        data = (
            f"Sentence: {original}\nFinding: {finding.verdict.value}\n"
            f"Evidence-based correction: {finding.suggestion or ''}"
        )
        request = build_request(config.model or DEFAULT_MODEL, data, snippets, rewrite=True)
        async with semaphore:
            estimate = estimate_request(request)
            if not await budget.reserve(estimate):
                plan.suggestions.append(f"{finding.claim.doc_path}:{finding.claim.line}: budget reached")
                return None
            started = time.perf_counter()
            response = await selected_client.complete(request)
            await budget.settle(estimate, response.usage.cost)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                result.llm_usage[key] = result.llm_usage.get(key, 0) + float(getattr(response.usage, key))
            result.llm_usage["claims"] = result.llm_usage.get("claims", 0) + 1
            result.llm_usage["seconds"] = result.llm_usage.get("seconds", 0) + time.perf_counter() - started
        rewritten = response.result.suggested_doc_text
        if (
            not rewritten
            or not validate_evidence(response, snippets)
            or not valid_rewrite(original, rewritten, finding, index, result)
        ):
            return None
        return TextEdit(
            file=finding.claim.doc_path,
            line=finding.claim.line,
            column=column,
            end_line=finding.claim.line,
            end_column=column + len(original),
            old_text=original,
            new_text=rewritten,
            reason="Model sentence rewrite passed static self-checks.",
            claim_id=finding.claim.claim_id,
            decided_by=DecidedBy.llm,
            replayed=response.replayed,
        )

    try:
        async with asyncio.timeout(config.timeout):
            proposed = await asyncio.gather(*(propose(finding) for finding in remaining))
    except TimeoutError:
        plan.suggestions.append("Model fixes exceeded their total timeout; review remaining sentences manually.")
        return plan
    except ClientError:
        raise
    plan.edits.extend(edit for edit in proposed if edit is not None)
    result.llm_usage["cost"] = result.llm_usage.get("cost", 0) + budget.spent
    return plan
