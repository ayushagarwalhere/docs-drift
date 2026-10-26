"""Context-aware matching: relative paths, OS/dependency env vars, third-party commands, calibration.

Each case reproduces a false positive from the October 2026 sweep of popular repositories
(vercel/serve, pallets/flask, fastapi/typer, encode/httpx, encode/uvicorn, psf/black,
simonw/datasette, simonw/llm, sindresorhus/np) with a minimal synthetic snippet. Nothing runs.
"""

from pathlib import Path

import pytest

from docs_drift.ids import make_claim_id, make_section_id, make_symbol_id
from docs_drift.index.base import CodeIndex
from docs_drift.match.engine import match_claims
from docs_drift.models import Claim, ClaimKind, Extraction, Finding, Symbol, SymbolKind, Verdict

GATE = 0.6


def _claim(  # noqa: PLR0913
    kind: ClaimKind,
    value: str,
    *,
    line: int = 1,
    column: int = 1,
    doc_path: str = "README.md",
    extraction: Extraction = Extraction.inline_code,
) -> Claim:
    return Claim(
        claim_id=make_claim_id(doc_path, ["Usage"], kind.value, f"{value}:{line}"),
        kind=kind,
        raw_text=f"`{value}`",
        value=value,
        doc_path=doc_path,
        line=line,
        column=column,
        section_id=make_section_id(doc_path, ["Usage"]),
        extraction=extraction,
        extraction_confidence=0.9,
    )


def _symbol(kind: SymbolKind, name: str, qname: str, file: str = "src/app/cli.py", **extra: object) -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(kind.value, qname, file),
        kind=kind,
        name=name,
        qualified_name=qname,
        file=file,
        line=1,
        language="python",
        **extra,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _line_of(text: str, needle: str) -> tuple[int, int]:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number, line.index(needle) + 1
    raise AssertionError(needle)


def _match(repo: Path, claim: Claim, index: CodeIndex | None = None) -> Finding:
    return match_claims([claim], index or CodeIndex(), repo)[0]


def _doc_claim(kind: ClaimKind, value: str, doc: str, needle: str | None = None, doc_path: str = "README.md") -> Claim:
    line, column = _line_of(doc, needle or f"`{value}`")
    return _claim(kind, value, line=line, column=column, doc_path=doc_path)


# ----- group 2: paths relative to another directory -----------------------------------------


def test_path_under_a_path_like_heading_resolves_against_that_directory(tmp_path: Path) -> None:
    # vercel/serve contributing.md lists `utilities/config.ts` under `#### source/utilities/`.
    doc = (
        "#### `source/utilities/`\n\n- `utilities/config.ts`: Parses configuration.\n\n"
        "#### `source/`\n\n- `main.ts`: Entrypoint.\n"
    )
    repo = _repo(tmp_path, {"contributing.md": doc})
    index = CodeIndex(source_paths=["source/utilities/config.ts", "source/main.ts", "source/types.ts"])
    nested = _match(
        repo, _doc_claim(ClaimKind.file_path, "utilities/config.ts", doc, doc_path="contributing.md"), index
    )
    top = _match(repo, _doc_claim(ClaimKind.file_path, "main.ts", doc, doc_path="contributing.md"), index)
    assert nested.verdict == Verdict.ok and nested.suggestion == "source/utilities/config.ts"
    assert top.verdict == Verdict.ok and top.suggestion == "source/main.ts"


def test_caption_names_the_file_an_adjacent_include_directive_shows(tmp_path: Path) -> None:
    # fastapi/typer: "Let's start with a file `reigns.py`:" followed by {* docs_src/.../reigns.py *}.
    doc = "Let's start with a file `reigns.py`:\n\n{* docs_src/subcommands/tutorial003_py310/reigns.py *}\n"
    repo = _repo(tmp_path, {"README.md": doc})
    index = CodeIndex(
        source_paths=["docs_src/subcommands/tutorial003_py310/reigns.py", "docs_src/subcommands/other/reigns.py"]
    )
    finding = _match(repo, _doc_claim(ClaimKind.file_path, "reigns.py", doc), index)
    assert finding.verdict == Verdict.ok
    assert finding.suggestion == "docs_src/subcommands/tutorial003_py310/reigns.py"


