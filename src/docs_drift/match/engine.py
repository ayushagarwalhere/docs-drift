"""Conservative exact matching from documentation claims to static index symbols."""

import fnmatch
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

from rapidfuzz import fuzz, process

from docs_drift.docs.qualifiers import XREF_PREFIX, context_finding
from docs_drift.index.base import CodeIndex, normalize_name
from docs_drift.index.routes_common import normalize_route
from docs_drift.match.commands import runner_declares_all, validate_commands
from docs_drift.match.context import MatchContext
from docs_drift.match.defaults import match_default_claims
from docs_drift.match.env_prefix import dynamic_env_guard
from docs_drift.match.environment import dependency_variable, standard_variable
from docs_drift.match.rename import rename_finding
from docs_drift.models import Claim, ClaimKind, Evidence, Extraction, Finding, Symbol, SymbolKind, Verdict

_KINDS: dict[ClaimKind, tuple[SymbolKind, ...]] = {
    ClaimKind.cli_flag: (SymbolKind.cli_flag,),
    ClaimKind.cli_command: (SymbolKind.cli_command,),
    ClaimKind.env_var: (SymbolKind.env_var,),
    ClaimKind.endpoint: (SymbolKind.route,),
    ClaimKind.config_key: (SymbolKind.config_key,),
    ClaimKind.symbol: (
        SymbolKind.function,
        SymbolKind.class_,
        SymbolKind.method,
        SymbolKind.attribute,
        SymbolKind.variable,
        SymbolKind.parameter,
        SymbolKind.module,
    ),
    ClaimKind.package_script: (SymbolKind.package_script,),
    ClaimKind.make_target: (SymbolKind.make_target,),
}
_SUPPORTED = {"python", "javascript", "typescript", "yaml", "toml", "json", "ini", "make", "just", "dockerfile"}
ROUTE_PART_COUNT = 2
# Confidence for missing paths, graded by how much the checkout corroborates the claim.
MISSING_SUPPORTED = 0.8
MISSING_PARTIAL = 0.5
MISSING_WEAK_PENALTY = 0.25
PATH_PARENT_EXISTS = 0.8
PATH_ROOT_ABSENT = 0.5
PATH_BARE_NAME = 0.45
PATH_GENERATED_ROOT = 0.35
PATH_SUFFIX_MATCH = 0.7
PATH_BASENAME_MATCH = 0.5
PATH_CONTEXT_MATCH = 0.85
PATH_NEAR_MISS = 0.75
NEAR_PATH_RATIO = 90
NEAR_PATH_SIBLING_RATIO = 95
NEAR_PATH_MIN_CHARS = 12
# Roles naming declarations the AST index enumerates. {data}, {attr} and {obj} also name settings
# keys and runtime attributes (Flask documents config keys as :data:), so they are not evidence.
_DECLARATION_ROLES = frozenset({"class", "exc", "func", "function", "meth", "method"})
# Kinds whose names are specific enough for a single close declaration to mean a typo or rename.
# (Symbols are not: thousands of indexed names make chance near-misses common.)
_NEAR_MISS_KINDS = frozenset({ClaimKind.cli_flag, ClaimKind.env_var, ClaimKind.config_key})
# Plain character similarity: typos and plurals (--line-number vs --line-numbers) score above 88.
NEAR_MISS_RATIO = 88
# Character similarity says little about very short names such as ``-a`` or ``--td``.
NEAR_MISS_MIN_CHARS = 4
# Directories that hold build output, environments or reports rather than tracked sources.
_GENERATED_ROOTS = frozenset(
    {
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        "dist",
        "build",
        "htmlcov",
        "node_modules",
        "__pycache__",
        "site-packages",
        "coverage",
        "target",
        "out",
        "_build",
        ".cache",
    }
)
# Package-manager subcommands that are built in rather than package.json scripts.
_PACKAGE_MANAGER_BUILTINS = frozenset(
    {
        "add",
        "audit",
        "cache",
        "ci",
        "config",
        "create",
        "dedupe",
        "dlx",
        "exec",
        "fund",
        "help",
        "i",
        "import",
        "info",
        "init",
        "install",
        "link",
        "list",
        "login",
        "logout",
        "ls",
        "outdated",
        "pack",
        "prune",
        "publish",
        "rebuild",
        "remove",
        "rm",
        "run",
        "store",
        "uninstall",
        "unlink",
        "up",
        "update",
        "upgrade",
        "version",
        "view",
        "whoami",
        "why",
        "x",
    }
)
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
    # Framework-implicit flags (--help, --version) are never a plausible new name for a removed flag.
    values = {item.name: item for item in candidates if not (item.derived and item.kind == SymbolKind.cli_flag)}
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


