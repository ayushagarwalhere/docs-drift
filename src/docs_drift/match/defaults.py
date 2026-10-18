"""Compare documented defaults only with values extracted from static source."""

from docs_drift.docs.defaults import normalize_value
from docs_drift.index.base import CodeIndex, normalize_name
from docs_drift.models import Claim, ClaimKind, Evidence, Finding, Symbol, SymbolKind, Verdict

_CLAIM_TO_SYMBOL = {
    "cli_flag": SymbolKind.cli_flag,
    "env_var": SymbolKind.env_var,
    "config_key": SymbolKind.config_key,
}


def _subject(claim: Claim, index: CodeIndex) -> list[Symbol]:
    kind = _CLAIM_TO_SYMBOL.get(claim.qualifier or "")
    if kind is None:
        return []
    return [
        symbol
        for symbol in index.by_kind.get(kind, [])
        if normalize_name(claim.value)
        in {normalize_name(symbol.name), *(normalize_name(alias) for alias in symbol.aliases)}
    ]


def match_default(claim: Claim, index: CodeIndex) -> Finding:
    """Create one evidence-backed result for a default value claim."""
    candidates = [symbol for symbol in _subject(claim, index) if symbol.authoritative]
    if not candidates:
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)],
            confidence=0.3,
            reasons=["No authoritative indexed default was available for this subject."],
        )
    # Duplicate settings definitions are ambiguous; abstain rather than selecting an arbitrary value.
    defaults = {
        (symbol.default, symbol.file, symbol.line): symbol for symbol in candidates if symbol.default is not None
    }
    if len(defaults) != 1:
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)],
            confidence=0.3,
            reasons=["The indexed default is missing or ambiguous across multiple definitions."],
        )
    symbol = next(iter(defaults.values()))
    actual = normalize_value(symbol.default or "", subject=claim.value)
    expected = normalize_value(claim.expected_default or "", subject=claim.value)
    equal = expected.equals(actual)
    verdict = Verdict.ok if equal is True else Verdict.default_changed if equal is False else Verdict.unverifiable
    confidence = 0.95 if equal is not None else 0.35
    reason = (
        f"Documented default {claim.expected_default!r} matches code."
        if equal is True
        else f"Documented default {claim.expected_default!r} differs from code default {symbol.default!r}."
        if equal is False
        else (
            f"Cannot safely compare documented default {claim.expected_default!r} with code default {symbol.default!r}."
        )
    )
    return Finding(
        claim=claim,
        matched_symbol=symbol,
        verdict=verdict,
        suggestion=symbol.default if equal is False else None,
        evidence=[
            Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text),
            Evidence(file=symbol.file, line=symbol.line, text=f"{symbol.qualified_name} default={symbol.default}"),
        ],
        confidence=confidence,
        reasons=[reason],
    )


def match_default_claims(claims: list[Claim], index: CodeIndex) -> list[Finding]:
    return [match_default(claim, index) for claim in claims if claim.kind == ClaimKind.default_value]
