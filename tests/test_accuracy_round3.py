"""Regression cases from the round-2 review of 20 real repositories (12 earlier, 8 held out).

Each test reproduces one reviewed finding offline: either a false positive the default threshold
reported, or a true positive it hid. The repository and line are named in each test.
"""

import hashlib
import json
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.docs.defaults import extract_defaults, normalize_value
from docs_drift.docs.extract_inline import extract_claims, path_context
from docs_drift.docs.preprocess import rst_claims
from docs_drift.docs.sections import ParsedDoc, parse_document
from docs_drift.ids import make_claim_id, make_section_id, make_symbol_id
from docs_drift.index.base import CodeIndex
from docs_drift.match.rename import plausible_rename, rename_finding
from docs_drift.models import (
    Claim,
    ClaimKind,
    DocFile,
    DocFormat,
    Evidence,
    Extraction,
    Finding,
    Symbol,
    SymbolKind,
    Verdict,
)
from docs_drift.scanning import run_scan

THRESHOLD = Config().min_confidence


def parsed(text: str, path: str = "README.md") -> ParsedDoc:
    fmt = DocFormat.rst if path.endswith(".rst") else DocFormat.markdown
    return parse_document(DocFile(path=path, content_hash=hashlib.sha256(text.encode()).hexdigest(), format=fmt), text)


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def scan(root: Path, files: dict[str, str], config: Config | None = None) -> list[Finding]:
    write(root, files)
    return run_scan(root, config or Config(), history=False).findings


def reported(findings: list[Finding]) -> list[Finding]:
    return [
        item
        for item in findings
        if item.verdict not in {Verdict.ok, Verdict.unverifiable} and item.confidence >= THRESHOLD
    ]


def finding(findings: list[Finding], value: str, kind: ClaimKind | None = None) -> Finding:
    return next(item for item in findings if item.claim.value == value and (kind is None or item.claim.kind == kind))


RICH_CLI = (
    "import click\n\n\n"
    "@click.command()\n"
    '@click.argument("resource")\n'
    '@click.option("--print", "-p", "print", is_flag=True)\n'
    '@click.option("--text-full", "-F", "text_full", is_flag=True)\n'
    '@click.option("--force-terminal", "force_terminal", is_flag=True)\n'
    '@click.option("--line-numbers", "-n", "line_numbers", is_flag=True)\n'
    '@click.option("--guides", "-g", "guides", is_flag=True)\n'
    '@click.option("--continue", "-c", "cont", is_flag=True)\n'
    '@click.option("--cid", "--conversation", "cid")\n'
    "def main(resource, print, text_full, force_terminal, line_numbers, guides, cont, cid):\n"
    "    pass\n"
)
RICH_PYPROJECT = '[project]\nname = "rich-cli"\n\n[project.scripts]\nrich = "rich_cli.__main__:main"\n'


def rich_findings(root: Path, readme: str) -> list[Finding]:
    return scan(
        root,
        {
            "pyproject.toml": RICH_PYPROJECT,
            "src/rich_cli/__init__.py": "",
            "src/rich_cli/__main__.py": RICH_CLI,
            "README.md": readme,
        },
    )


# Recall: plain "missing" findings with strong evidence pass the default threshold.
def test_flag_near_miss_of_one_declared_option_is_reported(tmp_path: Path) -> None:
    # Textualize/rich-cli README.md:61 (`--line-number`; the option is --line-numbers)
    findings = rich_findings(tmp_path, "# Rich\n\nAdd the `--line-number` or `-n` switch to enable line numbers.\n")
    result = finding(findings, "--line-number")
    assert result.verdict == Verdict.missing
    assert result.confidence >= THRESHOLD
    assert result.suggestion == "--line-numbers"


def test_flag_near_miss_alone_is_reported_without_a_paired_alias(tmp_path: Path) -> None:
    findings = rich_findings(tmp_path, "# Rich\n\nAdd the `--line-number` switch to enable line numbers.\n")
    result = finding(findings, "--line-number")
    assert (result.verdict, result.suggestion) == (Verdict.missing, "--line-numbers")
    assert result.confidence >= THRESHOLD


