"""Pure Markdown rendering for GitHub comments, reviews, check runs and issues.

Every repository-derived string is escaped with the report escaper (mentions,
links, images, HTML, backticks, table pipes) or placed in a fence longer than any
backtick run it contains. Bodies stay below GitHub's 65,536-character limit.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from docs_drift import __version__
from docs_drift.github.bundle import LineSuggestion, ReviewBundle
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import Finding
from docs_drift.report.markdown import escape, fence

SUMMARY_MARKER = "<!-- docs-drift:summary -->"
ISSUE_MARKER = "<!-- docs-drift:tracking-issue -->"
GITHUB_COMMENT_LIMIT = 65_536
BODY_BUDGET = 60_000
MAX_TABLE_ROWS = 50
TRUNCATION_NOTE = "\n\n> [!NOTE]\n> This comment was truncated to fit GitHub's size limit. See the full report.\n"
_INLINE = re.compile(r"<!-- docs-drift:inline key=([0-9a-f]{16}) hash=([0-9a-f]{16})(?: state=(\w+))? -->")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SLUG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
FOOTER = (
    f"<sub>docs-drift {escape(__version__)} · static analysis of documentation against source; "
    "no repository code was executed. A pass is not proof that documentation is correct.</sub>"
)


def _closers(text: str) -> str:
    """Close an open fence (with its own delimiter length) and any open <details>, innermost first."""
    fence_marker: str | None = None
    for line in text.splitlines():
        run = re.match(r"^\s{0,3}(`{3,})", line)
        if not run:
            continue
        if fence_marker is None:
            fence_marker = run.group(1)
        elif set(line.strip()) == {"`"} and len(line.strip()) >= len(fence_marker):
            fence_marker = None
    closers = f"\n{fence_marker}" if fence_marker else ""
    # Only count tags outside the open fence; inside it they are literal text.
    visible = text if fence_marker is None else text[: text.rfind(fence_marker)]
    return closers + "\n</details>" * max(0, visible.count("<details>") - visible.count("</details>"))


def truncate(body: str, limit: int = GITHUB_COMMENT_LIMIT) -> str:
    """Cut at a line boundary below ``limit`` and say so; markers at the top are preserved.

    Open fences and <details> are closed so the note is never swallowed, and the closers
    are reserved inside the limit rather than cut off by it.
    """
    if len(body) <= limit:
        return body
    room = limit - len(TRUNCATION_NOTE)
    while room > 0:
        cut = body.rfind("\n", 0, room)
        head = body[: cut if cut > 0 else room]
        closers = _closers(head)
        if len(head) + len(closers) + len(TRUNCATION_NOTE) <= limit:
            return head + closers + TRUNCATION_NOTE
        room = len(head) - max(1, len(closers))
    return TRUNCATION_NOTE.strip()


def short(sha: str | None) -> str:
    return (sha or "unknown")[:7]


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.fullmatch(slug)) and ".." not in slug


def blob_url(server_url: str, slug: str, sha: str, path: str, line: int) -> str | None:
    """Permalink pinned to a scanned commit; None when any component is not trustworthy."""
    if not valid_slug(slug) or not _SHA.fullmatch(sha) or not re.fullmatch(r"https://[A-Za-z0-9.-]+", server_url):
        return None
    return f"{server_url}/{slug}/blob/{sha}/{quote(path, safe='/')}#L{int(line)}"


def location(finding: Finding, server_url: str, slug: str, sha: str) -> str:
    text = f"{escape(finding.claim.doc_path)}:{finding.claim.line}"
    url = blob_url(server_url, slug, sha, finding.claim.doc_path, finding.claim.line)
    return f"[{text}]({url})" if url else text


def code(text: str) -> str:
    """Inline code for table cells: escaped text inside <code>, which cannot be broken out of."""
    return f"<code>{escape(mask_secrets(text))}</code>"


def fix_text(finding: Finding) -> str:
    return mask_secrets(finding.suggestion or finding.reasons[0])


def group_key(claim_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(claim_ids)).encode()).hexdigest()[:16]


def content_hash(key: str, body: str) -> str:
    return hashlib.sha256((key + "\0" + body).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class InlineComment:
    key: str
    path: str
    line: int
    body: str
    claim_ids: tuple[str, ...]

    @property
    def digest(self) -> str:
        return content_hash(self.key, self.body)

    @property
    def marked_body(self) -> str:
        return truncate(f"<!-- docs-drift:inline key={self.key} hash={self.digest} -->\n{self.body}")

    def payload(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "side": "RIGHT", "body": self.marked_body}


def parse_inline_marker(body: str) -> tuple[str, str, str | None] | None:
    match = _INLINE.search(body or "")
    return (match.group(1), match.group(2), match.group(3)) if match else None


def inline_comment(findings: list[Finding], suggestion: LineSuggestion | None) -> InlineComment:
    """One comment per document line, with a ```suggestion block when the edit is certain."""
    first = findings[0]
    parts = []
    for finding in findings:
        claim = code(finding.claim.value)
        verdict = escape(finding.verdict.value)
        parts.append(
            f"**docs-drift · {verdict}** {claim} — {escape(fix_text(finding))} "
            f"<sub>(confidence {finding.confidence:.2f})</sub>"
        )
    body = "\n\n".join(parts)
    if suggestion is not None:
        body += "\n\n" + fence(suggestion.replacement, "suggestion")
    evidence = [e for finding in findings for e in finding.evidence if e.file != first.claim.doc_path][:3]
    if evidence:
        body += "\n\n<details><summary>Source evidence</summary>\n\n"
        for item in evidence:
            body += f"{escape(item.file)}:{item.line}\n\n{fence(mask_secrets(item.text)[:1500], 'text')}\n\n"
        body += "</details>"
    claim_ids = tuple(sorted(f.claim.claim_id for f in findings))
    return InlineComment(group_key(claim_ids), first.claim.doc_path, first.claim.line, body, claim_ids)


def resolved_inline(key: str, head_sha: str, superseded: bool) -> str:
    note = (
        "♻️ Outdated: this finding changed and is tracked in a newer docs-drift comment or the summary."
        if superseded
        else f"✅ docs-drift no longer reports drift here as of `{short(head_sha)}`."
    )
    return f"<!-- docs-drift:inline key={key} hash={'0' * 16} state=resolved -->\n{note}"


def verdict_table(findings: list[Finding]) -> str:
    counts = Counter(f.verdict.value for f in findings)
    rows = "".join(f"| {escape(name)} | {count} |\n" for name, count in sorted(counts.items()))
    return "| Verdict | New findings |\n| --- | ---: |\n" + rows


def findings_table(  # noqa: PLR0913, PLR0917 - rendering context.
    findings: list[Finding], server_url: str, slug: str, sha: str, inline: set[str], budget: int
) -> tuple[str, int]:
    """Return the table and how many findings were omitted to stay within ``budget``."""
    header = "| Location | Verdict | Documented | Suggested fix | |\n| --- | --- | --- | --- | --- |\n"
    table = header
    shown = 0
    for finding in sorted(findings, key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id)):
        marker = "💬 inline" if finding.claim.claim_id in inline else ""
        row = (
            f"| {location(finding, server_url, slug, sha)} | {escape(finding.verdict.value)} | "
            f"{code(finding.claim.value)} | {escape(fix_text(finding))[:400]} | {marker} |\n"
        )
        if shown >= MAX_TABLE_ROWS or len(table) + len(row) > budget:
            break
        table += row
        shown += 1
    return table, len(findings) - shown


def summary_comment(  # noqa: PLR0913 - rendering context.
    bundle: ReviewBundle,
    *,
    slug: str,
    server_url: str = "https://github.com",
    head_sha: str | None = None,
    inline_ids: set[str] | None = None,
    artifact_url: str | None = None,
) -> str:
    """The sticky PR summary. A clean bundle renders the collapsed '✅ no new drift' state."""
    sha = head_sha or bundle.head_sha
    gate = bundle.gate
    link = f"\n\n📎 [Full report and artifacts]({artifact_url.replace(')', '%29')})" if artifact_url else ""
    if bundle.clean:
        return truncate(
            f"{SUMMARY_MARKER}\n### ✅ docs-drift: no new documentation drift\n\n"
            f"Checked `{short(sha)}` against base `{short(bundle.base_sha)}`. "
            f"Gate: **{escape(gate.status)}**; {gate.claims_extracted} documentation claims checked; "
            f"{gate.baseline_drift} baseline-accepted and {gate.reviewed_drift} reviewed findings are not repeated."
            f"{link}\n\n{FOOTER}\n"
        )
    inline_ids = inline_ids or set()
    count = len(bundle.findings)
    noun = "finding" if count == 1 else "findings"
    title = f"📄 docs-drift: {count} new documentation drift {noun}" if count else "✅ docs-drift: no new drift"
    head = (
        f"{SUMMARY_MARKER}\n### {title}\n\n"
        f"Gate: **{escape(gate.status)}** · head `{short(sha)}` · base `{short(bundle.base_sha)}` · "
        f"{gate.qualifying_new_drift} qualifying of {gate.new_drift} new "
        f"(confidence ≥ {gate.policy.min_confidence:g}, allowed {gate.policy.max_findings})\n\n"
        + (verdict_table(bundle.findings) if count else "")
    )
    every = [*bundle.findings, *bundle.touched]
    inline_count = len(inline_ids & {f.claim.claim_id for f in every})
    notes = (
        f"\n{inline_count} of {len(every)} are on lines changed by this PR and have inline review comments"
        + (" with suggested changes" if bundle.suggestions else "")
        + ". GitHub only accepts review comments on diff lines, so the rest are listed here.\n"
    )
    details = (
        "\n<details><summary>Gate details</summary>\n\n"
        f"- Claims extracted: {gate.claims_extracted}\n- Baseline accepted: {gate.baseline_drift}\n"
        f"- Reviewed (suppressed): {gate.reviewed_drift}\n- Below threshold: {gate.below_threshold_new_drift}\n"
        + "".join(f"- Warning: {escape(w)[:300]}\n" for w in bundle.warnings[:10])
        + "\n</details>\n"
    )
    tail = f"{link}\n\n{FOOTER}\n"
    budget = BODY_BUDGET - len(head) - len(notes) - len(details) - len(tail) - 400
    body = head + notes
    if count:
        table, omitted = findings_table(bundle.findings, server_url, slug, sha, inline_ids, budget // 2)
        body += "\n" + table + _more(omitted)
    if bundle.touched:
        table, omitted = findings_table(bundle.touched, server_url, slug, sha, inline_ids, budget // 2)
        body += (
            "\n#### Existing drift on lines this PR edits\n\n"
            "Not introduced by this PR and not counted by the gate; consider fixing it while you are here.\n\n"
            + table
            + _more(omitted)
        )
    return truncate(body + details + tail)


def _more(omitted: int) -> str:
    noun = "finding" if omitted == 1 else "findings"
    return f"\n…and {omitted} more {noun} in the full report.\n" if omitted else ""


def review_body(count: int) -> str:
    noun = "comment" if count == 1 else "comments"
    return f"docs-drift: {count} inline {noun} on documentation lines changed in this PR. See the summary comment."


def check_run_output(bundle: ReviewBundle) -> dict[str, str]:
    gate = bundle.gate
    title = (
        "No new documentation drift"
        if not bundle.findings
        else f"{len(bundle.findings)} new documentation drift finding{'s' if len(bundle.findings) != 1 else ''}"
    )
    summary = (
        f"Gate **{escape(gate.status)}**: {gate.qualifying_new_drift} qualifying new drift "
        f"(allowed {gate.policy.max_findings}, confidence ≥ {gate.policy.min_confidence:g}); "
        f"{gate.claims_extracted} claims checked."
    )
    return {"title": title, "summary": truncate(summary)}


def annotations(bundle: ReviewBundle) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for finding in bundle.findings:
        level = "failure" if finding.verdict.value == "invalid_command" else "warning"
        rows.append(
            {
                "path": finding.claim.doc_path,
                "start_line": finding.claim.line,
                "end_line": finding.claim.line,
                "annotation_level": level,
                "title": f"docs-drift: {finding.verdict.value}"[:255],
                "message": mask_secrets(f"{finding.claim.value}: {fix_text(finding)}")[:4000],
            }
        )
    return rows


def issue_title(count: int) -> str:
    noun = "statement" if count == 1 else "statements"
    return f"Documentation drift: {count} documented {noun} no longer match the code"


def issue_body(
    bundle: ReviewBundle, *, slug: str, server_url: str = "https://github.com", run_url: str | None = None
) -> str:
    """Tracking-issue body grouped by document, with permalinks pinned to the scanned SHA."""
    sha = bundle.head_sha
    head = (
        f"{ISSUE_MARKER}\n## Documentation drift report\n\n"
        f"docs-drift found **{len(bundle.findings)}** documented statements that the code at "
        f"`{short(sha)}` no longer supports. This issue is updated in place by scheduled scans and closes "
        "automatically when drift reaches zero.\n\n" + verdict_table(bundle.findings) + "\n"
    )
    if run_url:
        head += f"📎 [Latest scan run]({run_url.replace(')', '%29')})\n\n"
    tail = f"\n{FOOTER}\n"
    body = head
    by_doc: dict[str, list[Finding]] = {}
    for finding in sorted(bundle.findings, key=lambda f: (f.claim.doc_path, f.claim.line)):
        by_doc.setdefault(finding.claim.doc_path, []).append(finding)
    omitted = 0
    for doc, items in by_doc.items():
        section = f"### {escape(doc)}\n\n"
        for finding in items:
            section += (
                f"- [ ] {location(finding, server_url, slug, sha)} **{escape(finding.verdict.value)}** "
                f"{code(finding.claim.value)} — {escape(fix_text(finding))[:400]}\n"
            )
        if len(body) + len(section) + len(tail) > BODY_BUDGET:
            omitted += len(items)
            continue
        body += section + "\n"
    if omitted:
        body += f"…and {omitted} more findings in the full report.\n"
    return truncate(body + tail)


def resolved_issue_comment(sha: str) -> str:
    return f"✅ docs-drift found no documentation drift at `{short(sha)}`. Closing automatically."


def reopened_issue_comment(sha: str, count: int) -> str:
    return f"🔁 docs-drift found {count} drift findings again at `{short(sha)}`. Reopening."
