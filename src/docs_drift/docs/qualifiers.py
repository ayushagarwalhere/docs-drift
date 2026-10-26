"""Extraction-time context that makes a claim's absence from the code uninformative.

Documentation names many things the project does not own: another tool's flags,
a name the doc's own example code defines, a value written as historical record.
Extraction keeps those mentions (they stay visible in reports) but tags them with
a qualifier so matching reports them as unverifiable instead of missing.
"""

from docs_drift.models import Claim, Evidence, Finding, Verdict

CONTEXT_PREFIX = "unverifiable:"
# A symbol written with an explicit cross-reference role ({class}`Name`, :func:`name`) keeps the role.
XREF_PREFIX = "xref:"

_REASONS = {
    "third-party": "The surrounding text attributes this to another tool ({detail}), not to this project.",
    "doc-example": "The documentation's own example code defines this name; it is not expected in the source tree.",
    "placeholder": "This is a generic placeholder used to describe a pattern, not a concrete name.",
    "external-link": "The documentation links this name to external reference material ({detail}).",
    "external-host": "The request targets an external host ({detail}), not a route this project serves.",
    "forwarded": "These arguments are forwarded by a wrapper ({detail}) to another program.",
    "plugin": "This command is provided by a plugin installed earlier in the documentation ({detail}).",
    "generated-env": "This environment variable name is generated at runtime from a prefix ({detail}).",
}


def context_qualifier(reason: str, detail: str = "") -> str:
    """Build the qualifier for a mention whose absence does not establish drift."""
    return f"{CONTEXT_PREFIX}{reason}:{detail}" if detail else f"{CONTEXT_PREFIX}{reason}"


def context_finding(claim: Claim) -> Finding | None:
    """Return an unverifiable finding when extraction marked the claim as not checkable here."""
    if not claim.qualifier or not claim.qualifier.startswith(CONTEXT_PREFIX):
        return None
    reason, _, detail = claim.qualifier.removeprefix(CONTEXT_PREFIX).partition(":")
    template = _REASONS.get(reason, "The documentation context marks this mention as not checkable ({detail}).")
    return Finding(
        claim=claim,
        verdict=Verdict.unverifiable,
        evidence=[Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)],
        confidence=0.2,
        reasons=[template.format(detail=detail or reason) + " Its absence from the code does not establish drift."],
    )
