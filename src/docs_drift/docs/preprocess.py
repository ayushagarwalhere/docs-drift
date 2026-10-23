"""Line-preserving MDX stripping and explicit reStructuredText claims."""

import re

from docs_drift.docs.sections import ParsedDoc
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction


def strip_mdx(text: str) -> str:
    lines, fence, importing = [], None, False
    for line in text.splitlines(keepends=True):
        marker = re.match(r"\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence else marker[1][0]
        if fence or marker:
            lines.append(line)
            continue
        if re.match(r"^\s*(import|export)\b", line) or importing:
            importing = not bool(re.search(r"(?:from\s+['\"].*['\"]|[;}])\s*;?\s*$", line))
            lines.append("\n" if line.endswith("\n") else "")
        else:
            # Strip wrappers, retaining child code fences and inline Markdown.
            lines.append(re.sub(r"</?[A-Z][\w.]*(?:\s[^>]*?)?/?>", lambda m: " " * len(m[0]), line))
    return "".join(lines)


def rst_claims(parsed: ParsedDoc) -> list[Claim]:
    claims = []
    counts: dict[tuple[str, str, str], int] = {}
    for line_no, line in enumerate(parsed.text.splitlines(), 1):
        for match in re.finditer(r":(option|envvar):`([^`]+)`|``([^`]+)``", line):
            value = match[2] or match[3]
            kind = ClaimKind.env_var if match[1] == "envvar" else ClaimKind.cli_flag if value.startswith("-") else None
            if kind is None:
                from docs_drift.docs.extract_inline import (  # noqa: PLC0415 - shared lexical rules.
                    _classify,
                    _explicit_api_reference,
                    path_context,
                )

                # Share explicit API disambiguation without broadening unrelated
                # RST literals into Markdown-only configuration/prose heuristics.
                classified = _classify(value)
                if _explicit_api_reference(value, line):
                    kind = ClaimKind.symbol
                elif classified:
                    kind = classified[0]
                    value = classified[1]
            section = parsed.line_to_section.get(line_no)
            if kind is None or section is None:
                continue
            key = (section.section_id, kind.value, value)
            occurrence = counts.get(key, 0)
            counts[key] = occurrence + 1
            claims.append(
                Claim(
                    claim_id=make_claim_id(parsed.doc.path, section.heading_path, kind.value, value, occurrence),
                    kind=kind,
                    raw_text=line,
                    value=value,
                    qualifier=path_context(line, value) if kind == ClaimKind.file_path else None,
                    doc_path=parsed.doc.path,
                    line=line_no,
                    column=match.start(2 if match[2] else 3) + 1,
                    section_id=section.section_id,
                    extraction=Extraction.inline_code,
                    extraction_confidence=0.99 if match[1] else 0.9,
                )
            )
    return claims
