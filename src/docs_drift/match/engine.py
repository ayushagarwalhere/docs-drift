"""Conservative exact matching from documentation claims to static index symbols."""

from pathlib import Path

from rapidfuzz import process

from docs_drift.index.base import CodeIndex, normalize_name
from docs_drift.index.routes_common import normalize_route
from docs_drift.match.commands import validate_commands
from docs_drift.match.defaults import match_default_claims
from docs_drift.match.rename import rename_finding
from docs_drift.models import Claim, ClaimKind, Evidence, Finding, Symbol, SymbolKind, Verdict

_KINDS: dict[ClaimKind, tuple[SymbolKind, ...]] = {
    ClaimKind.cli_flag: (SymbolKind.cli_flag,),
    ClaimKind.cli_command: (SymbolKind.cli_command,),
    ClaimKind.env_var: (SymbolKind.env_var,),
    ClaimKind.endpoint: (SymbolKind.route,),
    ClaimKind.config_key: (SymbolKind.config_key,),
    ClaimKind.symbol: (SymbolKind.function, SymbolKind.class_, SymbolKind.method),
    ClaimKind.package_script: (SymbolKind.package_script,),
    ClaimKind.make_target: (SymbolKind.make_target,),
}
_SUPPORTED = {"python", "javascript", "typescript", "yaml", "toml", "json", "ini", "make", "dockerfile"}
ROUTE_PART_COUNT = 2
_COVERAGE_RANK = {"none": 0, "partial": 1, "supported": 2}


def _language(claim: Claim, index: CodeIndex, symbols: list[Symbol]) -> str | None:
    if symbols:
        languages = {item.language for item in symbols}
        return next(iter(languages)) if len(languages) == 1 else None
    if claim.kind in {ClaimKind.endpoint, ClaimKind.cli_flag, ClaimKind.cli_command}:
        frameworks = {item.framework for item in index.symbols if item.framework}
        relevant = {item.language for item in index.symbols if item.framework in frameworks}
        return next(iter(relevant)) if len(relevant) == 1 else None
    languages = set(index.files_by_language)
    return next(iter(languages)) if len(languages) == 1 else None


def _coverage(claim: Claim, index: CodeIndex, symbols: list[Symbol]) -> str:
    language = _language(claim, index, symbols)
    if language is None:
        languages = set(index.files_by_language)
        states = []
        for item_language in languages:
            exact = index.coverage.get(f"{claim.kind.value}:{item_language}")
            states.append(
                exact
                or index.coverage.get(f"all:{item_language}")
                or ("partial" if item_language in _SUPPORTED else "none")
            )
        return min(states, key=lambda state: _COVERAGE_RANK.get(state, 0)) if states else "none"
    exact = index.coverage.get(f"{claim.kind.value}:{language}")
    if exact:
        return exact
    global_state = index.coverage.get(f"all:{language}")
    if global_state:
        return global_state
    # Only literal constructs that have a registered indexer can be called partial.
    # A language having files alone is not evidence that this indexer understands it.
    return "partial" if language in _SUPPORTED else "none"


def _doc_evidence(claim: Claim) -> Evidence:
    return Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)


def _suggestions(claim: Claim, candidates: list[Symbol]) -> list[Symbol]:
    values = {item.name: item for item in candidates}
    if not values:
        return []
    hits = process.extract(claim.value, list(values), limit=3, score_cutoff=35)
    return [values[name] for name, _, _ in hits]


def _same_scope(claim: Claim, symbols: list[Symbol]) -> tuple[list[Symbol], bool]:
    if claim.kind != ClaimKind.cli_flag or not claim.qualifier:
        return symbols, False
    scoped = [
        item
        for item in symbols
        if f".{claim.qualifier}." in f".{item.qualified_name}." or item.qualified_name.endswith(f".{claim.qualifier}")
    ]
    if scoped:
        return scoped, False
    inherited = [item for item in symbols if ".root." in f".{item.qualified_name}."]
    if inherited:
        return inherited, False
    return symbols, bool(symbols)


def _symbol_matches(claim: Claim, index: CodeIndex) -> tuple[list[Symbol], bool]:
    key = (claim.kind, claim.value, claim.qualifier)
    if key not in index.match_cache:
        index.match_cache[key] = _uncached_symbol_matches(claim, index)
    return index.match_cache[key]


