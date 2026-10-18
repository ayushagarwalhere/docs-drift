"""Conservative rename proposals for exact matches that have disappeared."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from docs_drift.index.base import CodeIndex
from docs_drift.models import Claim, ClaimKind, Evidence, Finding, SymbolKind, Verdict

_KINDS = {
    ClaimKind.cli_flag: (SymbolKind.cli_flag,),
    ClaimKind.cli_command: (SymbolKind.cli_command,),
    ClaimKind.env_var: (SymbolKind.env_var,),
    ClaimKind.endpoint: (SymbolKind.route,),
    ClaimKind.config_key: (SymbolKind.config_key,),
    ClaimKind.symbol: (SymbolKind.function, SymbolKind.class_, SymbolKind.method),
    ClaimKind.package_script: (SymbolKind.package_script,),
    ClaimKind.make_target: (SymbolKind.make_target,),
}

_THRESHOLD = 82
_MIN_MARGIN = 8


def similarity(old: str, new: str) -> int:
    """Combine character and token similarity so formatting changes remain comparable."""
    old_tokens = set(re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", old).casefold()))
    new_tokens = set(re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", new).casefold()))
    token_score = round(100 * len(old_tokens & new_tokens) / max(1, len(old_tokens | new_tokens)))
    return round(0.6 * fuzz.ratio(old.casefold(), new.casefold()) + 0.4 * token_score)


def rename_finding(claim: Claim, index: CodeIndex, base: Finding) -> Finding:
    """Upgrade a missing result only when one same-kind candidate is clearly close."""
    if base.verdict != Verdict.missing or claim.kind not in _KINDS:
        return base
    pool = [symbol for kind in _KINDS[claim.kind] for symbol in index.by_kind.get(kind, [])]
    ranked = sorted(
        ((similarity(claim.value, symbol.name), symbol) for symbol in pool),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < _THRESHOLD:
        return base
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < _MIN_MARGIN:
        return base
    score, symbol = ranked[0]
    return base.model_copy(
        update={
            "verdict": Verdict.renamed,
            "suggestion": symbol.name,
            "matched_symbol": symbol,
            "confidence": min(0.94, score / 100),
            "evidence": [*base.evidence, Evidence(file=symbol.file, line=symbol.line, text=symbol.qualified_name)],
            "reasons": [*base.reasons, f"Likely rename: {claim.value} resembles {symbol.name} ({score}/100)."],
        }
    )