def test_dash_typo_of_a_declared_short_option_is_reported(tmp_path: Path) -> None:
    # Textualize/rich-cli README.md:180 ("`--print` or `--p`"; the short option is -p)
    findings = rich_findings(tmp_path, "# Rich\n\nIf you add the `--print` or `--p` option then markup is used.\n")
    result = finding(findings, "--p")
    assert (result.verdict, result.suggestion) == (Verdict.missing, "-p")
    assert result.confidence >= THRESHOLD


def test_short_alias_of_a_different_option_is_reported(tmp_path: Path) -> None:
    # Textualize/rich-cli README.md:196 ("`--force-terminal` or `-F`"; -F is --text-full's alias)
    findings = rich_findings(tmp_path, "# Rich\n\nYou may want to add `--force-terminal` or `-F` to keep ansi codes.\n")
    assert finding(findings, "--force-terminal").verdict == Verdict.ok
    result = finding(findings, "-F")
    assert result.verdict == Verdict.missing
    assert result.confidence >= THRESHOLD
    assert "--text-full" in result.reasons[0]


def test_two_options_listed_together_are_not_an_alias_pair(tmp_path: Path) -> None:
    # simonw/llm usage.md:453 ("continue a conversation using `-c` or `--cid`": two options)
    findings = rich_findings(tmp_path, "# Rich\n\nContinue a conversation using `-c` or `--cid`.\n")
    assert reported(findings) == []


def test_unknown_flag_without_a_close_declared_name_stays_below_threshold(tmp_path: Path) -> None:
    # pypa/pipx comparisons.rst:165 (`--show-with` is uv's option; nothing close is declared)
    findings = rich_findings(tmp_path, "# Rich\n\nThe `--show-with` option lists extras.\n")
    result = finding(findings, "--show-with")
    assert result.verdict == Verdict.missing
    assert result.confidence < THRESHOLD


def test_explicit_class_role_for_an_undeclared_class_is_reported(tmp_path: Path) -> None:
    # pallets/click docs/exceptions.md:110 ({class}`FileType`; the class is click.File)
    files = {
        "src/click/__init__.py": "from .types import File\n",
        "src/click/types.py": "class File:\n    pass\n\n\nclass Path:\n    pass\n",
        "docs/exceptions.md": "# Exceptions\n\n- {class}`FileType` if Click encounters issues opening the file.\n",
    }
    result = finding(scan(tmp_path, files), "FileType")
    assert result.verdict == Verdict.missing
    assert result.confidence >= THRESHOLD


def test_role_naming_a_settings_key_is_not_evidence(tmp_path: Path) -> None:
    # Flask documents config keys with the data role (:data:`SESSION_COOKIE_NAME`); they are dict keys.
    files = {
        "src/app/__init__.py": "class Flask:\n    pass\n",
        "docs/config.md": "# Config\n\nSee {data}`SESSION_COOKIE_NAME` before changing it.\n",
    }
    findings = scan(tmp_path, files)
    assert all(item.claim.value != "SESSION_COOKIE_NAME" for item in reported(findings))


def test_rst_slash_pair_names_the_long_option() -> None:
    # simonw/datasette authentication.rst:1074 (``-e/--expires-after`` was read as one flag)
    doc = parsed("Tokens\n======\n\nYou can specify a ``-e/--expires-after`` option in seconds.\n", "docs/auth.rst")
    assert [claim.value for claim in rst_claims(doc) if claim.kind == ClaimKind.cli_flag] == ["--expires-after"]


def test_option_attributed_to_another_program_after_the_span_is_third_party() -> None:
    # tox-dev/tox reference/config.rst:1557 ("the ``-k`` or ``--keep-going`` option of GNU Make")
    text = (
        "Options\n=======\n\nWhen executing the commands keep going; tox will exit non-zero. It is analogous "
        "to the ``-k`` or ``--keep-going`` option of GNU Make.\n"
    )
    claims = rst_claims(parsed(text, "docs/config.rst"), project_names=frozenset({"tox"}))
    assert {claim.value: claim.qualifier for claim in claims if claim.kind == ClaimKind.cli_flag} == {
        "-k": "unverifiable:third-party:make",
        "--keep-going": "unverifiable:third-party:make",
    }


