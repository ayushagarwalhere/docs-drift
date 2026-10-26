"""Contributor drafts from a scan: a patch file, a pull request description and issue texts.

Every repository-controlled value (paths, names, commit summaries, documentation text)
is placed inside Markdown code spans or fenced blocks, so a draft cannot inject links,
images, mentions or HTML into a pull request. Nothing here claims a change is correct:
the drafts leave verification to the person who opens the pull request or issue.
"""

import re
from urllib.parse import quote

from docs_drift.fix.bundle import FixBundle, FixStatus
from docs_drift.limits import MAX_ISSUE_DRAFTS
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import Finding, ScanResult, Verdict
from docs_drift.report.html import github_remote

PROJECT_URL = "https://github.com/ayushagarwalhere/docs-drift"
REVIEW_NOTICE = "Review every change before opening a PR."
MAX_PR_DIFF_CHARS = 40_000
MAX_LISTED = 20
MAX_EVIDENCE_ROWS = 4
_CONTROL = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]")


def _clean(text: str, limit: int) -> str:
    text = _CONTROL.sub(" ", mask_secrets(text).replace("\r", " ").replace("\n", " ")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def code(text: str, limit: int = 160) -> str:
    """An inline code span that no backtick sequence in `text` can close early."""
    value = _clean(text, limit) or " "
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if value.startswith("`") or value.endswith("`") or longest else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def fenced(text: str, language: str = "text") -> str:
    """A fenced block longer than any backtick or tilde run inside the content."""
    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=2)
    fence = "`" * (longest + 1)
    body = text if text.endswith("\n") else text + "\n"
    return f"{fence}{language}\n{body}{fence}"


def _link(label: str, url: str | None) -> str:
    return f"[{code(label)}]({url})" if url else code(label)


def remote_for(url: str | None) -> str | None:
    """The canonical https://github.com/owner/repo for links, or None when there is no GitHub remote."""
    return github_remote(url)


def blob_url(remote: str | None, sha: str, path: str, line: int | None = None) -> str | None:
    if not remote:
        return None
    anchor = f"#L{line}" if line else ""
    return f"{remote}/blob/{sha}/{quote(path, safe='/')}{anchor}"


def commit_url(remote: str | None, sha: str) -> str | None:
    return f"{remote}/commit/{sha}" if remote else None


def repository_name(remote: str | None) -> str:
    return remote.removeprefix("https://github.com/") if remote else "this repository"


def apply_commands(remote: str | None, sha: str, *, checkout: str | None = None) -> list[str]:
    """Clone (or enter) the scanned revision, branch from it and apply the downloaded patch."""
    if checkout:
        first = checkout
    elif remote:
        first = f"git clone {remote}.git && cd {remote.rsplit('/', 1)[-1]}"
    else:
        first = "cd path/to/your/checkout"
    return [first, f"git checkout -b docs-drift-fixes {sha}", "git apply fix.patch"]


def patch_document(bundle: FixBundle, remote: str | None) -> str:
    """The bundle's diff with a short preamble; `git apply` ignores text before the first hunk header."""
    preamble = [
        f"docs-drift documentation fixes for {repository_name(remote)}",
        f"Base commit: {bundle.head_sha}",
        "Apply on a checkout of that commit: git apply fix.patch",
        "Only certain, exact edits are included. " + REVIEW_NOTICE,
        "",
    ]
    return "\n".join(preamble) + "\n" + bundle.patch


def _actual_default(finding: Finding) -> str | None:
    if finding.matched_symbol and finding.matched_symbol.default is not None:
        return finding.matched_symbol.default
    return finding.suggestion


def problem(finding: Finding) -> str:
    """One sentence describing the drift; untrusted values are code spans."""
    value = code(finding.claim.value)
    if finding.verdict == Verdict.renamed and finding.suggestion:
        return f"documents {value}, which the code now calls {code(finding.suggestion)}."
    if finding.verdict == Verdict.default_changed:
        expected = code(finding.claim.expected_default or "?")
        actual = _actual_default(finding)
        current = f"the code default is {code(actual)}" if actual else "the code default differs"
        return f"says {value} defaults to {expected}, but {current}."
    if finding.verdict == Verdict.invalid_command:
        return f"shows {code(finding.claim.raw_text)}, which the current command-line interface does not accept."
    return f"documents {value}, which the code no longer declares."


