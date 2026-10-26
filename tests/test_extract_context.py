"""Regression cases from the 12-repository sweep: mentions the project does not own become unverifiable.

Each snippet is a minimal, offline reproduction of a hand-labelled false positive. The
claims stay in the report (so reviewers can see them) but carry an ``unverifiable:``
qualifier, and matching never reports them as missing.
"""

import hashlib
from pathlib import Path

from docs_drift.config import Config
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.pipeline import scan_claims
from docs_drift.docs.qualifiers import CONTEXT_PREFIX, context_finding
from docs_drift.docs.sections import parse_document
from docs_drift.models import Claim, ClaimKind, DocFile, DocFormat, Verdict
from docs_drift.scanning import run_scan

RST = Config(docs=["docs/**/*.rst"])


def extract(text: str, *, names: frozenset[str] = frozenset()) -> list[Claim]:
    doc = DocFile(path="README.md", content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    return extract_claims(parse_document(doc, text), project_names=names).claims


def qualifier_of(claims: list[Claim], value: str) -> str | None:
    return next(claim.qualifier for claim in claims if claim.value == value)


def test_context_finding_is_unverifiable_and_ignores_ordinary_qualifiers() -> None:
    claim = extract("Pass `--ssl-keyfile` to Uvicorn.")[0]
    finding = context_finding(claim)
    assert finding is not None and finding.verdict == Verdict.unverifiable
    assert "uvicorn" in finding.reasons[0]
    assert context_finding(extract("The `serve` subcommand accepts `--port`.")[-1]) is None


def test_flags_attributed_to_another_tool_in_the_same_sentence() -> None:
    # encode/httpx docs/advanced/ssl.md:83
    claims = extract(
        "For example, [Uvicorn](https://www.uvicorn.org) provides the `--ssl-keyfile` and `--ssl-certfile` options."
    )
    assert qualifier_of(claims, "--ssl-keyfile") == f"{CONTEXT_PREFIX}third-party:uvicorn"
    assert qualifier_of(claims, "--ssl-certfile") == f"{CONTEXT_PREFIX}third-party:uvicorn"


def test_list_items_inherit_the_tool_named_by_their_lead_in() -> None:
    # fastapi/typer docs/tutorial/options/name.md:145
    claims = extract("You can pass these short names to `tar`:\n\n* `-x`: extract.\n* `-v`: verbose.\n")
    assert qualifier_of(claims, "-x") == f"{CONTEXT_PREFIX}third-party:tar"


def test_section_about_another_program_owns_its_flags() -> None:
    # fastapi/typer docs/tutorial/options/name.md:105 (`ls -sh`)
    text = "## Short names\n\nFor example, the `ls` program has these options:\n\nSo you can use `-sh` together.\n"
    assert qualifier_of(extract(text), "-sh") == f"{CONTEXT_PREFIX}third-party:ls"


def test_preceding_tool_mention_in_the_paragraph_owns_later_flags() -> None:
    # psf/black docs/guides/introducing_black_to_your_project.md:14 (`git blame`)
    text = (
        "Migration clutters the output of `git blame`. Since Git 2.23 you can\n"
        "pass a file listing revisions with the `--ignore-revs-file` option.\n"
    )
    assert qualifier_of(extract(text), "--ignore-revs-file") == f"{CONTEXT_PREFIX}third-party:git"


def test_a_tool_mentioned_after_the_flag_does_not_claim_it() -> None:
    # tests/fixtures clean-repo: "The `--workers` default is `8`. ... Run `make test`."
    claims = extract("The `--workers` default is `8`. Run `make test`.\n")
    assert qualifier_of(claims, "--workers") is None


def test_the_project_name_in_the_sentence_wins_over_tool_words() -> None:
    claims = extract("Run `mytool --port 8000` behind nginx, or pass `--port` to mytool.", names=frozenset({"mytool"}))
    assert qualifier_of(claims, "--port") is None


def test_rich_cli_real_case_flags_stay_checkable() -> None:
    # docs/REAL-CASE.md: README.md:61 and README.md:180 must still be reported.
    text = (
        "Add the `--line-number` or `-n` switch to enable line numbers.\n\n"
        "If you add the `--print` or `--p` option then Rich will treat the first argument as "
        "[console markup](https://rich.readthedocs.io/en/latest/markup.html).\n"
    )
    claims = extract(text, names=frozenset({"rich", "rich-cli", "rich_cli"}))
    assert qualifier_of(claims, "--line-number") is None
    assert qualifier_of(claims, "--p") is None


def test_placeholder_and_externally_linked_names_are_unverifiable() -> None:
    # fastapi/typer bool.md:3 and encode/uvicorn logging.md:19 (link to docs.python.org)
    claims = extract(
        "A flag like `--something` or `--no-something`.\n\n"
        "Uvicorn uses [`dictConfig()`](https://docs.python.org/3/library/logging.config.html) for this function.\n"
    )
    assert qualifier_of(claims, "--something") == f"{CONTEXT_PREFIX}placeholder"
    assert qualifier_of(claims, "dictConfig") == f"{CONTEXT_PREFIX}external-link:docs.python.org"


def test_names_defined_by_the_docs_own_example_code(tmp_path: Path) -> None:
    # pallets/click option-decorators.md:50, parameters.md:73, commands-and-groups.md:302; flask cli.rst:405
    (tmp_path / "README.md").write_text(
        "# Confirmation\n\nAdd a boolean `--yes` flag.\n\n"
        "```python\n@click.command()\n@click.option('--yes', is_flag=True)\ndef dropdb():\n    pass\n```\n\n"
        "| decls | `@click.option('-g', '--gluten-free/--no-gluten-free')` |\n|---|---|\n| x | y |\n\n"
        "Pass `--gluten-free` to enable it.\n\n"
        "With `auto_envvar_prefix` set to `WEB`, the variable is `WEB_RUN_SERVER_HOST`.\n",
        encoding="utf-8",
    )
    claims = scan_claims(tmp_path, Config()).claims
    assert qualifier_of(claims, "--yes") == f"{CONTEXT_PREFIX}doc-example"
    assert qualifier_of(claims, "--gluten-free") == f"{CONTEXT_PREFIX}doc-example"
    assert qualifier_of(claims, "WEB_RUN_SERVER_HOST") == f"{CONTEXT_PREFIX}generated-env:auto_envvar_prefix"


def test_rst_literal_block_routes_and_assignments_are_local(tmp_path: Path) -> None:
    # pallets/flask api.rst:599 and patterns/fileuploads.rst:33
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "routing.rst").write_text(
        "Routing\n=======\n\nA definition for a URL with an optional page::\n\n"
        "    @app.route('/users/page/<int:page>')\n    def show_users(page):\n        pass\n\n"
        "    UPLOAD_FOLDER = '/path/to/the/uploads'\n\n"
        "Here ``/users/page/1`` redirects, and ``UPLOAD_FOLDER`` stores uploads.\n",
        encoding="utf-8",
    )
    claims = scan_claims(tmp_path, RST).claims
    assert qualifier_of(claims, "/users/page/1") == f"{CONTEXT_PREFIX}doc-example"
    assert qualifier_of(claims, "UPLOAD_FOLDER") == f"{CONTEXT_PREFIX}doc-example"