@lru_cache(maxsize=4096)
def _route_regex(raw: str) -> re.Pattern[str] | None:
    try:
        return re.compile(raw)
    except re.error:
        return None


def _regex_route_matches(raw_route: str | None, concrete: str) -> bool:
    """A concrete documented URL (``/fixtures/facetable.json``) matches a regex-registered route."""
    if not raw_route or not ("(?P<" in raw_route or raw_route.startswith("^") or raw_route.endswith("$")):
        return False
    if any(char in concrete for char in "{}<>"):
        return False
    pattern = _route_regex(raw_route)
    return bool(pattern and pattern.fullmatch(concrete))


def _route_matches(value: str, candidates: list[Symbol]) -> tuple[list[Symbol], bool]:
    parts = value.split(maxsplit=1)
    if len(parts) == 1:
        method, raw_path = None, parts[0]
    else:
        method, raw_path = parts[0].upper(), parts[1]
    # A query string or fragment is request data, not part of the route.
    raw_path = raw_path.split("#", 1)[0].split("?", 1)[0] or "/"
    _, path = normalize_route(method or "ANY", raw_path)
    result = []
    for item in candidates:
        item_parts = item.name.split(maxsplit=1)
        if len(item_parts) != ROUTE_PART_COUNT:
            continue
        candidate_method, candidate_raw = item_parts
        _, candidate_path = normalize_route(candidate_method, candidate_raw)
        same_path = path == candidate_path or _regex_route_matches(item.raw_path, raw_path)
        if same_path and (method is None or method == "ANY" or candidate_method in {method, "ANY"}):
            result.append(item)
    return result, False


def _file_match(claim: Claim, repo: Path, index: CodeIndex) -> tuple[list[Evidence], str | None]:
    raw = claim.value.replace("\\", "/")
    raw = raw.removeprefix("./")
    raw = raw.rstrip("/") or raw
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


def _match_path(claim: Claim, index: CodeIndex, repo: Path, context: MatchContext) -> Finding:  # noqa: PLR0911
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
        typo = _near_path(claim, index, sibling_only=True)
        if typo is not None:
            # Even a file the reader creates is misspelled when it differs from the project's own
            # file of that directory by a character (`tests/__init__py` for `tests/__init__.py`).
            return _near_path_finding(claim, typo)
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
    resolved = _relative_path_match(claim, index, context)
    if resolved is not None:
        return resolved
    ignored = _git_ignored(claim, context)
    if ignored is not None:
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[_doc_evidence(claim)],
            confidence=0.25,
            reasons=[
                f"Path {claim.value} matches the .gitignore pattern {ignored!r}: it is build output or a local "
                "file, so its absence from the checkout does not establish drift."
            ],
        )
    if _example_code_names(claim, context):
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[_doc_evidence(claim)],
            confidence=0.25,
            reasons=[
                f"The document's example code uses '{claim.value}' as a string, so it names a file of the "
                "reader's project; its absence from this checkout does not establish drift."
            ],
        )
    near = _near_path(claim, index)
    if near is not None:
        return _near_path_finding(claim, near)
    confidence, why = _missing_path_strength(claim, index)
    return Finding(
        claim=claim,
        verdict=Verdict.missing,
        evidence=[_doc_evidence(claim)],
        confidence=confidence,
        reasons=[f"Path {claim.value} does not exist relative to the repository or documentation. {why}"],
    )


