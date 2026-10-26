"""Check documented dependency and runtime claims against parsed manifests (rules only, no network)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version
from rapidfuzz import fuzz, process

from docs_drift.config import DepsConfig
from docs_drift.deps.claims import NPM, PROJECT, PYPI, install_commands
from docs_drift.deps.manifests import Declared, Ecosystem, Manifest, ManifestSet, Runtime, node_key, python_key
from docs_drift.deps.names import python_non_imported
from docs_drift.deps.releases import ReleaseLabel, prose_label, table_header, table_label
from docs_drift.deps.specs import (
    format_tuple,
    npm_contains,
    npm_minimum,
    npm_overlap,
    npm_range,
    parse_specifier,
    python_minimum,
    specifiers_overlap,
)
from docs_drift.models import Claim, ClaimKind, ClaimLocation, Evidence, Extraction, Finding, Verdict
from docs_drift.safe_files import metadata_text

DEPENDENCY_KINDS = frozenset({ClaimKind.dependency, ClaimKind.runtime_version})
_TOOLING = {"pip", "setuptools", "wheel", "uv", "pipx", "poetry", "hatch", "pdm", "npm", "pnpm", "yarn", "corepack"}
MIN_SUGGESTION_SCORE = 60
MAX_LISTED = 8
_DATED_DIRS = {"blog", "blogs", "news", "posts", "announcements", "releases", "release-notes"}


@dataclass
class _Context:
    root: Path
    manifests: ManifestSet
    config: DepsConfig
    lines: dict[str, list[str]] = field(default_factory=dict)
    _projects: dict[str, str | None] | None = None

    def source(self, path: str) -> list[str]:
        if path not in self.lines:
            try:
                self.lines[path] = metadata_text(self.root / path).splitlines()
            except (OSError, UnicodeError):
                self.lines[path] = []
        return self.lines[path]

    def text(self, path: str, line: int) -> str:
        lines = self.source(path)
        return lines[line - 1].strip() if 0 < line <= len(lines) else path

    def projects(self) -> dict[str, str | None]:
        """Every project name the manifests declare, with its declared version (None when not literal)."""
        if self._projects is None:
            self._projects = {}
            for manifest in self.manifests.manifests:
                if manifest.project_name and manifest.project_name not in self._projects:
                    self._projects[manifest.project_name] = manifest.version
        return self._projects

    def evidence(self, path: str, line: int) -> Evidence:
        return Evidence(file=path, line=max(1, line), text=self.text(path, line))


def _finding(  # noqa: PLR0913, PLR0917 - one finding's facts.
    claim: Claim,
    verdict: Verdict,
    evidence: list[Evidence],
    reasons: list[str],
    confidence: float,
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        claim=claim,
        verdict=verdict,
        evidence=evidence,
        reasons=reasons,
        confidence=confidence,
        suggestion=suggestion,
    )


def _runtime(context: _Context, claim: Claim) -> Finding | None:  # noqa: PLR0911 - one exit per outcome.
    family: Ecosystem = "python" if claim.value.startswith("python") else "node"
    chain = context.manifests.for_path(claim.doc_path, family)
    requirements = [r for m in chain for r in m.runtime]
    selected: Runtime | None = next((r for r in requirements if r.authoritative), None) or next(
        iter(requirements), None
    )
    if selected is None:
        return None
    documented = claim.value.split(">=", 1)[1]
    precision = len(documented.split("."))
    label = "Python" if family == "python" else "Node.js"
    if family == "python":
        specifier = parse_specifier(selected.spec)
        minimum = python_minimum(specifier) if specifier is not None else None
        if minimum is None:
            return None
        # Compare at the documented precision: "3.8+" matches ">=3.8.1"; "3.8.0+" does not.
        doc_key = (*Version(documented).release, 0, 0)[:precision]
        min_key = (*minimum.release, 0, 0)[:precision]
        minimum_text = str(minimum)
    else:
        intervals = npm_range(selected.spec)
        lowest = npm_minimum(intervals) if intervals else None
        if lowest is None:
            return None
        parts = [int(p) for p in documented.split(".")]
        doc_key = tuple(parts)
        min_key = lowest[:precision]
        minimum_text = format_tuple(lowest, precision)
    evidence = [context.evidence(selected.manifest, selected.line)]
    source = f"{selected.manifest} declares {selected.spec!r}"
    strength = 1.0 if selected.authoritative else 0.6
    if doc_key < min_key:
        return _finding(
            claim,
            Verdict.version_mismatch,
            evidence,
            [
                f"Documentation says {label} {documented}+ works, but {source}; the oldest admitted release is "
                f"{minimum_text}.",
            ],
            round(0.9 * strength, 3),
            minimum_text,
        )
    if doc_key > min_key:
        reason = (
            f"Documentation understates support: it asks for {label} {documented}+ while {source} "
            f"(oldest admitted release {minimum_text})."
        )
        if context.config.report_understated:
            return _finding(claim, Verdict.version_mismatch, evidence, [reason], round(0.6 * strength, 3), minimum_text)
        return _finding(claim, Verdict.ok, evidence, [reason], round(0.7 * strength, 3))
    return _finding(claim, Verdict.ok, evidence, [f"Matches {source}."], round(0.95 * strength, 3))


def _extras(text: str) -> list[str]:
    match = re.search(r"\[([^\]]*)\]", text)
    return [python_key(item) for item in match[1].split(",") if item.strip()] if match else []


def _extra_finding(context: _Context, claim: Claim, manifest: Manifest, extras: list[str]) -> Finding | None:
    if not manifest.extras_known:
        return None
    header = next((d.line for d in manifest.dependencies if d.extra), 1)
    evidence = [context.evidence(manifest.path, header)]
    unknown = [extra for extra in extras if extra not in manifest.extras]
    available = ", ".join(sorted(manifest.extras)) or "none"
    if not unknown:
        return _finding(claim, Verdict.ok, evidence, [f"{manifest.path} defines extra(s) {', '.join(extras)}."], 0.95)
    suggestion = None
    if manifest.extras:
        best = process.extractOne(
            unknown[0], sorted(manifest.extras), scorer=fuzz.ratio, score_cutoff=MIN_SUGGESTION_SCORE
        )
        if best:
            suggestion = str(best[0])
    return _finding(
        claim,
        Verdict.unknown_extra,
        evidence,
        [f"{manifest.path} defines no extra named {', '.join(unknown)}; available extras: {available}."],
        0.9,
        suggestion,
    )


def _declared(context: _Context, ecosystem: Ecosystem, key: str) -> list[Declared]:
    return [d for d in context.manifests.declared(ecosystem) if d.key == key and d.group != "build"]


def _self_manifest(context: _Context, claim: Claim, ecosystem: Ecosystem, key: str | None) -> Manifest | None:
    keys = python_key if ecosystem == "python" else node_key
    chain = context.manifests.for_path(claim.doc_path, ecosystem)
    if key is None:
        # "." is the nearest installable project; when its extras cannot be read, nothing is decided.
        nearest = next((m for m in chain if m.package), None)
        return nearest if nearest is not None and nearest.extras_known else None
    candidates = [m for m in chain if m.project_name and keys(m.project_name) == key]
    candidates.extend(
        m
        for m in context.manifests.manifests
        if m.ecosystem == ecosystem and m.project_name and keys(m.project_name) == key
    )
    return candidates[0] if candidates else None


def _missing(  # noqa: PLR0913, PLR0917 - sibling context for an undeclared package.
    context: _Context, claim: Claim, ecosystem: Ecosystem, key: str, name: str, siblings: list[str]
) -> Finding | None:
    if claim.qualifier and claim.qualifier.endswith(":global"):
        return None
    if key in _TOOLING or (ecosystem == "python" and python_non_imported(key)):
        return None
    # Only a command that otherwise installs declared dependencies of the project suggests a removal:
    # "pip install demo matplotlib" (the project plus an example library) or one naming several
    # undeclared libraries is a tutorial, not a stale dependency list.
    others = [
        s
        for s in siblings
        if s not in {key, PROJECT}
        and not _is_self(context, ecosystem, s)
        and s not in _TOOLING
        and not (ecosystem == "python" and python_non_imported(s))
    ]
    known = [s for s in others if _declared(context, ecosystem, s)]
    manifests = [m for m in context.manifests.manifests if m.ecosystem == ecosystem and m.dependencies]
    if not known or len(known) != len(others) or not manifests:
        return None
    listed = ", ".join(sorted(set(known))[:MAX_LISTED])
    return _finding(
        claim,
        Verdict.missing,
        [context.evidence(m.path, 1) for m in manifests[:3]],
        [
            f"The same install command names declared package(s) ({listed}), but no manifest declares {name}; "
            "the dependency may have been removed or never declared.",
        ],
        0.7,
    )


def _is_self(context: _Context, ecosystem: Ecosystem, key: str) -> bool:
    return key in context.manifests.project_names(ecosystem)


def _siblings(claim: Claim, ecosystem: Ecosystem) -> list[str]:
    result = []
    registry = NPM if ecosystem == "node" else PYPI
    for command in install_commands(claim.raw_text):
        if command.registry != registry:
            continue
        for requirement in command.requirements:
            parsed = _split(requirement, ecosystem)
            if parsed is not None:
                result.append(parsed[1])
    return result


def _split(value: str, ecosystem: Ecosystem) -> tuple[str, str, str, list[str]] | None:
    """(name, key, spec, extras) for one documented requirement."""
    if value.startswith(PROJECT):
        return PROJECT, PROJECT, "", _extras(value)
    if ecosystem == "python":
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            return None
        return requirement.name, python_key(requirement.name), str(requirement.specifier), sorted(requirement.extras)
    at = value.rfind("@")
    name, spec = (value[:at], value[at + 1 :]) if at > 0 else (value, "")
    if not re.fullmatch(r"(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*", name, re.I):
        return None
    return name, node_key(name), spec, []


def _spec_compatible(ecosystem: Ecosystem, documented: str, declared: str) -> bool | None:
    if not documented or not declared:
        return True
    if ecosystem == "python":
        first, second = parse_specifier(documented), parse_specifier(declared)
        return None if first is None or second is None else specifiers_overlap(first, second)
    left, right = npm_range(documented), npm_range(declared)
    return None if left is None or right is None else npm_overlap(left, right)


def _version_matches(ecosystem: Ecosystem, documented: str, version: str) -> bool | None:
    if ecosystem == "python":
        specifier = parse_specifier(documented)
        try:
            return None if specifier is None else specifier.contains(Version(version), prereleases=True)
        except InvalidVersion:
            return None
    intervals = npm_range(documented)
    return None if intervals is None else npm_contains(intervals, version)


def _released(ecosystem: Ecosystem, version: str) -> bool:
    """False for development, pre-release, local and placeholder versions, which name no published release."""
    if ecosystem == "python":
        try:
            parsed = Version(version)
        except InvalidVersion:
            return False
        return not (parsed.is_prerelease or parsed.is_devrelease or parsed.local) and any(parsed.release)
    return re.fullmatch(r"v?\d+\.\d+\.\d+", version.strip()) is not None and version.strip("v") != "0.0.0"


def _only_newer(ecosystem: Ecosystem, documented: str, version: str) -> bool:
    """True when every version the documentation admits is newer than the manifest version."""
    older = f"<={version}" if ecosystem == "python" else f"<={version.strip().removeprefix('v')}"
    return _spec_compatible(ecosystem, documented, older) is False


def _dependency(context: _Context, claim: Claim) -> Finding | None:  # noqa: PLR0911 - one exit per decision.
    ecosystem: Ecosystem = "node" if (claim.qualifier or "").startswith(NPM) else "python"
    parsed = _split(claim.value, ecosystem)
    if parsed is None:
        return None
    name, key, spec, extras = parsed
    if key == PROJECT:
        manifest = _self_manifest(context, claim, "python", None)
        return _extra_finding(context, claim, manifest, extras) if manifest and extras else None
    if _is_self(context, ecosystem, key):
        manifest = _self_manifest(context, claim, ecosystem, key)
        if manifest is None:
            return None
        if extras and ecosystem == "python":
            return _extra_finding(context, claim, manifest, extras)
        # Older pins are often intentional (and a just-bumped manifest is ahead of the last release),
        # so only documentation that asks for a version newer than the manifest declares is reported.
        if (
            spec
            and manifest.version
            and _released(ecosystem, manifest.version)
            and _version_matches(ecosystem, spec, manifest.version) is False
            and _only_newer(ecosystem, spec, manifest.version)
        ):
            return _finding(
                claim,
                Verdict.version_mismatch,
                [context.evidence(manifest.path, 1)],
                [
                    f"Documentation installs {name} {spec}, but {manifest.path} declares version "
                    f"{manifest.version}; no such release exists yet.",
                ],
                0.65,
                manifest.version,
            )
        return None
    declarations = _declared(context, ecosystem, key)
    if not declarations:
        return _missing(context, claim, ecosystem, key, name, _siblings(claim, ecosystem))
    evidence = [context.evidence(d.manifest, d.line) for d in declarations[:3]]
    if not spec:
        return _finding(claim, Verdict.ok, evidence, [f"{name} is declared in {declarations[0].manifest}."], 0.9)
    results = [_spec_compatible(ecosystem, spec, d.spec) for d in declarations]
    if any(result is True for result in results):
        return _finding(
            claim, Verdict.ok, evidence, [f"{name} {spec} is compatible with the declared requirement."], 0.9
        )
    if all(result is None for result in results):
        return None
    declared = next(d for d, result in zip(declarations, results, strict=True) if result is False)
    return _finding(
        claim,
        Verdict.version_mismatch,
        evidence,
        [
            f"Documentation installs {name} {spec}, but {declared.manifest} requires {name} {declared.spec}; "
            "no version satisfies both.",
        ],
        0.8,
        declared.spec,
    )


def _release_label(context: _Context, path: str, location: ClaimLocation) -> ReleaseLabel | None:
    lines = context.source(path)
    if not 0 < location.line <= len(lines):
        return None
    row = lines[location.line - 1]
    if location.extraction == Extraction.table or row.lstrip().startswith("|"):
        return table_label(row, location.raw_text, table_header(lines, location.line - 1), context.projects())
    return prose_label(row, location.raw_text, context.projects())


def _current_release(context: _Context, claim: Claim) -> Claim | None:
    """Drop locations that describe another release of the project ("1.x | Python 2.7+")."""
    locations = claim.locations or [
        ClaimLocation(line=claim.line, column=claim.column, raw_text=claim.raw_text, extraction=claim.extraction)
    ]
    declared = next((m.version for m in context.manifests.for_path(claim.doc_path) if m.version), None)
    kept = []
    for location in locations:
        label = _release_label(context, claim.doc_path, location)
        version = context.projects().get(label.project, declared) if label and label.project else declared
        if label is None or label.covers(version):
            kept.append(location)
    if not kept:
        return None
    if len(kept) == len(locations):
        return claim
    first = kept[0]
    update = {"locations": kept, "line": first.line, "column": first.column, "raw_text": first.raw_text}
    return claim.model_copy(update={**update, "extraction": first.extraction})


def _newest_runtime_per_section(claims: list[Claim]) -> list[Claim]:
    """Several minimums for one runtime in one section or table describe releases or platforms side by side.

    Only the newest is checked, so older, historically correct rows are never flagged or rewritten.
    """

    def parts(claim: Claim) -> tuple[tuple[str, str, str], tuple[int, ...]]:
        family, version = claim.value.split(">=", 1)
        return (claim.doc_path, claim.section_id, family), tuple(int(number) for number in version.split("."))

    newest: dict[tuple[str, str, str], tuple[int, ...]] = {}
    for claim in claims:
        key, value = parts(claim)
        newest[key] = max(newest.get(key, value), value)
    return [claim for claim in claims if parts(claim)[1] == newest[parts(claim)[0]]]


def _dated_post(path: str) -> bool:
    """Blog posts and release announcements describe requirements as they were on publication."""
    pure = PurePosixPath(path)
    return bool({part.casefold() for part in pure.parts[:-1]} & _DATED_DIRS) or pure.stem.casefold().startswith(
        ("announcing", "release-notes")
    )


def check_dependency_claims(
    claims: list[Claim], manifests: ManifestSet, root: Path, config: DepsConfig
) -> list[Finding]:
    """Return findings only for claims the manifests can decide; others are not reported."""
    context = _Context(root, manifests, config)
    current = [
        checked
        for claim in claims
        if claim.kind == ClaimKind.runtime_version and not _dated_post(claim.doc_path)
        for checked in [_current_release(context, claim)]
        if checked is not None
    ]
    runtimes = {claim.claim_id: claim for claim in _newest_runtime_per_section(current)}
    findings = []
    for claim in claims:
        if _dated_post(claim.doc_path):
            continue
        if claim.kind == ClaimKind.runtime_version:
            selected = runtimes.get(claim.claim_id)
            finding = _runtime(context, selected) if selected is not None else None
        elif claim.kind == ClaimKind.dependency:
            finding = _dependency(context, claim)
        else:
            continue
        if finding is not None:
            findings.append(finding)
    return findings