def _uncached_symbol_matches(claim: Claim, index: CodeIndex) -> tuple[list[Symbol], bool]:
    kinds = _KINDS.get(claim.kind, ())
    candidates = [item for kind in kinds for item in index.by_kind.get(kind, [])]
    if claim.kind == ClaimKind.endpoint:
        return _route_matches(claim.value, candidates)
    if claim.kind == ClaimKind.symbol and ("." in claim.value):
        matches = [
            item
            for item in candidates
            if item.qualified_name == claim.value or item.qualified_name.endswith("." + claim.value)
        ]
    else:
        normalized = normalize_name(claim.value)
        # The index already stores normalized names and aliases. Qualified names
        # are included in by_name too, so retain the original name-only test.
        found = {
            item.symbol_id
            for kind in kinds
            for item in (*index.by_name.get((kind, normalized), []), *index.by_alias.get((kind, normalized), []))
            if normalized == normalize_name(item.name)
            or any(normalized == normalize_name(alias) for alias in item.aliases)
        }
        matches = [item for item in candidates if item.symbol_id in found]
    if claim.kind == ClaimKind.cli_flag:
        matches, weak = _same_scope(claim, matches)
        if weak:
            return matches, True
    if claim.kind == ClaimKind.symbol and matches:
        public = [item for item in matches if not item.name.startswith("_")]
        if public:
            matches = public
        else:
            return matches, True
        exported = [item for item in matches if item.exported]
        if exported:
            matches = exported
    return matches, False


def _route_matches(value: str, candidates: list[Symbol]) -> tuple[list[Symbol], bool]:
    parts = value.split(maxsplit=1)
    if len(parts) == 1:
        method, raw_path = None, parts[0]
    else:
        method, raw_path = parts[0].upper(), parts[1]
    _, path = normalize_route(method or "ANY", raw_path)
    result = []
    for item in candidates:
        item_parts = item.name.split(maxsplit=1)
        if len(item_parts) != ROUTE_PART_COUNT:
            continue
        candidate_method, candidate_raw = item_parts
        _, candidate_path = normalize_route(candidate_method, candidate_raw)
        if path == candidate_path and (method is None or method == "ANY" or candidate_method in {method, "ANY"}):
            result.append(item)
    return result, False


def _file_match(claim: Claim, repo: Path, index: CodeIndex) -> tuple[list[Evidence], str | None]:
    raw = claim.value.replace("\\", "/")
    raw = raw.removeprefix("./")
    doc_relative = (Path(claim.doc_path).parent / raw).as_posix()
    roots = (raw, doc_relative)
    for relative in roots:
        if relative in index.path_lookup:
            return [Evidence(file=relative, line=1, text=f"Path exists: {relative}")], None
    folded = index.folded_paths
    case_candidate = folded.get(raw.casefold()) or folded.get(doc_relative.casefold())
    if case_candidate:
        return [], case_candidate
    for relative in roots:
        target = repo / relative
        if not target.is_symlink() and target.resolve().is_relative_to(repo) and target.exists():
            normalized = target.resolve().relative_to(repo).as_posix()
            if normalized and normalized != ".":
                return [Evidence(file=normalized, line=1, text=f"Path exists: {relative}")], None
    return [], None


def _reason_candidates(candidates: list[Symbol]) -> str:
    if not candidates:
        return "No close indexed candidates were found."
    listing = ", ".join(f"{item.name} ({item.file}:{item.line})" for item in candidates)
    return f"Closest indexed candidates: {listing}."


def _match_path(claim: Claim, index: CodeIndex, repo: Path) -> Finding:
    if claim.qualifier == "non-checkout-path":
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[_doc_evidence(claim)],
            confidence=0.2,
            reasons=[
                "This path is explicitly an example, generated output or external reference. "
                "Its absence from this checkout does not establish drift."
            ],
        )
    evidence, case_name = _file_match(claim, repo, index)
    if evidence:
        return Finding(
            claim=claim,
            verdict=Verdict.ok,
            evidence=evidence,
            confidence=0.95,
            reasons=[f"Path {claim.value} exists relative to the repository or documentation."],
        )
    if claim.qualifier == "ambiguous-path":
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[_doc_evidence(claim)],
            confidence=0.2,
            reasons=[
                "A slash alone does not establish a checkout path; this may be a protocol, media type or name. "
                "Use explicit file/directory context when asserting filesystem existence."
            ],
        )
    if case_name:
        return Finding(
            claim=claim,
            verdict=Verdict.missing,
            suggestion=case_name,
            evidence=[_doc_evidence(claim)],
            confidence=0.8,
            reasons=[f"Path {claim.value} differs in case from indexed path {case_name}."],
        )
    return Finding(
        claim=claim,
        verdict=Verdict.missing,
        evidence=[_doc_evidence(claim)],
        confidence=0.8,
        reasons=[f"Path {claim.value} does not exist relative to the repository or documentation."],
    )