def _negated_flag(claim: Claim, index: CodeIndex) -> Finding | None:
    """``--no-X`` with an indexed ``--X``: parsers often generate negations at runtime, so abstain."""
    if claim.kind != ClaimKind.cli_flag or not claim.value.startswith("--no-") or len(claim.value) <= len("--no-"):
        return None
    positive, _ = _symbol_matches(claim.model_copy(update={"value": "--" + claim.value[len("--no-") :]}), index)
    if not positive:
        return None
    symbol = sorted(positive, key=lambda item: (item.file, item.line))[0]
    return Finding(
        claim=claim,
        verdict=Verdict.unverifiable,
        evidence=[_doc_evidence(claim), Evidence(file=symbol.file, line=symbol.line, text=symbol.qualified_name)],
        confidence=0.3,
        reasons=[
            f"{claim.value} is not declared, but {symbol.name} is ({symbol.file}:{symbol.line}). "
            "The negated form may be generated at runtime, so its absence is not proof of drift."
        ],
    )


def _open_map_parent(claim: Claim, index: CodeIndex) -> Symbol | None:
    """The nearest declared ancestor of a dotted config key whose children are user-chosen."""
    if claim.kind != ClaimKind.config_key or "." not in claim.value:
        return None
    parts = claim.value.split(".")
    for end in range(len(parts) - 1, 0, -1):
        prefix = normalize_name(".".join(parts[:end]))
        found = [item for item in index.by_name.get((SymbolKind.config_key, prefix), []) if item.open_map]
        if found:
            return found[0]
    return None


def _normalized_path(claim: Claim) -> str:
    raw = claim.value.replace("\\", "/").removeprefix("./")
    return raw.rstrip("/") or raw


def _relative_path_match(  # noqa: PLR0911 - one return per resolution rule.
    claim: Claim, index: CodeIndex, context: MatchContext
) -> Finding | None:
    """Resolve a path written relative to some other directory than the repository or the document.

    Tutorials name files relative to the reader's project (``flaskr/db.py``), contributor guides
    list files under a path-like heading (``source/``), and captions name the file an adjacent
    include directive shows. Each is checked before calling the path missing.
    """
    raw = _normalized_path(claim)
    if not raw or raw.startswith(("/", "~", "..")):
        return None
    lookup = index.path_lookup
    for directory in context.heading_directories(claim):
        candidate = f"{directory}/{raw}"
        if candidate in lookup:
            return _resolved(claim, candidate, PATH_CONTEXT_MATCH, f"relative to the heading directory {directory}/")
    for nearby in context.nearby_paths(claim):
        if nearby != raw and nearby.endswith("/" + raw) and nearby.removeprefix("./") in lookup:
            path = nearby.removeprefix("./")
            return _resolved(claim, path, PATH_CONTEXT_MATCH, "named by an adjacent include or command")
    matches = [path for path in context.paths_with_basename(PurePosixPath(raw).name) if path.endswith("/" + raw)]
    if len(matches) == 1 and "/" in raw:
        return _resolved(claim, matches[0], PATH_SUFFIX_MATCH, "the only checkout path ending with this name")
    if len(matches) == 1:
        # One file sharing a bare name is weaker evidence: the reader may mean a file of their own.
        return _resolved(claim, matches[0], PATH_BASENAME_MATCH, "the only checkout file with this name")
    if len(matches) > 1:
        listing = ", ".join(matches[:3]) + (", ..." if len(matches) > 3 else "")  # noqa: PLR2004 - display cap
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=[_doc_evidence(claim)],
            confidence=0.3,
            reasons=[
                f"Path {claim.value} is relative to an unstated directory and matches {len(matches)} checkout "
                f"paths ({listing}). Name the directory so the reference can be checked."
            ],
        )
    return None


def _resolved(claim: Claim, path: str, confidence: float, how: str) -> Finding:
    return Finding(
        claim=claim,
        verdict=Verdict.ok,
        suggestion=path,
        evidence=[Evidence(file=path, line=1, text=f"Path exists: {path}")],
        confidence=confidence,
        reasons=[f"Path {claim.value} resolves to {path}, {how}. Writing the full path makes the reference exact."],
    )


def _git_ignored(claim: Claim, context: MatchContext) -> str | None:
    """The root .gitignore pattern that ignores this path (repository- or document-relative), if any."""
    raw = _normalized_path(claim).lstrip("/")
    if not raw:
        return None
    candidates = {raw, (PurePosixPath(claim.doc_path).parent / raw).as_posix()}
    for line in context.lines(".gitignore") or []:
        pattern = line.strip()
        if not pattern or pattern.startswith(("#", "!")):
            continue
        anchored = "/" in pattern.rstrip("/")
        cleaned = pattern.strip("/")
        for candidate in candidates:
            parts = candidate.split("/")
            if anchored:
                hit = any(fnmatch.fnmatchcase("/".join(parts[:end]), cleaned) for end in range(1, len(parts) + 1))
            else:
                hit = any(fnmatch.fnmatchcase(part, cleaned) for part in parts)
            if hit:
                return pattern
    return None