# Defaults.
UVICORN_MAIN = (
    "import click\n\n\n@click.command()\n"
    '@click.option("--log-level", type=str, default=None)\n'
    '@click.option("--forwarded-allow-ips", type=str, default=None)\n'
    '@click.option("--backlog", type=int, default=2048)\n'
    "def main(log_level, forwarded_allow_ips, backlog):\n    pass\n"
)


def test_none_code_default_is_a_runtime_fallback_not_a_contradiction(tmp_path: Path) -> None:
    # encode/uvicorn settings.md:88 (--log-level default=None; LOGGING_CONFIG makes it 'info')
    files = {
        "pyproject.toml": '[project]\nname = "uvicorn"\n\n[project.scripts]\nuvicorn = "uvicorn.main:main"\n',
        "uvicorn/__init__.py": "",
        "uvicorn/main.py": UVICORN_MAIN,
        "docs/settings.md": (
            "# Settings\n\n* `--log-level <str>` - Set the log level. **Default:** *'info'*.\n"
            "* `--backlog <int>` - Connections to hold. **Default:** *4096*.\n"
        ),
    }
    findings = [item for item in scan(tmp_path, files) if item.claim.kind == ClaimKind.default_value]
    assert finding(findings, "--log-level").verdict == Verdict.unverifiable
    # A literal default is still compared.
    assert finding(findings, "--backlog").verdict == Verdict.default_changed


def test_default_naming_an_environment_variable_if_set_is_descriptive() -> None:
    # encode/uvicorn settings.md:120 ("Defaults to `$FORWARDED_ALLOW_IPS` if set.")
    doc = parsed(
        "# HTTP\n\n* `--forwarded-allow-ips <list>` - IPs to trust. Defaults to `$FORWARDED_ALLOW_IPS` if set. "
        "Otherwise, `127.0.0.1` is trusted.\n"
    )
    assert extract_defaults(doc, extract_claims(doc).claims).claims == []


def test_parenthesised_default_stops_at_its_code_span() -> None:
    # httpie/cli docs/README.md:1238 ("(the default `0`, i.e., there's no limit)") and :1485 ("(default is `yes`)")
    doc = parsed(
        "# Limits\n\nThe `--max-headers=n` option limits headers read (the default `0`, i.e., there's no limit).\n\n"
        "## SSL\n\nTo skip verification, you can pass `--verify=no` (default is `yes`):\n"
    )
    defaults = {
        claim.value: claim.expected_default for claim in extract_defaults(doc, extract_claims(doc).claims).claims
    }
    assert defaults == {"--max-headers": "false", "--verify": "true"}


def test_default_line_belongs_to_the_enclosing_sphinx_declaration() -> None:
    # pallets/flask config.rst:178 (Default of SESSION_COOKIE_PATH was bound to APPLICATION_ROOT in its body)
    text = (
        "Configuration\n=============\n\n"
        ".. py:data:: SESSION_COOKIE_PATH\n\n"
        "    The path that the session cookie will be valid for. If not set, the cookie\n"
        "    will be valid underneath ``APPLICATION_ROOT`` or ``/`` if that is not set.\n\n"
        "    Default: ``None``\n"
    )
    doc = parsed(text, "docs/config.rst")
    claims = [*extract_claims(doc).claims, *rst_claims(doc)]
    assert [claim.value for claim in extract_defaults(doc, claims).claims] == []


def test_identifier_heading_default_is_not_bound_to_its_cli_namesake() -> None:
    # mkdocs/mkdocs configuration.md:721 ("### strict" ... "`--strict`" ... "**default**: `false`")
    doc = parsed(
        "# Configuration\n\n### strict\n\nDetermines how warnings are handled.\n\n"
        "This is also available as a command line flag: `--strict`.\n\n**default**: `false`\n"
    )
    assert extract_defaults(doc, extract_claims(doc).claims).claims == []


def test_identifier_heading_binds_its_own_default() -> None:
    # httpie/cli docs/README.md:2345 ("#### `default_options`" ... "(by default empty)")
    doc = parsed(
        "# Config\n\nCurrently, HTTPie offers a single configurable option, the `default_options` setting.\n\n"
        "#### `default_options`\n\nAn `Array` (by default empty) of default options.\n"
    )
    defaults = extract_defaults(doc, extract_claims(doc).claims).claims
    assert [(claim.value, claim.expected_default) for claim in defaults] == [("default_options", "none")]
    assert normalize_value("[]").equals(normalize_value("empty")) is True


