"""Certain replacements only, within the scanner's original literal spans."""

import hashlib
import re
from decimal import Decimal
from pathlib import Path

from docs_drift.docs.defaults import normalize_value
from docs_drift.docs.sections import is_historical_span
from docs_drift.fix.edits import FixPlan, StaleDocumentError, TextEdit, decode, safe_path
from docs_drift.history import GitHistory, _run
from docs_drift.models import Claim, ClaimKind, Finding, ScanResult, Verdict

_WORD = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")
MIN_RENAME_FIX_CONFIDENCE = 0.9
_UNITS = {
    "ms": Decimal(".001"),
    "s": Decimal(1),
    "sec": Decimal(1),
    "second": Decimal(1),
    "seconds": Decimal(1),
    "m": Decimal(60),
    "min": Decimal(60),
    "minute": Decimal(60),
    "minutes": Decimal(60),
    "h": Decimal(3600),
    "hour": Decimal(3600),
    "hours": Decimal(3600),
    "b": Decimal(1),
    "kb": Decimal(1000),
    "mb": Decimal(1000000),
    "gb": Decimal(1000000000),
    "kib": Decimal(1024),
    "mib": Decimal(1048576),
    "gib": Decimal(1073741824),
}
_UNIT_VALUE = re.compile(
    r"(?<!\w)(-?\d+(?:\.\d+)?)([\s`\"']*)(milliseconds?|seconds?|minutes?|hours?|MiB|GiB|KiB|MB|GB|KB|ms|sec|min|s|m|h|B)\b",
    re.I,
)


def _edit(claim: Claim, line: str, old: str, new: str, reason: str) -> TextEdit | None:
    # Never fall back to a file-wide substitution: a unique exact span on this line is required.
    matches = list(re.finditer(rf"(?<![\w-]){re.escape(old)}(?![\w-])", line))
    if len(matches) != 1 or old == new:
        return None
    match = matches[0]
    return TextEdit(
        file=claim.doc_path,
        line=claim.line,
        column=match.start() + 1,
        end_line=claim.line,
        end_column=match.end() + 1,
        old_text=old,
        new_text=new,
        reason=reason,
        claim_id=claim.claim_id,
    )


def _default_edit(finding: Finding, line: str) -> TextEdit | None:
    expected = finding.claim.expected_default
    actual = finding.matched_symbol.default if finding.matched_symbol else finding.suggestion
    if expected is None or actual is None:
        return None
    old = expected.strip("`\"'")
    new = actual.strip("`\"'")
    expected_value = normalize_value(expected, subject=finding.claim.value)
    actual_value = normalize_value(actual, subject=finding.claim.value)
    if expected_value.kind in {"duration", "size"}:
        if actual_value.kind != expected_value.kind:
            return None
        unit_matches = [
            match
            for match in _UNIT_VALUE.finditer(line)
            if normalize_value(match.group(1) + match.group(3), subject=finding.claim.value).equals(expected_value)
            is True
        ]
        if len(unit_matches) != 1:
            return None
        match = unit_matches[0]
        factor = _UNITS.get(match.group(3).casefold())
        if factor is None:
            return None
        old = match.group(1)
        new = format(Decimal(actual_value.value) / factor, "f").rstrip("0").rstrip(".")
        if "." not in format(Decimal(actual_value.value) / factor, "f"):
            new = str(int(Decimal(actual_value.value) / factor))
        return TextEdit(
            file=finding.claim.doc_path,
            line=finding.claim.line,
            column=match.start(1) + 1,
            end_line=finding.claim.line,
            end_column=match.end(1) + 1,
            old_text=old,
            new_text=new,
            reason="Update the default while preserving its documented unit.",
            claim_id=finding.claim.claim_id,
        )
    # The verdict already established comparable defaults. Keep the documented unit and quote syntax.
    number = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:s|B)?", old)
    if number:
        old = number.group(1)
    return _edit(finding.claim, line, old, new, "Update the documented default.")


def _removed_flag_shape(repo: Path, flag: str) -> tuple[bool, str] | None:
    history = GitHistory(repo).search(flag)
    if not history.commit:
        return None
    patch = _run(repo, ["show", "--no-ext-diff", "--format=", history.commit.sha])
    if patch is None:
        return None
    removed = [line[1:] for line in patch.stdout.splitlines() if line.startswith("-") and flag in line]
    declarations = [line for line in removed if re.search(r"(?:add_argument|\.option|\.Option)\(", line)]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    boolean = "store_true" in declaration or "store_false" in declaration
    if ".option(" in declaration and "add_argument" not in declaration:
        flag_string = re.search(r"\.option\(\s*['\"]([^'\"]+)", declaration)
        boolean = flag_string is not None and not re.search(r"[<\[]", flag_string.group(1))
    return boolean, history.commit.short_sha