def _example_code_names(claim: Claim, context: MatchContext) -> bool:
    """True when the document's code quotes this path as a string (`toMatchFileSnapshot('./test/x.html')`)."""
    pattern = re.compile(r"""(['"])""" + re.escape(claim.value) + r"\1")
    lines = context.lines(claim.doc_path) or []
    return any(pattern.search(line) for number, line in enumerate(lines, 1) if number != claim.line)


def _near_path_finding(claim: Claim, near: str) -> Finding:
    return Finding(
        claim=claim,
        verdict=Verdict.missing,
        suggestion=near,
        evidence=[_doc_evidence(claim), Evidence(file=near, line=1, text=f"Path exists: {near}")],
        confidence=PATH_NEAR_MISS,
        reasons=[f"Path {claim.value} does not exist; {near} differs from it by a few characters."],
    )


def _near_path(claim: Claim, index: CodeIndex, *, sibling_only: bool = False) -> str | None:
    """The one checkout path a missing multi-part path misspells (`test/requirements-library.txt`).

    Only paths with a directory qualify. A file is compared with checkout files of the same
    extension; an extensionless name (`packages/ui-component`) only with the entries of its own,
    existing parent directory. A single close candidate is a typo or rename, several are coincidence.
    """
    raw = _normalized_path(claim).lstrip("/")
    path = PurePosixPath(raw)
    suffix = path.suffix
    if "/" not in raw or len(raw) < NEAR_PATH_MIN_CHARS:
        return None
    if sibling_only:
        # A reader's own path: only a one-character slip from a file in the same, existing directory.
        if path.parent.as_posix() not in index.path_lookup:
            return None
        pool = [item for item in index.path_lookup if PurePosixPath(item).parent == path.parent]
        hits = process.extract(raw, pool, scorer=fuzz.ratio, limit=2, score_cutoff=NEAR_PATH_SIBLING_RATIO)
        return str(hits[0][0]) if len(hits) == 1 else None
    if suffix:
        pool = [item for item in index.path_lookup if item.endswith(suffix) and item.casefold() != raw.casefold()]
    elif path.parent.as_posix() in index.path_lookup:
        parent = path.parent.as_posix()
        pool = [item for item in index.path_lookup if PurePosixPath(item).parent.as_posix() == parent]
    else:
        return None
    hits = process.extract(raw, pool, scorer=fuzz.ratio, limit=2, score_cutoff=NEAR_PATH_RATIO)
    if len(hits) != 1:
        return None
    return str(hits[0][0])


def _missing_path_strength(claim: Claim, index: CodeIndex) -> tuple[float, str]:
    """Grade a missing path by how much of it the checkout corroborates."""
    raw = _normalized_path(claim)
    parts = PurePosixPath(raw).parts
    if parts and parts[0] in _GENERATED_ROOTS:
        return PATH_GENERATED_ROOT, f"{parts[0]}/ holds generated output or an environment, not tracked files."
    if len(parts) <= 1:
        return PATH_BARE_NAME, (
            "A bare file name with no directory, found nowhere in the checkout, "
            "usually names a file the reader creates."
        )
    lookup = index.path_lookup
    doc_parent = PurePosixPath(claim.doc_path).parent
    parents = {PurePosixPath(raw).parent.as_posix(), (doc_parent / PurePosixPath(raw).parent).as_posix()}
    if lookup and not any(parent in lookup for parent in parents):
        roots = {parts[0], (doc_parent / parts[0]).as_posix()}
        if not any(root in lookup for root in roots):
            return PATH_ROOT_ABSENT, (
                f"No directory {parts[0]}/ exists in the checkout, "
                "so the path is probably relative to the reader's project."
            )
    return PATH_PARENT_EXISTS, ""


