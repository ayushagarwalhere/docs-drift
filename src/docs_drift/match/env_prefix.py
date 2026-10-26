"""Abstain on environment names that a dynamic prefix may supply.

Indexers record prefixes such as viper's ``SetEnvPrefix("app")`` + ``AutomaticEnv()``
or ``envy::prefixed("APP_")`` as ``partial_prefix`` env_var symbols named
``APP_*``. Any name under such a prefix can be valid without appearing as a
literal, so its absence is not evidence of drift: the finding becomes
unverifiable instead of missing, and a prefix symbol is never offered as a
rename target.
"""

from collections.abc import Callable

from docs_drift.index.base import CodeIndex
from docs_drift.models import Claim, ClaimKind, Evidence, Finding, SymbolKind, Verdict

Rename = Callable[[Claim, CodeIndex, Finding], Finding]


def _covering_prefix(claim: Claim, index: CodeIndex) -> str | None:
    value = claim.value.upper()
    for symbol in index.by_kind.get(SymbolKind.env_var, []):
        if symbol.partial_prefix and value.startswith(symbol.name.removesuffix("*").upper()):
            return symbol.name
    return None


def dynamic_env_guard(claim: Claim, index: CodeIndex, base: Finding, rename: Rename) -> Finding:
    """Apply ``rename`` unless a dynamic environment prefix makes the claim unverifiable."""
    if claim.kind == ClaimKind.env_var and base.verdict == Verdict.missing:
        prefix = _covering_prefix(claim, index)
        if prefix is not None:
            source = next(
                symbol
                for symbol in index.by_kind.get(SymbolKind.env_var, [])
                if symbol.partial_prefix and symbol.name == prefix
            )
            return base.model_copy(
                update={
                    "verdict": Verdict.unverifiable,
                    "confidence": 0.2,
                    "suggestion": None,
                    "evidence": [*base.evidence, Evidence(file=source.file, line=source.line, text=source.name)],
                    "reasons": [
                        f"Environment variable {claim.value} falls under the dynamic prefix {prefix} read at "
                        f"{source.file}:{source.line}; names under it need not appear literally in source."
                    ],
                }
            )
    result = rename(claim, index, base)
    if result.matched_symbol is not None and result.matched_symbol.partial_prefix:
        return base
    return result