def test_bare_name_is_resolved_by_a_command_in_the_next_lines(tmp_path: Path) -> None:
    # simonw/datasette: "use the ``fixtures.py`` script" then "uv run python tests/fixtures.py".
    doc = "You can also use the ``fixtures.py`` script. To do that::\n\n    uv run python tests/fixtures.py db.db\n"
    repo = _repo(tmp_path, {"README.md": doc})
    index = CodeIndex(source_paths=["tests/fixtures.py", "other/fixtures.py"])
    finding = _match(repo, _doc_claim(ClaimKind.file_path, "fixtures.py", doc, "``fixtures.py``"), index)
    assert finding.verdict == Verdict.ok and finding.suggestion == "tests/fixtures.py"


def test_tutorial_project_path_resolves_by_unique_suffix(tmp_path: Path) -> None:
    # pallets/flask docs/tutorial/factory.rst names flaskr/__init__.py; it lives in examples/tutorial/.
    index = CodeIndex(source_paths=["examples/tutorial/flaskr/__init__.py", "src/flask/__init__.py"])
    finding = _match(tmp_path, _claim(ClaimKind.file_path, "flaskr/__init__.py", doc_path="docs/factory.rst"), index)
    assert finding.verdict == Verdict.ok
    assert finding.suggestion == "examples/tutorial/flaskr/__init__.py"
    assert finding.confidence < 0.95


def test_relative_path_matching_several_files_is_unverifiable(tmp_path: Path) -> None:
    index = CodeIndex(source_paths=["a/app/__init__.py", "b/app/__init__.py"])
    finding = _match(tmp_path, _claim(ClaimKind.file_path, "app/__init__.py"), index)
    assert finding.verdict == Verdict.unverifiable
    assert "matches 2 checkout paths" in finding.reasons[0]


def test_trailing_slash_directories_are_found(tmp_path: Path) -> None:
    index = CodeIndex(source_paths=["src/users/models.py"])
    assert _match(tmp_path, _claim(ClaimKind.file_path, "src/users/"), index).verdict == Verdict.ok


@pytest.mark.parametrize(
    ("value", "reported"),
    [
        ("src/removed.py", True),  # the directory exists, so a missing file is real evidence
        ("wsgi.py", False),  # bare name the reader creates
        ("templates/pages/about.html", False),  # no part of the path exists in the checkout
        (".venv/var/app-instance/config.py", False),  # environment directory
        ("dist/app-1.0.0-py3-none-any.whl", False),  # build output
    ],
)
def test_missing_path_confidence_follows_checkout_evidence(tmp_path: Path, value: str, reported: bool) -> None:
    index = CodeIndex(source_paths=["src/app.py", "docs/index.md"])
    finding = _match(tmp_path, _claim(ClaimKind.file_path, value), index)
    assert finding.verdict == Verdict.missing
    assert (finding.confidence >= GATE) is reported


# ----- group 12: environment variables read outside project code -----------------------------


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "NO_PROXY", "XDG_CACHE_HOME", "NO_COLOR", "PYTHONPATH", "LC_ALL"])
def test_standard_environment_variables_are_unverifiable(tmp_path: Path, name: str) -> None:
    index = CodeIndex(symbols=[_symbol(SymbolKind.env_var, "APP_HOME", "APP_HOME")])
    finding = _match(tmp_path, _claim(ClaimKind.env_var, name), index)
    assert finding.verdict == Verdict.unverifiable
    assert finding.confidence < GATE


def test_dependency_prefixed_variable_is_attributed_to_the_dependency(tmp_path: Path) -> None:
    # encode/uvicorn docs/settings.md: WATCHFILES_FORCE_POLLING is read by watchfiles.
    pyproject = '[project]\nname = "uvicorn"\ndependencies = ["click>=7"]\n[project.optional-dependencies]\n'
    pyproject += 'standard = ["watchfiles>=0.13", "python-dotenv>=0.13"]\n'
    repo = _repo(tmp_path, {"pyproject.toml": pyproject})
    index = CodeIndex(symbols=[_symbol(SymbolKind.env_var, "UVICORN_PORT", "UVICORN_PORT")])
    polling = _match(repo, _claim(ClaimKind.env_var, "WATCHFILES_FORCE_POLLING"), index)
    assert polling.verdict == Verdict.unverifiable and "watchfiles" in polling.reasons[0]
    # The project's own prefix is never treated as a dependency, and unrelated names stay missing.
    assert _match(repo, _claim(ClaimKind.env_var, "UVICORN_HOST"), index).verdict == Verdict.missing
    assert _match(repo, _claim(ClaimKind.env_var, "APP_DEBUG"), index).verdict == Verdict.missing


