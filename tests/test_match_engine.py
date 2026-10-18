"""Exact match behavior; static symbols are synthetic and no repository code runs."""

from pathlib import Path

from docs_drift.cli import _same_env_concept
from docs_drift.ids import make_claim_id, make_section_id, make_symbol_id
from docs_drift.index.base import CodeIndex
from docs_drift.match.engine import match_claims
from docs_drift.models import Claim, ClaimKind, Extraction, Symbol, SymbolKind, Verdict


def _claim(kind: ClaimKind, value: str, qualifier: str | None = None, doc_path: str = "README.md") -> Claim:
    section_id = make_section_id(doc_path, ["Usage"])
    return Claim(
        claim_id=make_claim_id(doc_path, ["Usage"], kind.value, value),
        kind=kind,
        raw_text=f"`{value}`",
        value=value,
        qualifier=qualifier,
        doc_path=doc_path,
        line=4,
        column=1,
        section_id=section_id,
        extraction=Extraction.inline_code,
        extraction_confidence=0.95,
    )


def _symbol(  # noqa: PLR0913
    kind: SymbolKind,
    name: str,
    qname: str,
    *,
    file: str = "src/app.py",
    line: int = 10,
    aliases: list[str] | None = None,
    framework: str | None = None,
) -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(kind.value, qname, file),
        kind=kind,
        name=name,
        qualified_name=qname,
        aliases=aliases or [],
        file=file,
        line=line,
        language="python",
        framework=framework,
    )


def test_alias_and_scoped_flag_matching() -> None:
    index = CodeIndex(
        symbols=[
            _symbol(SymbolKind.cli_flag, "--port", "app.serve.--port", aliases=["-p"]),
            _symbol(SymbolKind.cli_flag, "--port", "app.migrate.--port", file="src/migrate.py"),
        ]
    )
    matched = match_claims([_claim(ClaimKind.cli_flag, "-p", "serve")], index, Path("."))[0]
    assert matched.verdict == Verdict.ok
    assert matched.matched_symbol is not None and matched.matched_symbol.qualified_name == "app.serve.--port"
    weak = match_claims([_claim(ClaimKind.cli_flag, "--port", "deploy")], index, Path("."))[0]
    assert weak.verdict == Verdict.ok and weak.confidence == 0.5


def test_endpoint_path_match_ignores_parameter_spelling_and_method_mismatch_is_explained() -> None:
    route = _symbol(SymbolKind.route, "POST /users/{param}", "POST /users/{param}", framework="fastapi")
    index = CodeIndex(symbols=[route])
    finding = match_claims([_claim(ClaimKind.endpoint, "GET /users/{id}")], index, Path("."))[0]
    assert finding.verdict == Verdict.missing
    assert finding.suggestion == "POST"
    assert "exists as POST, not GET" in finding.reasons[0]
    path_only = match_claims([_claim(ClaimKind.endpoint, "/users/:id")], index, Path("."))[0]
    assert path_only.verdict == Verdict.ok


def test_qualified_symbol_suffix_and_private_low_confidence() -> None:
    index = CodeIndex(
        symbols=[
            _symbol(SymbolKind.function, "list", "pkg.client.users.list", file="src/client.py"),
            _symbol(SymbolKind.function, "_parse", "pkg.client._parse", file="src/client.py", line=20),
        ]
    )
    public = match_claims([_claim(ClaimKind.symbol, "client.users.list")], index, Path("."))[0]
    assert public.verdict == Verdict.ok
    private = match_claims([_claim(ClaimKind.symbol, "_parse")], index, Path("."))[0]
    assert private.verdict == Verdict.ok and private.confidence == 0.5


def test_file_path_checks_exact_case_and_relative_doc_location(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "docs").mkdir()
    (repo / "docs" / "setup.md").write_text("setup", encoding="utf-8")
    index = CodeIndex(source_paths=["docs/setup.md"])
    relative = match_claims([_claim(ClaimKind.file_path, "setup.md", doc_path="docs/README.md")], index, repo)[0]
    assert relative.verdict == Verdict.ok
    case = match_claims([_claim(ClaimKind.file_path, "docs/SETUP.md")], index, repo)[0]
    assert case.verdict == Verdict.missing and case.suggestion == "docs/setup.md"


def test_documented_default_compares_with_authoritative_code_default() -> None:
    claim = _claim(ClaimKind.default_value, "--workers", "cli_flag").model_copy(
        update={"expected_default": "4", "raw_text": "workers default 4"}
    )
    symbol = _symbol(SymbolKind.cli_flag, "--workers", "app.serve.--workers")
    symbol.default = "8"
    finding = match_claims([claim], CodeIndex(symbols=[symbol]), Path("."))[0]
    assert finding.verdict == Verdict.default_changed
    assert finding.suggestion == "8"
    assert len(finding.evidence) == 2


def test_code_command_third_party_is_never_checked_as_project_cli(tmp_path: Path) -> None:
    claim = _claim(ClaimKind.code_command, "git commit --amend")
    finding = match_claims([claim], CodeIndex(), tmp_path)[0]
    assert finding.verdict == Verdict.unverifiable
    assert "third-party commands are skipped" in finding.reasons[0]


def test_make_command_validates_known_target_from_static_index(tmp_path: Path) -> None:
    claim = _claim(ClaimKind.code_command, "make test")
    target = _symbol(SymbolKind.make_target, "test", "test", file="Makefile")
    finding = match_claims([claim], CodeIndex(symbols=[target]), tmp_path)[0]
    assert finding.verdict == Verdict.ok


def test_env_var_rename_requires_shared_specific_meaning_token() -> None:
    assert _same_env_concept("API_URL", "API_BASE_URL")
    assert not _same_env_concept("APP_DEBUG", "APP_LOG_LEVEL")


def test_npm_run_requires_a_known_package_script(tmp_path: Path) -> None:
    claim = _claim(ClaimKind.code_command, "npm run buidl")
    script = _symbol(SymbolKind.package_script, "build", "build", file="package.json")
    finding = match_claims([claim], CodeIndex(symbols=[script]), tmp_path)[0]
    assert finding.verdict == Verdict.invalid_command
    assert "Package script buidl was not found" in finding.reasons[0]


def test_python_module_command_matches_static_subcommand_and_flag(tmp_path: Path) -> None:
    claim = _claim(ClaimKind.code_command, "python -m pkg serve --port 8000")
    command = _symbol(SymbolKind.cli_command, "serve", "pkg.serve")
    flag = _symbol(SymbolKind.cli_flag, "--port", "pkg.serve.--port")
    finding = match_claims([claim], CodeIndex(symbols=[command, flag]), tmp_path)[0]
    assert finding.verdict == Verdict.ok
