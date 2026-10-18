"""Select behavioural sentences only when static source anchors exist."""

import re

from docs_drift.docs.pipeline import _ignored, _suppression
from docs_drift.docs.sections import is_historical_span, parse_document
from docs_drift.ids import make_claim_id
from docs_drift.llm.types import Candidate
from docs_drift.models import Claim, ClaimKind, Extraction, ScanResult

_BEHAVIOUR = re.compile(
    r"\b(?:by default|will|always|never|only|at most|retr(?:y|ies)|rejects?|skips?|requires?|time out)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:ms|seconds?|minutes?|hours?|[KMGT]i?B|bytes?|times)\b",
    re.I,
)
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|$)", re.S)
MIN_ANCHOR_LENGTH = 3


def select_candidates(result: ScanResult) -> list[Candidate]:  # noqa: PLR0912 - conservative eligibility gates.
    """Prioritize sections with drift; historical and suppressed text never becomes a candidate."""
    files_by_section: dict[str, set[str]] = {}
    drift_sections = {item.claim.section_id for item in result.findings if item.is_drift}
    decided_lines = {
        (item.claim.doc_path, item.claim.line) for item in result.findings if item.verdict != "unverifiable"
    }
    for finding in result.findings:
        if finding.matched_symbol:
            files_by_section.setdefault(finding.claim.section_id, set()).add(finding.matched_symbol.file)
    candidates: dict[str, Candidate] = {}
    for doc in result.docs:
        path = (result.repo_path / doc.path).resolve()
        if not path.is_relative_to(result.repo_path.resolve()):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        parsed = parse_document(doc, text)
        for index, token in enumerate(parsed.tokens):
            if token.type != "inline" or token.map is None:
                continue
            if index and parsed.tokens[index - 1].type == "heading_open":
                continue
            for match in _SENTENCE.finditer(token.content):
                sentence = match.group().strip()
                line = token.map[0] + 1 + token.content[: match.start()].count("\n")
                section = parsed.line_to_section.get(line)
                if not sentence or not _BEHAVIOUR.search(sentence) or not section:
                    continue
                if is_historical_span(section, line) or (doc.path, line) in decided_lines:
                    continue
                anchored = {
                    symbol.file
                    for symbol in result.symbols
                    if len(symbol.name) >= MIN_ANCHOR_LENGTH
                    and re.search(rf"(?<!\w){re.escape(symbol.name)}(?!\w)", sentence)
                }
                source_files = sorted(anchored | files_by_section.get(section.section_id, set()))
                if not source_files:
                    continue
                claim = Claim(
                    claim_id=make_claim_id(doc.path, section.heading_path, ClaimKind.prose, sentence),
                    kind=ClaimKind.prose,
                    value=sentence,
                    raw_text=sentence,
                    doc_path=doc.path,
                    line=line,
                    column=max(1, text.splitlines()[line - 1].find(sentence.splitlines()[0]) + 1),
                    section_id=section.section_id,
                    extraction=Extraction.prose_pattern,
                    extraction_confidence=0.55,
                )
                if _suppression(parsed, claim) or _ignored(claim, result.config_used.ignore):
                    continue
                candidates[claim.claim_id] = Candidate(claim=claim, source_files=source_files)
    ordered = sorted(
        candidates.values(),
        key=lambda item: (item.claim.section_id not in drift_sections, item.claim.doc_path, item.claim.line),
    )
    return ordered[: result.config_used.llm.max_claims]