def test_root_path_default_keeps_its_slash() -> None:
    # pallets/flask config.rst:312 (APPLICATION_ROOT "Default: ``'/'``" compared as an empty string)
    assert normalize_value("/").value == "/"
    assert normalize_value("'/'").equals(normalize_value("/")) is True
    assert normalize_value("/").equals(normalize_value("")) is False


# Config keys.
def test_entries_of_a_free_form_schema_map_are_not_renamed_keys(tmp_path: Path) -> None:
    # tox-dev/tox reference/config.rst:2809 (`set_env = { A = "1", B = "2"}` reported as renamed to set_env)
    schema = {
        "type": "object",
        "properties": {
            "env_run_base": {
                "type": "object",
                "properties": {
                    "set_env": {"type": "object", "additionalProperties": {"type": "string"}},
                    "commands": {"type": "array"},
                },
            }
        },
    }
    files = {
        "pyproject.toml": '[project]\nname = "tox"\n',
        "src/tox/tox.schema.json": json.dumps(schema),
        "docs/config.md": (
            '# Config\n\n```toml\n[tool.tox.env_run_base]\nset_env = { A = "1", B = "2"}\ncommands = []\n```\n'
        ),
    }
    findings = scan(tmp_path, files)
    assert reported(findings) == []
    assert finding(findings, "env_run_base.set_env.A").verdict == Verdict.ok


def _symbol(kind: SymbolKind, name: str) -> Symbol:
    return Symbol(
        symbol_id=make_symbol_id(kind, name, "src/models.py"),
        kind=kind,
        name=name,
        qualified_name=name,
        file="src/models.py",
        line=1,
        language="python",
    )


def _claim(kind: ClaimKind, value: str) -> Claim:
    section = make_section_id("docs/plugins.md", ["Plugins"])
    return Claim(
        claim_id=make_claim_id("docs/plugins.md", ["Plugins"], kind.value, value, 0),
        kind=kind,
        raw_text=value,
        value=value,
        doc_path="docs/plugins.md",
        line=3,
        column=1,
        section_id=section,
        extraction=Extraction.inline_code,
        extraction_confidence=0.72,
    )


def test_rename_never_proposes_an_ancestor_or_a_naming_style_change() -> None:
    # tox-dev/tox (set_env.A -> set_env) and simonw/llm advanced-model-plugins.md:361 (tool_result -> ToolResult)
    assert not plausible_rename("env_run_base.set_env.A", "env_run_base.set_env")
    assert not plausible_rename("tool_result", "ToolResult")
    assert plausible_rename("get_winter_size", "get_terminal_size")
    index = CodeIndex(symbols=[_symbol(SymbolKind.class_, "ToolResult")])
    claim = _claim(ClaimKind.symbol, "tool_result")
    evidence = [Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)]
    base = Finding(claim=claim, verdict=Verdict.missing, confidence=0.5, evidence=evidence, reasons=["missing"])
    assert rename_finding(claim, index, base).verdict == Verdict.missing


# Commands.
PIPX_MAIN = (
    "import argparse\n\n\n"
    "def _add_install(subparsers):\n"
    '    parser = subparsers.add_parser("install")\n'
    '    parser.add_argument("package_spec")\n'
    '    parser.add_argument("--pip-args")\n'
    "\n\n"
    "def _add_run(subparsers):\n"
    '    parser = subparsers.add_parser("run")\n'
    '    parser.add_argument("--python-args", metavar="ARGS")\n'
    '    parser.add_argument("--no-cache", action="store_true")\n'
    '    parser.add_argument("app_with_args", nargs=argparse.REMAINDER)\n'
    '    parser.add_argument("--python")\n'
    '    parser.add_argument("--pypackages", action="store_true")\n'
    "\n\n"
    "def get_command_parser():\n"
    '    parser = argparse.ArgumentParser(prog="pipx")\n'
    "    subparsers = parser.add_subparsers()\n"
    "    _add_install(subparsers)\n"
    "    _add_run(subparsers)\n"
    "    return parser\n"
)
PIPX_FILES = {
    "pyproject.toml": '[project]\nname = "pipx"\n\n[project.scripts]\npipx = "pipx.main:cli"\n',
    "src/pipx/__init__.py": "",
    "src/pipx/main.py": PIPX_MAIN,
}