def doc_link(finding: Finding, remote: str | None, sha: str) -> str:
    claim = finding.claim
    return _link(f"{claim.doc_path}:{claim.line}", blob_url(remote, sha, claim.doc_path, claim.line))


def _evidence_lines(finding: Finding, remote: str | None, sha: str) -> list[str]:
    """Source declaration, other evidence files and the commit; the documentation line is linked by the caller."""
    rows: list[str] = []
    symbol = finding.matched_symbol
    if symbol:
        rows.append(
            f"  - Code: {_link(f'{symbol.file}:{symbol.line}', blob_url(remote, sha, symbol.file, symbol.line))}"
            f" declares {code(symbol.name)}"
        )
    seen = {finding.claim.doc_path} | ({symbol.file} if symbol else set())
    for item in finding.evidence:
        if item.file in seen or len(rows) >= MAX_EVIDENCE_ROWS:
            continue
        seen.add(item.file)
        rows.append(f"  - Evidence: {_link(f'{item.file}:{item.line}', blob_url(remote, sha, item.file, item.line))}")
    commit = finding.introduced_by
    if commit:
        rows.append(
            f"  - Changed in {_link(commit.short_sha, commit_url(remote, commit.sha))}: {code(commit.summary, 100)}"
        )
    return rows


def _drift(result: ScanResult) -> list[Finding]:
    return sorted(
        (finding for finding in result.findings if finding.is_drift),
        key=lambda f: (f.claim.doc_path, f.claim.line, f.claim.claim_id),
    )


def _footer(project_url: str) -> str:
    return f"Found with docs-drift ({project_url}), verified by …"


def pull_request_title(bundle: FixBundle) -> str:
    fixed = bundle.count(FixStatus.patched)
    noun = "reference" if fixed == 1 else "references"
    if len(bundle.files) == 1:
        return _clean(f"docs: fix {fixed} stale {noun} in {bundle.files[0].path}", 120)
    return f"docs: fix {fixed} stale {noun} in {len(bundle.files)} files"


def render_pull_request(
    result: ScanResult, bundle: FixBundle, *, remote: str | None = None, project_url: str = PROJECT_URL
) -> str:
    """First line: suggested title. The rest: a Markdown pull request description."""
    sha = bundle.head_sha
    statuses = bundle.status()
    findings = _drift(result)
    patched = [f for f in findings if statuses.get(f.claim.claim_id) == FixStatus.patched]
    other = [f for f in findings if statuses.get(f.claim.claim_id) != FixStatus.patched]
    lines = [
        pull_request_title(bundle),
        "",
        "<!--",
        "Suggested by docs-drift. The first line is the PR title; everything below is the description.",
        REVIEW_NOTICE,
        "Check each statement against the code yourself, then replace … in the last line with your name.",
        "-->",
        "",
        "## What's wrong",
        "",
        f"At {_link(sha[:12], commit_url(remote, sha))}, the documentation describes code that has since changed:",
        "",
    ]
    for finding in patched[:MAX_LISTED]:
        lines.append(f"- {doc_link(finding, remote, sha)} {problem(finding)}")
        lines.extend(_evidence_lines(finding, remote, sha))
    if len(patched) > MAX_LISTED:
        lines.append(f"- …and {len(patched) - MAX_LISTED} more edits in the diff below.")
    lines += ["", "## The change", ""]
    lines.append(
        f"{len(bundle.files)} file{'s' if len(bundle.files) != 1 else ''}, "
        "exact replacements only (no rewording). Generated by `docs-drift fix` rules:"
    )
    lines.append("")
    if len(bundle.patch) <= MAX_PR_DIFF_CHARS:
        lines.append(fenced(bundle.patch, "diff"))
    else:
        lines.append("The diff is too long to include here; it is attached as `fix.patch`.")
    if other:
        lines += ["", "## Not changed", "", "These findings need a human decision and are not part of this PR:", ""]
        for finding in other[:MAX_LISTED]:
            lines.append(f"- {doc_link(finding, remote, sha)} {problem(finding)}")
        if len(other) > MAX_LISTED:
            lines.append(f"- …and {len(other) - MAX_LISTED} more.")
    if bundle.notes:
        lines += ["", "Notes for reviewers:", ""]
        lines.extend(f"- {code(note, 240)}" for note in bundle.notes[:MAX_LISTED])
    lines += ["", "---", "", _footer(project_url), ""]
    return "\n".join(lines)


