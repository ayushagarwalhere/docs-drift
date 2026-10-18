"""SARIF 2.1.0 with stable claim fingerprints."""

import json
from urllib.parse import quote

from docs_drift import __version__
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import RULES, message, ordered


def render(result: ScanResult, options: RenderOptions) -> str:
    rules = [
        {
            "id": rid,
            "name": name,
            "shortDescription": {"text": help_text},
            "help": {"text": help_text},
            "helpUri": f"https://github.com/ayushagarwalhere/docs-drift/blob/main/docs/rules/{rid}.md",
        }
        for key, (rid, name, help_text) in RULES.items()
        if key != "ok"
    ]
    results = []
    for f in ordered(result):
        if f.verdict == "ok":
            continue
        results.append(
            {
                "ruleId": RULES[f.verdict][0],
                "level": "note"
                if f.decided_by == "llm" or not f.is_drift
                else "error"
                if f.verdict == "invalid_command"
                else "warning",
                "message": {"text": message(f)},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": quote(f.claim.doc_path)},
                            "region": {
                                "startLine": f.claim.line,
                                "startColumn": f.claim.column,
                                "endColumn": f.claim.column + len(f.claim.value),
                            },
                        }
                    }
                ],
                "partialFingerprints": {"claimId/v1": f.claim.claim_id},
                "properties": {"confidence": f.confidence, "decided_by": f.decided_by.value},
            }
        )
    return (
        json.dumps(
            {
                "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {"driver": {"name": "docs-drift", "version": __version__, "rules": rules}},
                        "results": results,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