def _context_verdict(claim: Claim, context: MatchContext) -> Finding | None:
    """Return an unverifiable result when surrounding facts show the claim is not about this project."""
    reason: str | None = None
    confidence = 0.2
    if claim.kind == ClaimKind.env_var:
        reason = standard_variable(claim.value, context.dependency_env_prefixes | _own_prefixes(context))
        if reason is None:
            dependency = dependency_variable(claim.value, context.dependency_env_prefixes, _own_prefixes(context))
            if dependency:
                confidence = 0.25
                reason = (
                    f"{claim.value} carries the prefix of the declared dependency {dependency.lower()}, "
                    "which reads it; project code need not mention it."
                )
    elif claim.kind in {ClaimKind.cli_flag, ClaimKind.cli_command}:
        token = claim.value.split(maxsplit=1)[0] if claim.kind == ClaimKind.cli_flag else claim.value
        program = context.third_party_owner(claim, token)
        if program:
            reason = (
                f"{claim.value} is passed to {program}, which is not this project's CLI; "
                "its options and subcommands are outside the static index."
            )
        elif claim.kind == ClaimKind.cli_command:
            plugin = context.installed_plugin(claim, claim.value)
            if plugin:
                reason = f"{claim.value} is provided by the plugin {plugin}, installed earlier in this document."
    elif claim.kind == ClaimKind.package_script and claim.value in _PACKAGE_MANAGER_BUILTINS:
        reason = f"{claim.value} is a built-in package-manager command, not a package.json script."
    if reason is None:
        return None
    return Finding(
        claim=claim,
        verdict=Verdict.unverifiable,
        evidence=[_doc_evidence(claim)],
        confidence=confidence,
        reasons=[reason],
    )


def _own_prefixes(context: MatchContext) -> set[str]:
    return {"".join(char if char.isalnum() else "_" for char in name.upper()) for name in context.project_names}


def _match_one(  # noqa: PLR0911, PLR0912 - ordered verdict rules.
    claim: Claim, index: CodeIndex, repo: Path, context: MatchContext
) -> Finding:
    if claim.kind == ClaimKind.file_path:
        return _match_path(claim, index, repo, context)

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
    negated = _negated_flag(claim, index)
    if negated is not None:
        return negated
    open_map = _open_map_parent(claim, index)
    if open_map is not None:
        return Finding(
            claim=claim,
            matched_symbol=open_map,
            verdict=Verdict.ok,
            evidence=[Evidence(file=open_map.file, line=open_map.line, text=open_map.qualified_name)],
            confidence=0.7,
            reasons=[
                f"{claim.value} is an entry of {open_map.name}, which takes free-form keys "
                f"({open_map.file}:{open_map.line})."
            ],
        )

    contextual = _context_verdict(claim, context)
    if contextual is not None:
        return contextual
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
    as_path = _endpoint_naming_a_directory(claim, index)
    if as_path is not None:
        return _match_path(as_path, index, repo, context)
    if confidence_state == "none":
        verdict, confidence = Verdict.unverifiable, 0.2
        reason = f"Cannot verify {claim.kind.value} {claim.value}; index coverage for this language is unavailable."
    elif claim.kind == ClaimKind.symbol and "." in claim.value and confidence_state == "partial":
        verdict, confidence = Verdict.unverifiable, 0.3
        reason = (
            f"Cannot resolve qualified reference {claim.value} with the partial static index. "
            "Imports, module aliases and instance attributes may supply it; absence is not proof of drift."
        )
    else:
        if confidence_state != "none" and _declared_support(claim, index):
            confidence_state = "supported"
        evidence, near = _strong_evidence(claim, pool)
        if claim.kind == ClaimKind.make_target and runner_declares_all(claim.raw_text, index, context):
            # `make X` names a target of the project's own task file, which declares every target literally.
            confidence_state = "supported"
            evidence = "The task file is read statically and declares no pattern rule, so every target is listed."
        verdict, confidence = Verdict.missing, _missing_strength(claim, confidence_state, evidence=evidence)
        if evidence:
            reason += f" {evidence}"
        if near:
            return Finding(
                claim=claim,
                verdict=verdict,
                suggestion=near,
                evidence=[_doc_evidence(claim)],
                confidence=confidence,
                reasons=[reason],
            )
    return Finding(
        claim=claim,
        verdict=verdict,
        suggestion=closest[0].name if len(closest) == 1 else None,
        evidence=[_doc_evidence(claim)],
        confidence=confidence,
        reasons=[reason],
    )