def test_argparse_builders_scope_arguments_to_their_own_subcommand(tmp_path: Path) -> None:
    write(tmp_path, PIPX_FILES)
    result = run_scan(tmp_path, Config(docs=["README.md"]), history=False)
    arguments = {item.qualified_name: item.nargs for item in result.symbols if item.kind == SymbolKind.cli_argument}
    assert arguments["pipx.main.run.app_with_args"] == "..."
    assert "pipx.main.install.package_spec" in arguments


def test_quoted_option_values_and_pass_through_arguments_are_not_flags(tmp_path: Path) -> None:
    # pypa/pipx troubleshoot.rst:119, run-scripts.rst:44 and run-applications.rst:63
    readme = (
        "# Usage\n\n```console\n"
        "$ pipx install termpair --pip-args '--trusted-host files.pythonhosted.org --trusted-host pypi.org'\n"
        '$ pipx run --python-args "-X dev" my-script.py\n'
        "$ pipx run --no-cache pycowsay --colour\n"
        "$ pipx install black --bogus-flag\n"
        "```\n"
    )
    findings = scan(tmp_path, {**PIPX_FILES, "README.md": readme})
    commands = {item.claim.value: item for item in findings if item.claim.kind == ClaimKind.code_command}
    assert all(item.verdict == Verdict.ok for value, item in commands.items() if "--bogus-flag" not in value), commands
    # A real unknown flag of a resolved subcommand is still invalid.
    assert commands["pipx install black --bogus-flag"].verdict == Verdict.invalid_command


def test_example_whose_output_is_an_error_is_not_checked(tmp_path: Path) -> None:
    # pypa/pipx run-applications.rst:45 (the "Ambiguous arguments" example prints the parser's error)
    readme = (
        "# Ambiguous\n\n```console\n$ pipx install pycowsay --py\n"
        "pipx install: error: ambiguous option: --py could match --python, --pypackages\n```\n"
    )
    findings = scan(tmp_path, {**PIPX_FILES, "README.md": readme})
    assert finding(findings, "pipx install pycowsay --py").verdict == Verdict.unverifiable


# Paths.
def test_hedged_and_hypothetical_paths_are_not_checkout_claims() -> None:
    # pallets/flask testing.rst:133 and mkdocs/mkdocs writing-your-docs.md:120
    flask = "It can be useful to store files in a ``tests/resources`` folder."
    assert path_context(flask, "tests/resources") is not None
    mkdocs = "the source files for the above configuration would be located at `docs/index.md` and `docs/about.md`"
    assert path_context(mkdocs, "docs/about.md") is not None
    assert path_context("The configuration lives in `docs/about.md`.", "docs/about.md") is None


def test_template_paths_are_naming_patterns() -> None:
    # tox-dev/tox development.rst:28 (`docs/changelog/####.{breaking,deprecation,feature}.rst`)
    value = "docs/changelog/####.{breaking,deprecation,feature,bugfix,doc,packaging,contrib}.rst"
    assert path_context("Add a fragment named", value) == "non-checkout-path"


def test_link_text_naming_another_repository_is_not_a_path() -> None:
    # httpie/cli docs/packaging/README.md:24 ([`httpie/debian.httpie.io`](https://github.com/httpie/debian.httpie.io))
    doc = parsed(
        "# Packaging\n\nThe index lives in [`httpie/debian.httpie.io`](https://github.com/httpie/debian.httpie.io).\n"
        "Its source is [`src/app.py`](https://github.com/httpie/cli/blob/master/src/app.py).\n"
    )
    qualifiers = {claim.value: claim.qualifier for claim in extract_claims(doc).claims}
    assert qualifiers["httpie/debian.httpie.io"] == "non-checkout-path"
    assert qualifiers["src/app.py"] is None


def test_candidates_deduplicates_symbols_found_by_name_and_alias() -> None:
    flag = _symbol(SymbolKind.cli_flag, "--print").model_copy(update={"aliases": ["--print", "-p"]})
    index = CodeIndex(symbols=[flag])
    assert [item.name for item in index.candidates(SymbolKind.cli_flag, "--print")] == ["--print"]