def test_tool_prefix_is_project_owned_in_that_tools_own_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": '[project]\nname = "pip"\n'})
    index = CodeIndex(symbols=[_symbol(SymbolKind.env_var, "PIP_HOME", "PIP_HOME")])
    assert _match(repo, _claim(ClaimKind.env_var, "PIP_CONFIG_FILE"), index).verdict == Verdict.missing


# ----- group 13: third-party commands and flags ---------------------------------------------


def _cli_index() -> CodeIndex:
    return CodeIndex(
        symbols=[
            _symbol(SymbolKind.cli_command, "tool", "app.tool"),
            _symbol(SymbolKind.cli_flag, "--line-numbers", "app.tool.--line-numbers"),
        ],
        files_by_language={"python": 1},
    )


PYPROJECT = '[project]\nname = "tool"\n[project.scripts]\ntool = "app.cli:main"\n'


def test_flags_listed_under_a_lead_in_naming_another_program(tmp_path: Path) -> None:
    # fastapi/typer docs/tutorial/options/name.md:145
    doc = "You can pass these *CLI option* short names to `tar`:\n\n* `-x`: means extract.\n* `-v`: verbose.\n"
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    finding = _match(repo, _doc_claim(ClaimKind.cli_flag, "-x", doc), _cli_index())
    assert finding.verdict == Verdict.unverifiable and "tar" in finding.reasons[0]


def test_flag_attributed_to_another_server_in_the_same_sentence(tmp_path: Path) -> None:
    # encode/httpx docs/advanced/ssl.md:83
    doc = (
        "2. Pass the key files when starting your server. (For example, [Uvicorn](https://www.uvicorn.org) "
        "provides the `--ssl-keyfile` and `--ssl-certfile` options.)\n3. Configure `tool` to use them.\n"
    )
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    for flag in ("--ssl-keyfile", "--ssl-certfile"):
        assert _match(repo, _doc_claim(ClaimKind.cli_flag, flag, doc), _cli_index()).verdict == Verdict.unverifiable


def test_flag_used_only_with_another_program_elsewhere_in_the_document(tmp_path: Path) -> None:
    # fastapi/typer first-steps.md:218 refers back to `ls ./myproject --size` far above.
    doc = "```console\n$ ls ./myproject --size\n```\n" + "\n" * 40 + "The *CLI option* `--size` is a flag.\n"
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    assert _match(repo, _doc_claim(ClaimKind.cli_flag, "--size", doc), _cli_index()).verdict == Verdict.unverifiable


def test_interpreter_and_task_runner_arguments_are_not_project_flags(tmp_path: Path) -> None:
    doc = "```console\n$ uv run python -m typer\n$ just playwright firefox --headed\n$ uv run tool --verbose\n```\n"
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    module = _doc_claim(ClaimKind.cli_flag, "-m", doc, "-m")
    headed = _doc_claim(ClaimKind.cli_flag, "--headed", doc, "--headed")
    browser = _doc_claim(ClaimKind.cli_command, "firefox", doc, "firefox")
    verbose = _doc_claim(ClaimKind.cli_flag, "--verbose", doc, "--verbose")
    findings = match_claims([module, headed, browser, verbose], _cli_index(), repo)
    assert [item.verdict for item in findings] == [
        Verdict.unverifiable,
        Verdict.unverifiable,
        Verdict.unverifiable,
        Verdict.missing,  # passed to the project's own CLI through `uv run`
    ]