def _endpoint_naming_a_directory(claim: Claim, index: CodeIndex) -> Claim | None:
    """`/scripts` or `/packages/ui-component` with no method: a repository path written with a leading slash.

    When the first segment is a top-level checkout directory, the span is checked as a path.
    """
    if claim.kind != ClaimKind.endpoint or " " in claim.value:
        return None
    raw = claim.value.strip("/")
    if not raw or raw.split("/", 1)[0] not in index.path_lookup or any(char in raw for char in "{}<>:"):
        return None
    return claim.model_copy(update={"kind": ClaimKind.file_path, "value": raw, "qualifier": None})


def _declared_support(claim: Claim, index: CodeIndex) -> bool:
    """Whether the indexer for this claim's surface found the declarations a match would need.

    A project CLI whose flags a framework recognizer declared is fully enumerated for undecorated
    names; an undotted Python/JavaScript name is searched across every AST declaration. Other
    surfaces (settings read dynamically, environment variables) stay partial.
    """
    if claim.kind == ClaimKind.cli_flag:
        return any(item.framework and not item.derived for item in index.by_kind.get(SymbolKind.cli_flag, []))
    if claim.kind == ClaimKind.symbol and "." not in claim.value:
        return any(index.by_kind.get(kind) for kind in (SymbolKind.function, SymbolKind.class_, SymbolKind.method))
    return False


def _flag_core(value: str) -> str:
    return value.lstrip("-")


def _strong_evidence(claim: Claim, pool: list[Symbol]) -> tuple[str, str | None]:  # noqa: PLR0911
    """Evidence that the documentation names this specific declaration, so its absence is drift.

    Returns a reason sentence (empty when there is none) and a suggested replacement name.
    """
    role = (claim.qualifier or "").removeprefix(XREF_PREFIX) if (claim.qualifier or "").startswith(XREF_PREFIX) else ""
    if claim.kind == ClaimKind.symbol and role in _DECLARATION_ROLES:
        return f"The documentation cross-references it with an explicit {{{role}}} role.", None
    if claim.kind not in _NEAR_MISS_KINDS:
        return "", None
    flag = claim.kind == ClaimKind.cli_flag
    names = {
        name: item
        for item in pool
        if not item.derived
        for name in (item.name, *item.aliases)
        if name.startswith("-") or not flag
    }
    core = _flag_core(claim.value)
    # `--p` for `-p`, `-verbose` for `--verbose`: the right name with the wrong number of dashes.
    for dashes in ("-", "--") if flag else ():
        typo = dashes + core
        if typo != claim.value and typo in names and (len(core) == 1) == (dashes == "-"):
            return f"{typo} is declared; the documented form has the wrong number of dashes.", typo
    if len(core) < NEAR_MISS_MIN_CHARS:
        return "", None
    ranked = sorted(
        ((fuzz.ratio(claim.value, name), name) for name in names if _flag_core(name) != core),
        reverse=True,
    )
    if not ranked or ranked[0][0] < NEAR_MISS_RATIO or (len(ranked) > 1 and ranked[1][0] == ranked[0][0]):
        return "", None
    near = ranked[0][1]
    owner = names[near]
    # Two spellings of one option (`--line-number` and `--line-numbers`) only count when they are close
    # to exactly one declared option.
    if {id(names[name]) for score, name in ranked if score >= NEAR_MISS_RATIO} - {id(owner)}:
        return "", None
    return f"It differs by a few characters from the declared {owner.name} ({owner.file}:{owner.line}).", owner.name


def _missing_strength(claim: Claim, coverage: str, *, evidence: str = "") -> float:
    """Confidence that an unmatched identifier is drift rather than an index or extraction gap.

    Only a surface whose declarations the index enumerates ("supported") can establish absence,
    and even then only with evidence that the documentation names a specific declaration: an
    explicit cross-reference role, a near-miss of one declared name, or a dash typo. Without it
    the name may belong to another tool or to the reader's own code, so it stays below the
    default reporting threshold. Prose-pattern extraction weakens it further.
    """
    if coverage == "supported":
        confidence = MISSING_SUPPORTED if evidence else MISSING_SUPPORTED - MISSING_WEAK_PENALTY
    else:
        confidence = MISSING_PARTIAL
    if claim.extraction == Extraction.prose_pattern:
        confidence -= MISSING_WEAK_PENALTY
    return round(confidence, 2)