# Held-out hunt (29 Python + 15 JS/TS repositories reviewed by other agents).
def test_inline_make_target_missing_from_a_static_makefile_is_reported(tmp_path: Path) -> None:
    # Textualize/textual CONTRIBUTING.md:104 (`make test-snapshot`; only test-snapshot-update exists)
    files = {
        "Makefile": "test:\n\tpytest\n\ntest-snapshot-update:\n\tpytest --snapshot-update\n",
        "CONTRIBUTING.md": "# Contributing\n\nCheck the output of `make test-snapshot` now looks correct.\n",
    }
    result = finding(scan(tmp_path, files), "test-snapshot")
    assert result.verdict == Verdict.missing
    assert result.confidence >= THRESHOLD


def test_make_target_behind_a_pattern_rule_stays_below_threshold(tmp_path: Path) -> None:
    files = {
        "Makefile": "test:\n\tpytest\n\n%:\n\t@echo $@\n",
        "CONTRIBUTING.md": "# Contributing\n\nRun `make html` to build the docs.\n",
    }
    assert reported(scan(tmp_path, files)) == []


def test_path_misspelling_one_checkout_file_is_reported(tmp_path: Path) -> None:
    # pyinstaller/pyinstaller tests/README.md:79 (test/requirements-library.txt; real tests/requirements-libraries.txt)
    files = {
        "tests/requirements-libraries.txt": "pytest\n",
        "tests/README.md": "# Tests\n\nInstall the file `test/requirements-library.txt` first.\n",
    }
    result = finding(scan(tmp_path, files, Config(docs=["tests/README.md"])), "test/requirements-library.txt")
    assert (result.verdict, result.suggestion) == (Verdict.missing, "tests/requirements-libraries.txt")
    assert result.confidence >= THRESHOLD


def test_bare_file_listed_with_siblings_of_one_directory_is_reported(tmp_path: Path) -> None:
    # aio-libs/aiohttp THREAT_MODEL.md:482 (`reader.py` / `reader_c.py` / `reader_py.py`; reader_c.py is gone)
    files = {
        "aiohttp/__init__.py": "",
        "aiohttp/_websocket/__init__.py": "",
        "aiohttp/_websocket/reader.py": "",
        "aiohttp/_websocket/reader_py.py": "",
        "THREAT_MODEL.md": "# Model\n\n- `reader.py` / `reader_c.py` / `reader_py.py` - frame reader file.\n",
    }
    findings = scan(tmp_path, files, Config(docs=["THREAT_MODEL.md"]))
    result = finding(findings, "reader_c.py")
    assert result.verdict == Verdict.missing
    assert result.confidence >= THRESHOLD


def test_tracked_build_package_is_indexed(tmp_path: Path) -> None:
    # pypa/build (the package lives in src/build/): its flags were all reported missing.
    import subprocess  # noqa: PLC0415 - only this test needs a Git checkout.

    write(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "build"\n\n[project.scripts]\npyproject-build = "build.__main__:main"\n'
            ),
            "src/build/__init__.py": "",
            "src/build/__main__.py": (
                "import argparse\n\n\ndef main():\n    parser = argparse.ArgumentParser()\n"
                '    parser.add_argument("--sdist", action="store_true")\n'
            ),
            "README.md": "# build\n\nPass `--sdist` to build a source distribution.\n",
        },
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    findings = run_scan(tmp_path, Config(), history=False).findings
    assert finding(findings, "--sdist").verdict == Verdict.ok


def test_default_named_by_a_flag_is_descriptive() -> None:
    # python-semantic-release docs/api/commands.rst:379 ("**Default:** ``--commit``" under --commit/--no-commit)
    doc = parsed("# Version\n\n## `--commit` / `--no-commit`\n\nWhether to commit. **Default:** `--commit`\n")
    assert extract_defaults(doc, extract_claims(doc).claims).claims == []