def issue_title(finding: Finding) -> str:
    claim = finding.claim
    where = f"{claim.doc_path}:{claim.line}"
    value = _clean(claim.value, 60)
    if finding.verdict == Verdict.renamed and finding.suggestion:
        return _clean(f"docs: {where} still says {value}, renamed to {finding.suggestion}", 140)
    if finding.verdict == Verdict.default_changed:
        return _clean(f"docs: {where} documents an outdated default for {value}", 140)
    if finding.verdict == Verdict.invalid_command:
        return _clean(f"docs: {where} shows a command that no longer works", 140)
    return _clean(f"docs: {where} mentions {value}, which no longer exists", 140)


def render_issue_drafts(
    result: ScanResult,
    bundle: FixBundle | None = None,
    *,
    remote: str | None = None,
    project_url: str = PROJECT_URL,
) -> str:
    """One contributor-friendly issue draft per stale statement, in document order."""
    sha = result.head_sha
    statuses = bundle.status() if bundle else {}
    findings = _drift(result)
    lines = [
        f"# Documentation issue drafts for {code(repository_name(remote))}",
        "",
        "<!--",
        "Drafted by docs-drift. Review every finding before filing, search the tracker for an existing issue,",
        "and replace … in each footer with your name once you have checked it.",
        "-->",
        "",
        f"{len(findings)} stale statement{'s' if len(findings) != 1 else ''} at "
        f"{_link(sha[:12], commit_url(remote, sha))}. Each section below is one issue: copy its title and body.",
    ]
    for number, finding in enumerate(findings[:MAX_ISSUE_DRAFTS], start=1):
        claim = finding.claim
        status = statuses.get(claim.claim_id)
        lines += ["", "---", "", f"## Issue {number}", "", "**Title**", "", fenced(issue_title(finding)), ""]
        lines += ["**Body**", "", f"The documentation at {doc_link(finding, remote, sha)} says:", ""]
        lines.append(fenced(mask_secrets((finding.doc_text or claim.raw_text)[:600])))
        lines += ["", f"It {problem(finding)}", ""]
        evidence = [row.removeprefix("  ") for row in _evidence_lines(finding, remote, sha)]
        if evidence:
            lines += ["Evidence:", "", *evidence, ""]
        actual = _actual_default(finding)
        if status == FixStatus.patched and finding.verdict == Verdict.renamed and finding.suggestion:
            lines.append(
                f"**Suggested fix:** replace {code(claim.value)} with {code(finding.suggestion)} "
                "on that line. This is a small, self-contained change, a good first contribution."
            )
        elif status == FixStatus.patched and finding.verdict == Verdict.default_changed and actual:
            lines.append(
                f"**Suggested fix:** change the documented default to {code(actual)}. "
                "This is a small, self-contained change, a good first contribution."
            )
        elif status == FixStatus.patched:
            lines.append(
                "**Suggested fix:** an exact one-line edit is available in docs-drift's `fix.patch`. "
                "This is a small, self-contained change, a good first contribution."
            )
        else:
            lines.append(
                "**Suggested fix:** decide whether to update, remove or mark this statement as deprecated; "
                "there is no certain mechanical replacement."
            )
        lines += ["", _footer(project_url)]
    if len(findings) > MAX_ISSUE_DRAFTS:
        lines += ["", "---", "", f"{len(findings) - MAX_ISSUE_DRAFTS} more findings are in the JSON report."]
    lines.append("")
    return "\n".join(lines)