_ALIAS_PAIR = re.compile(r"(`{1,2})(--?[A-Za-z][\w-]*)\1\s*(?:,|/|\bor\b)\s*(`{1,2})(--?[A-Za-z][\w-]*)\3")
PAIR_CONFIDENCE = 0.8


def _scope(symbol: Symbol) -> str:
    return symbol.qualified_name.rsplit(".", 1)[0]


def _flag_names(symbol: Symbol) -> set[str]:
    return {symbol.name, *symbol.aliases}


def _words(flag: str) -> set[str]:
    core = _flag_core(flag)
    return {word.casefold() for word in re.split(r"[-_]", core) if len(word) > 1} if len(core) > 1 else set()


def _initials(flag: str) -> set[str]:
    """First letters of a long option's words: `--force-terminal` gives {f, t}."""
    return {word[0].casefold() for word in re.split(r"[-_]", _flag_core(flag)) if word}


def _pair_finding(finding: Finding, suggestion: str | None, reason: str, owner: Symbol) -> Finding:
    return finding.model_copy(
        update={
            "verdict": Verdict.missing,
            "suggestion": suggestion,
            "matched_symbol": None,
            "confidence": PAIR_CONFIDENCE,
            "evidence": [_doc_evidence(finding.claim), Evidence(file=owner.file, line=owner.line, text=owner.name)],
            "reasons": [reason],
        }
    )


def _check_pair(  # noqa: PLR0911 - one return per pairing outcome.
    long_finding: Finding, short_finding: Finding, index: CodeIndex
) -> tuple[Finding, Finding]:
    """Check "`--long` or `-s`": the documentation says both spell one option."""
    long_name, short_name = long_finding.claim.value, short_finding.claim.value
    letter = _flag_core(short_name).casefold()
    if letter not in _initials(long_name):
        return long_finding, short_finding  # Unrelated letters usually list two different options.
    longs = [item for item in index.candidates(SymbolKind.cli_flag, long_name) if not item.derived]
    shorts = [item for item in index.candidates(SymbolKind.cli_flag, short_name) if not item.derived]
    if any(short_name in _flag_names(item) for item in longs):
        return long_finding, short_finding
    if longs and shorts:
        scopes = {_scope(item) for item in longs}
        # When the short form's own option starts with its letter (`-c` for `--continue` beside `--cid`),
        # the sentence most likely lists two options; only an unexplained letter is a mis-pairing.
        others = [
            item
            for item in shorts
            if _scope(item) in scopes and not _flag_core(item.name).casefold().startswith(letter)
        ]
        if not others:
            return long_finding, short_finding
        owner = others[0]
        alias = next((name for item in longs for name in item.aliases if len(_flag_core(name)) == 1), None)
        reason = f"{short_name} is the short form of {owner.name} ({owner.file}:{owner.line}), not of {long_name}; " + (
            f"{long_name} is also spelled {alias}." if alias else f"{long_name} has no short form."
        )
        return long_finding, _pair_finding(short_finding, alias, reason, owner)
    if longs and short_finding.verdict == Verdict.missing:
        owner = longs[0]
        alias = next((name for name in owner.aliases if len(_flag_core(name)) == 1), None)
        reason = f"{long_name} ({owner.file}:{owner.line}) " + (
            f"is also spelled {alias}, not {short_name}." if alias else f"has no short form {short_name}."
        )
        return long_finding, _pair_finding(short_finding, alias, reason, owner)
    if shorts and long_finding.verdict in {Verdict.missing, Verdict.renamed}:
        owners = {item.name for item in shorts}
        owner = shorts[0]
        # The undeclared long name must be a variant of the short form's option (`--line-number` for
        # `--line-numbers`), not an unrelated option the sentence happens to pair it with.
        if len(owners) != 1 or not (_words(owner.name) & _words(long_name)):
            return long_finding, short_finding
        reason = (
            f"{short_name} is the short form of {owner.name} ({owner.file}:{owner.line}); {long_name} is not declared."
        )
        return _pair_finding(long_finding, owner.name, reason, owner), short_finding
    return long_finding, short_finding