def test_data_directive_names_are_configuration_keys_not_env_vars(tmp_path: Path) -> None:
    # pallets/flask config.rst:109 (TRAP_HTTP_EXCEPTIONS is a config key documented by `.. py:data::`)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "config.rst").write_text(
        "Config\n======\n\n.. py:data:: TRAP_HTTP_EXCEPTIONS\n\n    Enable this to trap errors.\n\n"
        "This is more specific than ``TRAP_HTTP_EXCEPTIONS``.\n",
        encoding="utf-8",
    )
    kinds = {claim.kind for claim in scan_claims(tmp_path, RST).claims if claim.value == "TRAP_HTTP_EXCEPTIONS"}
    assert kinds == {ClaimKind.config_key}


def test_doc_local_definitions_do_not_hide_project_flags(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Usage\n\n```python\n@click.option('--yes')\ndef demo():\n    pass\n```\n\nThe tool accepts `--port`.\n",
        encoding="utf-8",
    )
    assert qualifier_of(scan_claims(tmp_path, Config()).claims, "--port") is None


def test_contextual_claims_are_unverifiable_end_to_end_while_real_drift_remains(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text(
        "import argparse\np = argparse.ArgumentParser(prog='mytool')\np.add_argument('--listen')\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "# Usage\n\nStart `mytool` with `--port`.\n\nFor TLS, Uvicorn provides the `--ssl-keyfile` option.\n",
        encoding="utf-8",
    )
    findings = {
        finding.claim.value: finding.verdict for finding in run_scan(tmp_path, Config(), history=False).findings
    }
    assert findings["--port"] in {Verdict.missing, Verdict.renamed}
    assert findings["--ssl-keyfile"] == Verdict.unverifiable
