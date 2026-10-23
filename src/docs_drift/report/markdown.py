"""Bounded GitHub Markdown, with escaped untrusted document text."""

import html

from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import health, ordered


def escape(text: object) -> str:
    return (
        html.escape(str(text), quote=True)
        .replace("@", "&#64;")
        .replace("!", "&#33;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("`", "&#96;")
        .replace("|", "&#124;")
        .replace("\n", "<br>")
    )


def fence(text: str, language: str) -> str:
    # A longer delimiter prevents content from closing the block.
    import re  # noqa: PLC0415 - optional dependency or shared pipeline import.

    count = max([2, *[len(m.group()) for m in re.finditer(r"`+", text)]]) + 1
    marker = "`" * count
    return f"{marker}{language}\n{text}\n{marker}"


def render(result: ScanResult, options: RenderOptions) -> str:
    header = f"# Documentation health: {health(result.findings)}/100\n\n| Verdict | Count |\n| --- | ---: |\n"
    header += "".join(f"| {key} | {value} |\n" for key, value in result.summary()["by_verdict"].items())
    note = f"\nReport truncated. [Full artifact]({options.artifact_url.replace(')', '%29')}).\n"
    blocks, previous = [header], None
    for f in ordered(result):
        if f.verdict == "ok":
            continue
        block = f"\n## {escape(f.claim.doc_path)}\n" if previous != f.claim.doc_path else ""
        block += f"\n**{escape(f.verdict)} · line {f.claim.line}** {escape(f.claim.raw_text)}\n\n"
        block += (
            fence(f.suggestion or f.reasons[0], "text")
            if options.pr_mode
            else fence(f"- {f.claim.raw_text}\n+ {f.suggestion or f.reasons[0]}", "diff")
        )
        block += "\n\n<details><summary>Evidence</summary>\n\n"
        for evidence in f.evidence:
            block += f"{escape(evidence.file)}:{evidence.line}\n\n" + fence(evidence.text, "text") + "\n\n"
        if f.introduced_by:
            block += escape(f"{f.introduced_by.short_sha} {f.introduced_by.summary} {f.introduced_by.date.isoformat()}")
        block += "\n</details>\n"
        if len(("".join(blocks) + block + note).encode()) > options.size_cap:
            blocks.append(note)
            break
        blocks.append(block)
        previous = f.claim.doc_path
    return "".join(blocks)