def check_alias_pairs(findings: list[Finding], index: CodeIndex, context: MatchContext) -> list[Finding]:
    """Resolve both halves of "`--long` or `-s`" and report a pairing the code does not declare."""
    by_line: dict[tuple[str, int], dict[str, int]] = {}
    for position, finding in enumerate(findings):
        claim = finding.claim
        if claim.kind == ClaimKind.cli_flag and finding.verdict != Verdict.unverifiable:
            by_line.setdefault((claim.doc_path, claim.line), {}).setdefault(claim.value, position)
    result = list(findings)
    for (doc_path, line), positions in by_line.items():
        if len(positions) < 2:  # noqa: PLR2004 - a pair
            continue
        lines = context.lines(doc_path) or []
        if not 1 <= line <= len(lines):
            continue
        for match in _ALIAS_PAIR.finditer(lines[line - 1]):
            first, second = match.group(2), match.group(4)
            if first not in positions or second not in positions:
                continue
            short_first = len(_flag_core(first)) == 1
            if short_first == (len(_flag_core(second)) == 1):
                continue  # Two long or two short names: a list of options, not one option's spellings.
            long_value, short_value = (second, first) if short_first else (first, second)
            long_at, short_at = positions[long_value], positions[short_value]
            result[long_at], result[short_at] = _check_pair(result[long_at], result[short_at], index)
    return result


PATH_SIBLING_DIRECTORY = 0.7
MIN_SIBLINGS = 2


def check_sibling_paths(findings: list[Finding], index: CodeIndex) -> list[Finding]:
    """A bare file name listed beside siblings that all live in one directory belongs to that directory.

    "`reader.py` / `reader_c.py` / `reader_py.py`" names three files of one module: when the other
    two resolve to ``aiohttp/_websocket/``, the missing one is missing from that directory, which
    is stronger evidence than a lone bare name that may be the reader's own file.
    """
    by_line: dict[tuple[str, int], list[int]] = {}
    for position, finding in enumerate(findings):
        claim = finding.claim
        if claim.kind == ClaimKind.file_path and "/" not in _normalized_path(claim):
            by_line.setdefault((claim.doc_path, claim.line), []).append(position)
    result = list(findings)
    for positions in by_line.values():
        directories = {
            PurePosixPath(findings[at].suggestion or "").parent.as_posix()
            for at in positions
            if findings[at].verdict == Verdict.ok and findings[at].suggestion
        }
        resolved = sum(1 for at in positions if findings[at].verdict == Verdict.ok and findings[at].suggestion)
        if len(directories) != 1 or resolved < MIN_SIBLINGS:
            continue
        directory = next(iter(directories))
        for at in positions:
            finding = findings[at]
            if finding.verdict != Verdict.missing or finding.confidence >= PATH_SIBLING_DIRECTORY:
                continue
            candidate = f"{directory}/{_normalized_path(finding.claim)}"
            if candidate in index.path_lookup:
                continue
            result[at] = finding.model_copy(
                update={
                    "confidence": PATH_SIBLING_DIRECTORY,
                    "reasons": [
                        f"{finding.claim.value} is listed with files that all live in {directory}/, "
                        f"but {candidate} does not exist."
                    ],
                }
            )
    return result


def match_claims(claims: list[Claim], index: CodeIndex, repo: Path) -> list[Finding]:
    """Match claims exactly, retaining low-confidence and unverifiable results for diagnostics."""
    root = repo.resolve()
    context = MatchContext(root, index)
    findings = [
        context_finding(claim)
        or dynamic_env_guard(claim, index, _match_one(claim, index, root, context), rename_finding)
        for claim in claims
        if claim.kind
        not in {
            ClaimKind.code_command,
            ClaimKind.default_value,
            ClaimKind.prose,
            ClaimKind.dependency,
            ClaimKind.runtime_version,
        }
    ]
    findings = check_alias_pairs(findings, index, context)
    findings = check_sibling_paths(findings, index)
    findings.extend(match_default_claims(claims, index))
    findings.extend(validate_commands(claims, index, repo, context))
    return findings