def test_project_flags_near_other_tool_names_are_still_checked(tmp_path: Path) -> None:
    # rich-cli README.md:61 (the verified real case) and sindresorhus/np readme.md:211.
    doc = (
        "To highlight a file enter `tool` followed by a path.\n\n```\ntool loop.py -n\n```\n\n"
        "Add the `--line-number` or `-n` switch to enable line numbers.\n\n"
        "Instead of `npm test` or `yarn test`, you can use the `--test-script` flag.\n\n"
        "For example, `tool --test-script=publish-test` runs that script.\n"
    )
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    renamed = _match(repo, _doc_claim(ClaimKind.cli_flag, "--line-number", doc), _cli_index())
    script = _match(repo, _doc_claim(ClaimKind.cli_flag, "--test-script", doc), _cli_index())
    assert renamed.is_drift and renamed.verdict in {Verdict.missing, Verdict.renamed}
    assert script.verdict == Verdict.missing


def test_command_from_a_plugin_installed_earlier_is_unverifiable(tmp_path: Path) -> None:
    # simonw/llm docs/setup.md:78 runs `llm python` after `llm install llm-python`.
    doc = "```bash\ntool install tool-python\ntool python\n```\n"
    repo = _repo(tmp_path, {"README.md": doc, "pyproject.toml": PYPROJECT})
    index = CodeIndex(
        symbols=[
            _symbol(SymbolKind.cli_command, "tool", "app.tool"),
            _symbol(SymbolKind.cli_command, "install", "app.tool.install"),
        ]
    )
    line, _ = _line_of(doc, "tool python")
    command = _claim(ClaimKind.cli_command, "python", line=line, extraction=Extraction.code_block)
    full = _claim(ClaimKind.code_command, "tool python", line=line, extraction=Extraction.code_block)
    findings = match_claims([command, full], index, repo)
    assert [item.verdict for item in findings] == [Verdict.unverifiable, Verdict.unverifiable]
    assert "tool-python" in findings[0].reasons[0]


def test_package_manager_builtins_are_not_package_scripts(tmp_path: Path) -> None:
    # vercel/serve contributing.md:94 `pnpm install`.
    index = CodeIndex(symbols=[_symbol(SymbolKind.package_script, "build", "build", file="package.json")])
    assert _match(tmp_path, _claim(ClaimKind.package_script, "install"), index).verdict == Verdict.unverifiable
    assert _match(tmp_path, _claim(ClaimKind.package_script, "buidl"), index).is_drift


def test_arguments_forwarded_by_a_repository_script_are_unverifiable(tmp_path: Path) -> None:
    # encode/uvicorn docs/contributing.md:102 `scripts/docs serve` wraps another program.
    doc = "```shell\n$ scripts/docs serve\n```\n"
    repo = _repo(tmp_path, {"README.md": doc, "scripts/docs": '#!/bin/sh\nexec zensical "$@"\n'})
    index = CodeIndex(symbols=[_symbol(SymbolKind.cli_command, "uvicorn", "uvicorn.main")])
    line, _ = _line_of(doc, "scripts/docs")
    command = _claim(ClaimKind.code_command, "scripts/docs serve", line=line, extraction=Extraction.code_block)
    word = _claim(ClaimKind.cli_command, "serve", line=line, extraction=Extraction.code_block)
    findings = match_claims([word, command], index, repo)
    assert {item.verdict for item in findings} == {Verdict.unverifiable}


# ----- command validation calibration -------------------------------------------------------


def _command(repo: Path, text: str, index: CodeIndex) -> Finding:
    return _match(repo, _claim(ClaimKind.code_command, text, extraction=Extraction.code_block), index)


def _group_index() -> CodeIndex:
    return CodeIndex(
        symbols=[
            _symbol(SymbolKind.cli_command, "tool", "app.tool"),
            _symbol(SymbolKind.cli_command, "deploy", "app.tool.deploy"),
            _symbol(SymbolKind.cli_flag, "--model", "app.tool.--model", aliases=["-m"]),
            _symbol(SymbolKind.cli_flag, "--line-numbers", "app.tool.deploy.--line-numbers"),
        ]
    )