def _command_edit(repo: Path, finding: Finding, line: str) -> tuple[TextEdit | None, str | None]:
    unknown = set(re.findall(r"--[\w-]+", " ".join(finding.reasons)))
    if len(unknown) != 1:
        return None, None
    flag = next(iter(unknown))
    shape = _removed_flag_shape(repo, flag)
    if shape is None:
        return None, None
    boolean, sha = shape
    tokens = list(_WORD.finditer(line))
    flags = [index for index, token in enumerate(tokens) if token.group().split("=", 1)[0] == flag]
    if len(flags) != 1:
        return None, None
    index = flags[0]
    start, end = tokens[index].span()
    if not boolean and "=" not in tokens[index].group():
        if index + 1 == len(tokens) or tokens[index + 1].group().startswith("-"):
            return None, None
        end = tokens[index + 1].end()
    if end < len(line) and line[end] == " ":
        end += 1
    elif start and line[start - 1] == " ":
        start -= 1
    edit = TextEdit(
        file=finding.claim.doc_path,
        line=finding.claim.line,
        column=start + 1,
        end_line=finding.claim.line,
        end_column=end + 1,
        old_text=line[start:end],
        new_text="",
        reason=f"Remove {flag}, deleted in {sha}.",
        claim_id=finding.claim.claim_id,
    )
    note = f"{flag} was removed in {sha}; check whether this example should change more than this."
    return edit, note


def plan_rule_fixes(result: ScanResult) -> FixPlan:  # noqa: PLR0912 - explicit independent edit safety gates.
    plan = FixPlan(hashes={doc.path: doc.content_hash for doc in result.docs})
    texts: dict[str, list[str]] = {}
    for doc in result.docs:
        raw = safe_path(result.repo_path, doc.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != doc.content_hash:
            raise StaleDocumentError(f"{doc.path} changed since scan; scan again.")
        try:
            texts[doc.path] = decode(raw)[0].splitlines()
        except UnicodeError:
            plan.suggestions.append(f"{doc.path}: encoding is not safely editable; review manually.")
    for finding in result.findings:
        if not finding.is_drift or finding.claim.doc_path not in texts:
            continue
        claim = finding.claim
        line = texts[claim.doc_path][claim.line - 1]
        edit = None
        if (
            finding.verdict == Verdict.renamed
            and finding.suggestion
            and finding.confidence >= MIN_RENAME_FIX_CONFIDENCE
        ):
            replacement = finding.suggestion
            old = claim.value
            if claim.kind == ClaimKind.endpoint:
                old = claim.raw_text.strip("`").split(maxsplit=1)[-1]
                replacement = (
                    finding.matched_symbol.raw_path
                    if finding.matched_symbol and finding.matched_symbol.raw_path
                    else replacement.split(maxsplit=1)[-1]
                )
            edit = _edit(claim, line, old, replacement, "Replace a confirmed renamed identifier.")
        elif finding.verdict == Verdict.default_changed:
            edit = _default_edit(finding, line)
        elif finding.verdict == Verdict.invalid_command:
            edit, note = _command_edit(result.repo_path, finding, line)
            if note:
                plan.suggestions.append(note)
        if edit:
            section = next((item for item in result.sections if item.section_id == claim.section_id), None)
            if section and is_historical_span(section, claim.line):
                continue
            plan.edits.append(edit)
            # Same-section duplicates retain exact locations; historical locations are excluded explicitly.
            for location in claim.locations:
                if location.line == claim.line or (section and is_historical_span(section, location.line)):
                    continue
                duplicate = _edit(
                    claim.model_copy(update={"line": location.line}),
                    texts[claim.doc_path][location.line - 1],
                    edit.old_text,
                    edit.new_text,
                    edit.reason,
                )
                if duplicate:
                    plan.edits.append(duplicate)
        else:
            plan.suggestions.append(
                f"{claim.doc_path}:{claim.line}: no certain rule edit for {claim.value!r}; "
                "remove the stale statement or mark it deprecated after review."
            )
    unique = {(edit.file, edit.line, edit.column, edit.end_column, edit.new_text): edit for edit in plan.edits}
    plan.edits = list(unique.values())
    return plan
