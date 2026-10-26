"""Contributor mode: turn well-evidenced drift into beginner-friendly issue drafts and a health card.

Everything here is derived from a finished scan and the conservative rule fix plan. Nothing is
posted anywhere: drafts are Markdown or JSON for a person to review, file and assign.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from docs_drift.drift_age import AgeStatus, age_status, days, utc_date
from docs_drift.fix.edits import FixPlan, StaleDocumentError, TextEdit, unified_patch
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.llm.privacy import mask_secrets, redact_result
from docs_drift.models import Finding, ScanResult, Verdict
from docs_drift.report.common import health
from docs_drift.report.html import github_remote
from docs_drift.report.markdown import escape, fence

Difficulty = Literal["good-first-issue", "easy", "medium"]
MAX_LOCATIONS_PER_ISSUE = 20
MAX_EVIDENCE_PER_ISSUE = 8
MAX_EXCERPT_CHARS = 600
MAX_EXCERPT_LINES = 12
MAX_TITLE_CHARS = 120
DEFAULT_LIMIT = 50
CARD_START = "<!-- docs-drift:health-card:start -->"
CARD_END = "<!-- docs-drift:health-card:end -->"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

WHY = {
    Verdict.missing: (
        "The documentation names something the code no longer provides. Readers who follow it get an error, "
        "or a setting that silently does nothing."
    ),
    Verdict.renamed: (
        "The code renamed this, but the documentation still uses the old name. Copying the example fails, "
        "and searching the docs for the new name finds nothing."
    ),
    Verdict.default_changed: (
        "The documented default no longer matches the code. Readers plan capacity, timeouts or behaviour "
        "around a value the program does not use."
    ),
    Verdict.invalid_command: (
        "This command example no longer parses against the program's declared interface, so copying it fails."
    ),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueLocation(_Model):
    claim_id: str
    file: str
    line: int
    verdict: str
    kind: str
    stale_value: str
    current_text: str
    correct_value: str | None
    proposed_text: str | None
    certain_fix: bool
    confidence: float
    stale_since: date | None = None
    age_days: int | None = None
    fix_by: date | None = None
    url: str | None = None


class IssueEvidence(_Model):
    file: str
    line: int
    excerpt: str
    url: str | None = None


class IssueCommit(_Model):
    sha: str
    short_sha: str
    summary: str
    date: date
    url: str | None = None


class ContributionIssue(_Model):
    issue_id: str
    title: str
    difficulty: Difficulty
    labels: list[str]
    why_it_matters: str
    grouping: Literal["rename", "section"]
    locations: list[IssueLocation]
    evidence: list[IssueEvidence]
    commits: list[IssueCommit]
    verify: list[str]
    patch: str | None = None


class ContributionReport(_Model):
    schema_version: Literal[1] = 1
    head_sha: str
    scan_date: date
    remote: str | None
    min_confidence: float
    total_drift: int
    eligible_drift: int
    skipped_drift: int
    truncated: bool = False
    # Totals before --limit, so summaries count every ready draft, not only the ones listed.
    available_issues: int = 0
    available_good_first_issues: int = 0
    issues: list[ContributionIssue] = Field(default_factory=list)


class StaleDoc(_Model):
    path: str
    drift: int


class OldestDrift(_Model):
    file: str
    line: int
    verdict: str
    stale_since: date
    age_days: int


class HealthCard(_Model):
    schema_version: Literal[1] = 1
    head_sha: str
    scan_date: date
    health: int
    claims: int
    by_verdict: dict[str, int]
    drift: int
    documents_with_drift: int
    # The five documents with the most drift; documents_with_drift counts all of them.
    stale_documents: list[StaleDoc]
    oldest_drift: OldestDrift | None
    overdue: int
    # Every ready draft at the threshold, not only those under --limit.
    issue_drafts: int
    good_first_issues: int


def _clean(text: str, limit: int) -> str:
    """Single-line, bounded, control-free text; presentation escaping happens at render time."""
    flat = _CONTROL.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _excerpt(text: str) -> str:
    lines = mask_secrets(text).splitlines()[:MAX_EXCERPT_LINES]
    joined = "\n".join(_CONTROL.sub(" ", line).rstrip() for line in lines)
    return joined if len(joined) <= MAX_EXCERPT_CHARS else joined[: MAX_EXCERPT_CHARS - 1] + "…"


def _short_ids(findings: Sequence[Finding]) -> dict[str, str]:
    """Shortest unambiguous prefix (at least 18 characters) for each claim ID, for `docs-drift explain`."""
    ids = sorted({f.claim.claim_id for f in findings})
    result = {}
    for claim_id in ids:
        size = 18
        while size < len(claim_id) and sum(other.startswith(claim_id[:size]) for other in ids) > 1:
            size += 4
        result[claim_id] = claim_id[:size]
    return result


class _Links:
    """Pinned GitHub blob links when the origin remote and HEAD are known; otherwise relative paths."""

    def __init__(self, result: ScanResult) -> None:
        self.base = github_remote(result.remote_url)
        self.head = result.head_sha if result.head_sha.strip("0") else None
        self.docs_elsewhere = result.docs_repo_path is not None

    def blob(self, path: str, line: int, *, doc: bool = False) -> str | None:
        if not self.base or not self.head or (doc and self.docs_elsewhere):
            return None
        return f"{self.base}/blob/{self.head}/{quote(path, safe='/')}#L{line}"

    def commit(self, sha: str) -> str | None:
        return f"{self.base}/commit/{sha}" if self.base else None


def eligible(finding: Finding, threshold: float, fixed: set[str]) -> bool:
    """Drift above the threshold with a certain fix, a known introducing commit, or a matched replacement."""
    if not finding.is_drift or finding.confidence < threshold:
        return False
    return (
        finding.claim.claim_id in fixed
        or finding.introduced_by is not None
        or (finding.matched_symbol is not None and finding.suggestion is not None)
    )


def _group_key(finding: Finding) -> tuple[str, ...]:
    if finding.verdict == Verdict.renamed and finding.suggestion:
        return ("rename", finding.claim.kind.value, finding.claim.value, finding.suggestion)
    return ("section", finding.claim.doc_path, finding.claim.section_id)


def _proposed_line(text: str | None, edits: Sequence[TextEdit]) -> str | None:
    if text is None or not edits:
        return None
    line = text
    for edit in sorted(edits, key=lambda e: e.column, reverse=True):
        start, end = edit.column - 1, edit.end_column - 1
        if line[start:end] != edit.old_text:
            return None
        line = line[:start] + edit.new_text + line[end:]
    return line


def _title(grouping: str, findings: Sequence[Finding], heading: str) -> str:
    first = findings[0]
    places = f" ({len(findings)} places)" if len(findings) > 1 else ""
    if grouping == "rename":
        text = f"Docs: replace renamed `{first.claim.value}` with `{first.suggestion}`{places}"
    elif len(findings) > 1:
        where = f"{first.claim.doc_path} § {heading}" if heading else first.claim.doc_path
        text = f"Docs: fix {len(findings)} stale references in {where}"
    elif first.verdict == Verdict.default_changed:
        text = f"Docs: default for `{first.claim.value}` is now {first.suggestion or 'different'}"
    elif first.verdict == Verdict.invalid_command:
        text = f"Docs: command example in {first.claim.doc_path} uses invalid `{first.claim.value}`"
    elif first.verdict == Verdict.missing and first.suggestion:
        text = f"Docs: `{first.claim.value}` no longer exists; the code uses `{first.suggestion}`"
    else:
        text = f"Docs: `{first.claim.value}` in {first.claim.doc_path} no longer exists in the code"
    return _clean(text, MAX_TITLE_CHARS)


def _difficulty(locations: Sequence[IssueLocation], edits: Sequence[TextEdit]) -> Difficulty:
    if not all(location.certain_fix for location in locations):
        return "medium"
    if len({(edit.file, edit.line) for edit in edits}) == 1:
        return "good-first-issue"
    return "easy"


def _issue(  # noqa: PLR0913 - one issue's complete context.
    key: tuple[str, ...],
    findings: Sequence[Finding],
    *,
    plan: FixPlan,
    result: ScanResult,
    links: _Links,
    short: dict[str, str],
) -> ContributionIssue:
    root = result.docs_repo_path or result.repo_path
    claim_ids = {f.claim.claim_id for f in findings}
    edits = [edit for edit in plan.edits if edit.claim_id in claim_ids]
    sections = {s.section_id: s for s in result.sections}
    section = sections.get(findings[0].claim.section_id)
    heading = " / ".join(section.heading_path) if section else ""
    policy = result.config_used.policy
    locations = []
    for f in findings:
        own = [e for e in edits if e.claim_id == f.claim.claim_id and e.line == f.claim.line]
        locations.append(
            IssueLocation(
                claim_id=f.claim.claim_id,
                file=f.claim.doc_path,
                line=f.claim.line,
                verdict=f.verdict.value,
                kind=f.claim.kind.value,
                stale_value=_clean(f.claim.value, 200),
                current_text=_clean(mask_secrets(f.doc_text or f.claim.raw_text), 400),
                correct_value=_clean(f.suggestion, 200) if f.suggestion else None,
                proposed_text=_clean(mask_secrets(p), 400) if (p := _proposed_line(f.doc_text, own)) else None,
                certain_fix=bool(own),
                confidence=round(f.confidence, 4),
                stale_since=f.stale_since,
                age_days=f.age_days,
                fix_by=f.fix_by,
                url=links.blob(f.claim.doc_path, f.claim.line, doc=True),
            )
        )
    evidence: dict[tuple[str, int, str], IssueEvidence] = {}
    for f in findings:
        for item in f.evidence:
            if item.file == f.claim.doc_path and item.line == f.claim.line:
                continue  # The documentation line itself is already shown as "current".
            excerpt = _excerpt(item.text)
            evidence.setdefault(
                (item.file, item.line, excerpt),
                IssueEvidence(file=item.file, line=item.line, excerpt=excerpt, url=links.blob(item.file, item.line)),
            )
    commits = {
        f.introduced_by.sha: IssueCommit(
            sha=f.introduced_by.sha,
            short_sha=f.introduced_by.short_sha,
            summary=_clean(mask_secrets(f.introduced_by.summary), 200),
            # The same UTC landing date that stale_since uses, so one draft never shows two dates.
            date=utc_date(f.introduced_by.landed),
            url=links.commit(f.introduced_by.sha),
        )
        for f in findings
        if f.introduced_by
    }
    patch = None
    if edits:
        try:
            patch = mask_secrets(unified_patch(root, plan.model_copy(update={"edits": edits}))) or None
        except (StaleDocumentError, ValueError, OSError):
            patch = None
    difficulty = _difficulty(locations, edits if patch else [])
    if patch is None and difficulty != "medium":
        difficulty = "medium"
    labels = ["documentation", "docs-drift"]
    if difficulty == "good-first-issue":
        labels.append("good first issue")
    labels.extend(sorted({f"drift: {f.verdict.value.replace('_', ' ')}" for f in findings}))
    if any(age_status(f, policy) == AgeStatus.overdue for f in findings):
        labels.append("overdue")
    verify = [f"docs-drift explain {short[cid]}" for cid in sorted(claim_ids)][:5]
    verify.extend(f"git show --stat {sha}" for sha in sorted({c.short_sha for c in commits.values()})[:3])
    verify.append("docs-drift check --no-color")
    why = " ".join(dict.fromkeys(WHY[f.verdict] for f in findings if f.verdict in WHY))
    issue_id = "issue_" + hashlib.sha256("\n".join(sorted(claim_ids)).encode()).hexdigest()[:16]
    return ContributionIssue(
        issue_id=issue_id,
        title=_title(key[0], findings, heading),
        difficulty=difficulty,
        labels=labels,
        why_it_matters=why,
        grouping="rename" if key[0] == "rename" else "section",
        locations=locations,
        evidence=sorted(evidence.values(), key=lambda e: (e.file, e.line, e.excerpt))[:MAX_EVIDENCE_PER_ISSUE],
        commits=sorted(commits.values(), key=lambda c: (c.date, c.sha)),
        verify=verify,
        patch=patch,
    )


def build_contributions(
    scan: ScanResult, *, min_confidence: float | None = None, limit: int = DEFAULT_LIMIT
) -> ContributionReport:
    """Group eligible drift into deterministic issue drafts with ready patches where a rule fix is certain."""
    result = redact_result(scan)
    threshold = result.config_used.min_confidence if min_confidence is None else min_confidence
    try:
        plan = plan_rule_fixes(scan)
    except (StaleDocumentError, ValueError, OSError):
        plan = FixPlan()
    fixed = {edit.claim_id for edit in plan.edits}
    drift = sorted(
        (f for f in result.findings if f.is_drift),
        key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id),
    )
    chosen = [f for f in drift if eligible(f, threshold, fixed)]
    groups: dict[tuple[str, ...], list[Finding]] = {}
    for finding in chosen:
        groups.setdefault(_group_key(finding), []).append(finding)
    chunks = [
        (key, members[start : start + MAX_LOCATIONS_PER_ISSUE])
        for key, members in groups.items()
        for start in range(0, len(members), MAX_LOCATIONS_PER_ISSUE)
    ]
    links = _Links(result)
    short = _short_ids(result.findings)
    issues = [_issue(key, members, plan=plan, result=result, links=links, short=short) for key, members in chunks]
    issues.sort(key=lambda i: (i.locations[0].file, i.locations[0].line, i.issue_id))
    return ContributionReport(
        head_sha=result.head_sha,
        scan_date=utc_date(result.scan_time),
        remote=links.base,
        min_confidence=threshold,
        total_drift=len(drift),
        eligible_drift=len(chosen),
        skipped_drift=len(drift) - len(chosen),
        truncated=len(issues) > limit,
        available_issues=len(issues),
        available_good_first_issues=sum(i.difficulty == "good-first-issue" for i in issues),
        issues=issues[:limit],
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _link(label: str, url: str | None) -> str:
    text = escape(label)
    return f"[{text}]({url.replace(')', '%29')})" if url else text


def _code(text: str) -> str:
    # Escaped entities render inside <code>, unlike inside Markdown backtick spans.
    return f"<code>{escape(text)}</code>"


def issue_markdown(issue: ContributionIssue) -> str:
    """One self-contained issue body; every repository-derived string is escaped or fenced."""
    difficulty = {"good-first-issue": "good first issue", "easy": "easy", "medium": "needs investigation"}
    lines = [
        f"<!-- docs-drift-issue: {issue.issue_id} -->",
        f"# {escape(issue.title)}",
        "",
        f"**Difficulty:** {difficulty[issue.difficulty]} · **Suggested labels:** "
        + ", ".join(_code(label) for label in issue.labels),
        "",
        "## Why it matters",
        "",
        escape(issue.why_it_matters),
        "",
        "## Where",
        "",
    ]
    for loc in issue.locations:
        age = (
            f" · stale since {loc.stale_since.isoformat()} ({days(loc.age_days)})"
            if loc.stale_since and loc.age_days is not None
            else ""
        )
        due = f" · fix by {loc.fix_by.isoformat()}" if loc.fix_by else ""
        lines.append(
            f"- {_link(f'{loc.file}:{loc.line}', loc.url)} · {escape(loc.verdict)} {_code(loc.stale_value)}"
            + (f" → {_code(loc.correct_value)}" if loc.correct_value else "")
            + f" · confidence {loc.confidence:.0%}{age}{due}"
        )
    lines.extend(["", "## Current vs correct", ""])
    for loc in issue.locations:
        lines.append(f"{escape(loc.file)}:{loc.line}")
        lines.append("")
        if loc.proposed_text is not None:
            lines.append(fence(f"- {loc.current_text}\n+ {loc.proposed_text}", "diff"))
        else:
            hint = (
                f"Replace {loc.stale_value!r} with {loc.correct_value!r} after checking the surrounding text."
                if loc.correct_value
                else f"Remove or rewrite the statement about {loc.stale_value!r}; the code no longer supports it."
            )
            lines.append(fence(f"Current: {loc.current_text}\nSuggested: {hint}", "text"))
        lines.append("")
    if issue.evidence or issue.commits:
        lines.extend(["## Evidence", ""])
        for item in issue.evidence:
            lines.extend([f"- {_link(f'{item.file}:{item.line}', item.url)}", "", fence(item.excerpt, "text"), ""])
        for commit in issue.commits:
            lines.append(
                f"- Introduced by {_link(commit.short_sha, commit.url)} "
                f"({commit.date.isoformat()}): {escape(commit.summary)}"
            )
        lines.append("")
    lines.extend(
        [
            "## How to verify locally",
            "",
            fence("\n".join(issue.verify), "sh"),
            "",
            "After the change, `docs-drift check` should no longer report the locations above.",
            "",
        ]
    )
    if issue.patch:
        lines.extend(
            [
                "## Ready patch",
                "",
                "Generated by `docs-drift fix` from a certain rule edit. Review it, then apply with `git apply`.",
                "",
                fence(issue.patch.rstrip("\n"), "diff"),
                "",
            ]
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def digest_markdown(report: ContributionReport) -> str:
    """All issue drafts in one document, preceded by a short index."""
    head = report.head_sha[:8] if report.head_sha.strip("0") else "unversioned"
    lines = [
        "# Documentation contribution opportunities",
        "",
        f"{_count(len(report.issues), 'issue draft')} from "
        f"{_count(report.eligible_drift, 'well-evidenced drift finding')} "
        f"(minimum confidence {report.min_confidence:.2f}) at `{head}` ({report.scan_date.isoformat()}). "
        f"{_count(report.skipped_drift, 'drift finding')} without a certain fix or history evidence "
        "were left out.",
        "",
    ]
    if report.truncated:
        lines.extend(["More drafts are available; raise `--limit` to include them.", ""])
    for number, issue in enumerate(report.issues, 1):
        lines.append(f"{number}. {escape(issue.title)} · {issue.difficulty}")
    for issue in report.issues:
        body = issue_markdown(issue).replace("\n## ", "\n### ").replace("\n# ", "\n## ", 1)
        lines.extend(["", "---", "", body])
    return "\n".join(lines).rstrip("\n") + "\n"


def issue_filename(number: int, issue: ContributionIssue) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue.title.casefold()).strip("-")[:48].strip("-") or "docs"
    return f"{number:03d}-{slug}-{issue.issue_id[6:14]}.md"


def health_card(scan: ScanResult, report: ContributionReport | None = None) -> HealthCard:
    """Counts for CONTRIBUTING.md. Stale values are never named, so the card cannot become drift itself.

    Every count uses the report's confidence threshold, so a card never mixes two thresholds.
    """
    result = scan
    contributions = report or build_contributions(scan)
    threshold = contributions.min_confidence
    drift = [f for f in result.findings if f.is_drift and f.confidence >= threshold]
    aged = [f for f in drift if f.age_days is not None and f.stale_since is not None]
    oldest = min(aged, key=lambda f: (-(f.age_days or 0), f.claim.doc_path, f.claim.line), default=None)
    docs = Counter(f.claim.doc_path for f in drift)
    return HealthCard(
        head_sha=result.head_sha,
        scan_date=utc_date(result.scan_time),
        health=health(result.findings),
        claims=len(result.claims),
        by_verdict=result.summary()["by_verdict"],
        drift=len(drift),
        documents_with_drift=len(docs),
        stale_documents=[StaleDoc(path=p, drift=n) for p, n in sorted(docs.items(), key=lambda i: (-i[1], i[0]))[:5]],
        oldest_drift=OldestDrift(
            file=oldest.claim.doc_path,
            line=oldest.claim.line,
            verdict=oldest.verdict.value,
            stale_since=oldest.stale_since,
            age_days=oldest.age_days,
        )
        if oldest and oldest.stale_since and oldest.age_days is not None
        else None,
        overdue=sum(age_status(f, result.config_used.policy) == AgeStatus.overdue for f in drift),
        issue_drafts=max(contributions.available_issues, len(contributions.issues)),
        good_first_issues=max(
            contributions.available_good_first_issues,
            sum(i.difficulty == "good-first-issue" for i in contributions.issues),
        ),
    )


def health_card_markdown(card: HealthCard) -> str:
    head = card.head_sha[:8] if card.head_sha.strip("0") else "unversioned"
    lines = [
        CARD_START,
        "### Documentation health",
        "",
        f"**{card.health}/100** · {_count(card.drift, 'drift finding')} in "
        f"{_count(card.documents_with_drift, 'document')}"
        f" · {card.claims} claims checked at {head} ({card.scan_date.isoformat()})",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
        *(f"| {verdict.replace('_', ' ')} | {count} |" for verdict, count in card.by_verdict.items()),
        "",
    ]
    if card.stale_documents:
        lines.extend(["Most stale documents:", ""])
        lines.extend(f"- {escape(doc.path)} ({doc.drift})" for doc in card.stale_documents)
        lines.append("")
    if card.oldest_drift:
        old = card.oldest_drift
        lines.extend(
            [
                f"Oldest drift: {escape(old.file)} line {old.line} ({old.verdict.replace('_', ' ')}), "
                f"stale since {old.stale_since.isoformat()} ({days(old.age_days)}).",
                "",
            ]
        )
    if card.overdue:
        lines.extend([f"Overdue under the drift age policy: {card.overdue}.", ""])
    lines.extend(
        [
            f"Issue drafts ready: {card.issue_drafts}, of which {card.good_first_issues} are good first issues. "
            "Generate them with docs-drift contribute.",
            CARD_END,
        ]
    )
    return "\n".join(lines) + "\n"