@pytest.mark.parametrize(
    "text",
    [
        "tool --help",  # generated by every supported framework
        "tool -m model -",  # '-' is standard input
        "tool deploy --[TAB][TAB]",  # completion placeholder
        "tool Ten fun names for a pet pelican",  # prose prompt, not a subcommand
        "tool -m model What is the capital of France?",
        "tool Tell me about my system: $(uname -a)",  # subshell belongs to another program
        "tool deploy -m model | grep -v x",  # the pipeline continues with another program
    ],
)
def test_command_data_and_implicit_options_are_not_invalid(tmp_path: Path, text: str) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": PYPROJECT})
    assert _command(repo, text, _group_index()).verdict == Verdict.ok


@pytest.mark.parametrize(
    ("text", "reported"),
    [
        ("tool deploy --dry-run", True),  # flag contradicts a resolved subcommand's declaration
        ("tool deploy --line-number", True),  # close indexed name
        ("tool --version", False),  # frameworks add it implicitly
        ("tool --app hello deploy", False),  # root option the static index may not see
        ("tool extract text", False),  # a positional word may be a default command's input
    ],
)
def test_invalid_command_confidence_follows_evidence(tmp_path: Path, text: str, reported: bool) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": PYPROJECT})
    finding = _command(repo, text, _group_index())
    assert finding.verdict == Verdict.invalid_command
    assert (finding.confidence >= GATE) is reported


def test_near_miss_suggestion_stays_out_of_reason_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": PYPROJECT})
    finding = _command(repo, "tool deploy --line-number", _group_index())
    assert finding.suggestion == "--line-numbers"
    assert "--line-numbers" not in " ".join(finding.reasons)  # rule fixes parse unknown flags from reasons


def test_root_option_near_miss_in_a_command_group_stays_weak(tmp_path: Path) -> None:
    # simonw/llm: `llm --schema ...` goes to the default prompt command; only `--schemas` was indexed.
    root = _symbol(SymbolKind.cli_command, "tool", "app.tool")
    child = _symbol(SymbolKind.cli_command, "logs", "app.tool.logs", parent_id=root.symbol_id)
    flag = _symbol(SymbolKind.cli_flag, "--schemas", "app.tool.logs.--schemas")
    repo = _repo(tmp_path, {"pyproject.toml": PYPROJECT})
    finding = _command(repo, "tool --schema dog.json", CodeIndex(symbols=[root, child, flag]))
    assert finding.verdict == Verdict.invalid_command and finding.suggestion == "--schemas"
    assert finding.confidence < GATE


def test_unindexed_task_runner_file_makes_targets_unverifiable(tmp_path: Path) -> None:
    # simonw/datasette and simonw/llm use a capitalised Justfile the index did not read.
    repo = _repo(tmp_path, {"Justfile": "playwright *options:\n    pytest {{options}}\n"})
    index = CodeIndex(source_paths=["Justfile"])
    assert _command(repo, "just playwright firefox", index).verdict == Verdict.unverifiable
    pattern = _repo(tmp_path / "docs", {"Makefile": "%: Makefile\n\t@sphinx-build -M $@\nhelp:\n\techo\n"})
    target = _symbol(SymbolKind.make_target, "help", "help", file="docs/Makefile")
    index = CodeIndex(symbols=[target], source_paths=["docs/Makefile"])
    assert _command(pattern.parent, "make html", index).verdict == Verdict.unverifiable
    # Without a runner file the target is still reported.
    assert _command(tmp_path / "docs", "just missing", CodeIndex()).verdict == Verdict.invalid_command


def test_prose_extracted_identifiers_report_lower_confidence(tmp_path: Path) -> None:
    index = CodeIndex(
        symbols=[_symbol(SymbolKind.env_var, "APP_HOME", "APP_HOME")], coverage={"env_var:python": "supported"}
    )
    # A supported surface reports absence only with evidence of a specific name: APP_HOMES is one
    # character from the declared APP_HOME. Prose extraction stays weaker than inline code.
    supported = match_claims([_claim(ClaimKind.env_var, "APP_HOMES")], index, tmp_path)[0]
    prose = match_claims(
        [_claim(ClaimKind.env_var, "APP_HOMES", extraction=Extraction.prose_pattern)], index, tmp_path
    )[0]
    assert supported.verdict == prose.verdict == Verdict.missing
    assert supported.confidence >= GATE > prose.confidence