def test_npm_run_placeholders_and_document_defined_scripts_are_not_checked(tmp_path: Path) -> None:
    # semantic-release CONTRIBUTING.md:274 (`npm run <script-name>`) and vitest docs/guide/projects.md:193
    readme = (
        '# Testing\n\n```json\n{\n  "scripts": {\n    "test:unit": "vitest --project unit"\n  }\n}\n```\n\n'
        "```sh\nnpm run '<script-name>'\nnpm run test:unit\nnpm run lint\n```\n"
    )
    files = {"package.json": '{"name": "demo", "scripts": {"build": "tsc"}}', "README.md": readme}
    commands = {
        item.claim.value: item.verdict for item in scan(tmp_path, files) if item.claim.kind == ClaimKind.code_command
    }
    assert commands["npm run '<script-name>'"] == Verdict.unverifiable
    assert commands["npm run test:unit"] == Verdict.unverifiable
    assert commands["npm run lint"] == Verdict.invalid_command


def test_triple_dash_separator_ends_option_parsing(tmp_path: Path) -> None:
    # plasma-umass/scalene README.md:102 (`scalene run your_prog.py --- --arg1 --arg2`)
    readme = "# Usage\n\n```console\n$ pipx install black --- --arg1 --arg2\n```\n"
    findings = scan(tmp_path, {**PIPX_FILES, "README.md": readme})
    assert finding(findings, "pipx install black --- --arg1 --arg2").verdict == Verdict.ok


def test_combined_repeated_and_glued_short_options_are_known(tmp_path: Path) -> None:
    # locust `-t10m`, tox `-vv`: argparse accepts a short option followed by its value or more short options.
    main = PIPX_MAIN.replace(
        'parser.add_argument("--pip-args")',
        'parser.add_argument("--pip-args")\n'
        '    parser.add_argument("-v", action="count")\n'
        '    parser.add_argument("-t")',
    )
    readme = "# Usage\n\n```console\n$ pipx install black -vv -t10m\n```\n"
    findings = scan(tmp_path, {**PIPX_FILES, "src/pipx/main.py": main, "README.md": readme})
    assert finding(findings, "pipx install black -vv -t10m").verdict == Verdict.ok


def test_placeholder_generated_and_foreign_paths_are_not_checkout_claims() -> None:
    # streamlink developing.rst:187, pypa/hatch build.md:145, vitest AGENTS.md:201, textual text_area.md:456,
    # plasma-umass/scalene CLAUDE.md:377
    assert path_context("Create the plugin file", "src/streamlink/plugins/pluginname.py") == "ambiguous-path"
    assert path_context("Include the file", "src/foo/file.ext") == "ambiguous-path"
    assert path_context("Edit the module file", "src/mypackage/__init__.py") == "ambiguous-path"
    generated = "`docs/.vitepress/contributor-names.json` is generated by `pnpm docs:contributors`"
    assert path_context(generated, "docs/.vitepress/contributor-names.json") == "non-checkout-path"
    foreign = "In these repos you can often find a file of node types at `src/node-types.json`."
    assert path_context(foreign, "src/node-types.json") == "non-checkout-path"
    create = "**Create provider module** (`scalene/scalene-gui/newprovider.ts`):"
    assert path_context(create, "scalene/scalene-gui/newprovider.ts") == "non-checkout-path"
    # A real repository file stays checkable.
    assert (
        path_context(
            "The parser lives in the file `src/streamlink/plugin/plugin.py`.", "src/streamlink/plugin/plugin.py"
        )
        is None
    )


def test_path_quoted_by_the_documents_own_example_code_is_the_readers(tmp_path: Path) -> None:
    # vitest docs/guide/snapshot.md:109 (`toMatchFileSnapshot('./test/basic.output.html')`)
    files = {
        "src/index.ts": "export const x = 1\n",
        "test/other.test.ts": "",
        "docs/snapshot.md": (
            "# Snapshot\n\n```ts\nawait expect(result).toMatchFileSnapshot('./test/basic.output.html')\n```\n\n"
            "It will compare with the content of the file `./test/basic.output.html`.\n"
        ),
    }
    assert finding(scan(tmp_path, files), "./test/basic.output.html").verdict == Verdict.unverifiable


def test_rename_rejects_dropping_a_word_and_changing_case_convention() -> None:
    # pypa/build how-it-works.rst:149 (get_requires_for_build_sdist -> get_requires_for_build)
    # jazzband/pip-tools configuration.md:38 (cache-dir -> CACHE_DIR)
    assert not plausible_rename("get_requires_for_build_sdist", "get_requires_for_build")
    assert not plausible_rename("cache-dir", "CACHE_DIR")
    assert plausible_rename("-sl", "--sl")