def _match_one(claim: Claim, index: CodeIndex, repo: Path) -> Finding:
    if claim.kind == ClaimKind.file_path:
        return _match_path(claim, index, repo)

    candidates, weak = _symbol_matches(claim, index)
    if candidates:
        symbol = sorted(candidates, key=lambda item: (item.file, item.line))[0]
        confidence = 0.5 if weak else (0.55 if claim.kind == ClaimKind.symbol and symbol.name.startswith("_") else 0.95)
        reason = f"{claim.kind.value} {claim.value} matches {symbol.qualified_name} at {symbol.file}:{symbol.line}."
        if weak:
            reason += " The match is outside the requested command scope or is private."
        return Finding(
            claim=claim,
            matched_symbol=symbol,
            verdict=Verdict.ok,
            evidence=[Evidence(file=symbol.file, line=symbol.line, text=symbol.qualified_name)],
            confidence=confidence,
            reasons=[reason],
        )

    searched_kinds = _KINDS.get(claim.kind, ())
    pool = [item for kind in searched_kinds for item in index.by_kind.get(kind, [])]
    closest = _suggestions(claim, pool)
    reason = f"No indexed {claim.kind.value} named {claim.value} was found. {_reason_candidates(closest)}"
    confidence_state = _coverage(claim, index, pool)
    if claim.kind == ClaimKind.endpoint:
        parts = claim.value.split(maxsplit=1)
        if len(parts) == ROUTE_PART_COUNT:
            _, requested_path = normalize_route(parts[0], parts[1])
            wrong_methods = []
            for item in pool:
                item_parts = item.name.split(maxsplit=1)
                if (
                    len(item_parts) == ROUTE_PART_COUNT
                    and normalize_route(item_parts[0], item_parts[1])[1] == requested_path
                ):
                    wrong_methods.append(item)
            if wrong_methods:
                methods = ", ".join(sorted({item.name.split(maxsplit=1)[0] for item in wrong_methods}))
                reason = f"Route {claim.value} exists as {methods}, not {parts[0].upper()}."
                return Finding(
                    claim=claim,
                    verdict=Verdict.missing,
                    suggestion=methods,
                    evidence=[
                        _doc_evidence(claim),
                        *[Evidence(file=item.file, line=item.line, text=item.name) for item in wrong_methods],
                    ],
                    confidence=0.8,
                    reasons=[reason],
                )
    if confidence_state == "none":
        verdict, confidence = Verdict.unverifiable, 0.2
        reason = f"Cannot verify {claim.kind.value} {claim.value}; index coverage for this language is unavailable."
    elif claim.kind == ClaimKind.symbol and "." in claim.value and confidence_state == "partial":
        verdict, confidence = Verdict.unverifiable, 0.3
        reason = (
            f"Cannot resolve qualified reference {claim.value} with the partial static index. "
            "Imports, module aliases and instance attributes may supply it; absence is not proof of drift."
        )
    elif confidence_state == "partial":
        verdict, confidence = Verdict.missing, 0.5
    else:
        verdict, confidence = Verdict.missing, 0.8
    return Finding(
        claim=claim,
        verdict=verdict,
        suggestion=closest[0].name if len(closest) == 1 else None,
        evidence=[_doc_evidence(claim)],
        confidence=confidence,
        reasons=[reason],
    )


def match_claims(claims: list[Claim], index: CodeIndex, repo: Path) -> list[Finding]:
    """Match claims exactly, retaining low-confidence and unverifiable results for diagnostics."""
    findings = [
        rename_finding(claim, index, _match_one(claim, index, repo.resolve()))
        for claim in claims
        if claim.kind not in {ClaimKind.code_command, ClaimKind.default_value, ClaimKind.prose}
    ]
    findings.extend(match_default_claims(claims, index))
    findings.extend(validate_commands(claims, index, repo))
    return findings
