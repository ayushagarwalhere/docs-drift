"""GitHub workflow commands, with command injection escaping."""

from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import message, ordered


def escape(text: object, prop: bool = False) -> str:
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def render(result: ScanResult, options: RenderOptions) -> str:
    return "".join(
        f"::{'error' if f.verdict == 'invalid_command' else 'warning'} "
        f"file={escape(f.claim.doc_path, True)},line={f.claim.line},col={f.claim.column}::"
        f"{escape(message(f))} "
        f"{escape('; '.join(f'{e.file}:{e.line}: {e.text}' for e in f.evidence)[:2000])} "
        f"{escape('commit ' + f.introduced_by.sha if f.introduced_by else 'commit attribution unavailable')}\n"
        for f in ordered(result)
        if f.is_drift
    )