def test_slash_path_naming_a_repository_directory_is_checked_as_a_path(tmp_path: Path) -> None:
    # verdaccio CONTRIBUTING.md:130 (`/packages/ui-component`; the directory is packages/ui-components)
    files = {
        "package.json": '{"name": "verdaccio"}',
        "packages/ui-components/index.js": "export {}\n",
        "packages/core/index.js": "export {}\n",
        "scripts/build.js": "",
        "CONTRIBUTING.md": ("# Contributing\n\nThe UI lives in `/packages/ui-component` and helpers in `/scripts`.\n"),
    }
    findings = scan(tmp_path, files)
    result = finding(findings, "packages/ui-component")
    assert (result.verdict, result.suggestion) == (Verdict.missing, "packages/ui-components")
    assert result.confidence >= THRESHOLD
    assert finding(findings, "scripts").verdict == Verdict.ok


def test_index_cache_write_failure_is_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import docs_drift.index as index_module  # noqa: PLC0415

    def locked(path: Path, value: dict[str, object]) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(index_module, "_write_cache", locked)
    write(tmp_path, {"app.py": "def main():\n    pass\n"})
    result = index_module.build_index(tmp_path, Config())
    assert any("Index cache not written" in warning for warning in result.warnings)


def test_gitignored_paths_are_build_output_or_local(tmp_path: Path) -> None:
    # prettier website/README.md:41 (`static/playground/index.html` is built into an ignored directory)
    files = {
        ".gitignore": "/website/static/playground\nnode_modules/\n",
        "website/static/logo.png": "",
        "website/README.md": (
            "# Site\n\nThe entry point of the playground is at the file `static/playground/index.html`.\n"
        ),
    }
    result = finding(scan(tmp_path, files, Config(docs=["website/README.md"])), "static/playground/index.html")
    assert result.verdict == Verdict.unverifiable


def test_files_a_tutorial_will_create_are_the_readers_but_typos_still_show(tmp_path: Path) -> None:
    # Textualize/textual docs/how-to/package-with-hatch.md:94 (`src/textual_calculator/__init__.py` and the
    # misspelled `tests/__init__py`), :127 and :239 (copy/add files into the reader's package directory)
    files = {
        "src/textual/__init__.py": "",
        "tests/__init__.py": "",
        "docs/hatch.md": (
            "# Package\n\nThis will create the following files:\n\n"
            "- `src/textual_calculator/__init__.py`  and `tests/__init__py` mark Python packages.\n\n"
            "We will copy `calculator.py` into the `src/textual_calculator` directory.\n\n"
            "Add the following file to the `src/textual_calculator` folder.\n"
        ),
    }
    findings = scan(tmp_path, files)
    assert [item.claim.value for item in reported(findings)] == ["tests/__init__py"]
    assert finding(findings, "tests/__init__py").suggestion == "tests/__init__.py"


def test_default_under_a_double_backtick_rst_heading_binds_to_that_setting() -> None:
    # python-semantic-release configuration.rst:1246 (under ``signoff_commit``, bound to ``--no-commit``)
    text = (
        'Settings\n========\n\n``signoff_commit``\n""""""""""""""""""\n\n'
        "Adds a trailer. This option has no effect when the ``--no-commit`` option is passed.\n\n"
        "**Default:** ``false``\n"
    )
    doc = parsed(text, "docs/configuration.rst")
    claims = [*extract_claims(doc).claims, *rst_claims(doc)]
    assert [claim.value for claim in extract_defaults(doc, claims).claims] == []


def test_npm_run_without_package_scripts_or_for_the_readers_script_is_not_checked(tmp_path: Path) -> None:
    # vitest test/unit/src/wasm/wasm-bindgen-no-cyclic/README.md:10 and visual-regression-testing.md:856
    readme = "# Wasm\n\n```sh\nnpm run build\nnpm run test # Your test script that runs vitest\n```\n"
    files = {"package.json": '{"name": "hello_world", "version": "0.1.0"}', "README.md": readme}
    commands = {
        item.claim.value: item.verdict for item in scan(tmp_path, files) if item.claim.kind == ClaimKind.code_command
    }
    assert set(commands.values()) == {Verdict.unverifiable}
