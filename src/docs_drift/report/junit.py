"""One JUnit case for every checked claim."""

from xml.etree.ElementTree import Element, SubElement, tostring

from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import message, ordered


def render(result: ScanResult, options: RenderOptions) -> str:
    suite = Element(
        "testsuite",
        name="docs-drift",
        tests=str(len(result.findings)),
        failures=str(sum(f.is_drift for f in result.findings)),
    )
    for f in ordered(result):
        case = SubElement(
            suite,
            "testcase",
            name=f.claim.claim_id,
            classname=f.claim.doc_path,
            file=f.claim.doc_path,
            line=str(f.claim.line),
        )
        if f.is_drift:
            SubElement(case, "failure", message=message(f), type=f.verdict.value).text = "\n".join(f.reasons)
        elif f.verdict == "unverifiable":
            SubElement(case, "skipped", message=f.reasons[0])
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(suite, encoding="unicode") + "\n"
