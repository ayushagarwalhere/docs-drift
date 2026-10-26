"""Regression cases from the 12-repository sweep for inline span classification.

Each case is a minimal reproduction of a hand-labelled false positive (repository and
line in the comment), plus the true positive that the same rule must keep.
"""

import hashlib

import pytest

from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.preprocess import rst_claims
from docs_drift.docs.sections import parse_document
from docs_drift.models import Claim, ClaimKind, DocFile, DocFormat


def extract(text: str, *, names: frozenset[str] = frozenset()) -> list[Claim]:
    doc = DocFile(path="README.md", content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    return extract_claims(parse_document(doc, text), project_names=names).claims


def rst(text: str) -> list[Claim]:
    doc = DocFile(path="docs/x.rst", content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.rst)
    return rst_claims(parse_document(doc, text))


def pairs(claims: list[Claim]) -> list[tuple[ClaimKind, str]]:
    return [(claim.kind, claim.value) for claim in claims]


# Group 3: the configuration noun must qualify the span itself.
@pytest.mark.parametrize(
    "text",
    [
        "Mount the ASGI application at a subpath by setting `root_path`.",  # encode/httpx transports.md:128
        "Use `pyproject.toml` to store the setting.",  # psf/black the_basics.md:322
        "Uploading `foo.png` sets the form field.",  # encode/httpx clients.md:319
        "You can install `mitmproxy` and change the setting.",  # encode/httpx contributing.md:192
        "The `lifespan` setting accepts `off`.",  # encode/uvicorn lifespan.md:103 (`off` is a value)
        "Disable it by setting `rich_markup_mode` to `None`.",  # fastapi/typer commands/help.md:215
    ],
)
def test_sentence_wide_config_nouns_do_not_make_config_keys(text: str) -> None:
    values = {claim.value for claim in extract(text) if claim.kind == ClaimKind.config_key}
    assert values <= {"lifespan"}


@pytest.mark.parametrize(
    "text",
    [
        "The `timeout_seconds` setting controls it.",
        "Set the config key `timeout_seconds`.",
        "The `timeout_seconds` field.",
    ],
)
def test_adjacent_config_nouns_still_make_config_keys(text: str) -> None:
    assert (ClaimKind.config_key, "timeout_seconds") in pairs(extract(text))


def test_project_name_is_not_its_own_config_key() -> None:
    # sindresorhus/np readme.md:312 ("a top-level `np` field in `package.json`")
    claims = extract("Configure it in a top-level `np` field in `package.json`.", names=frozenset({"np"}))
    assert all(claim.kind != ClaimKind.config_key for claim in claims)


# Group 4: language-level names are not project symbols; explicit roles are always checked.
@pytest.mark.parametrize(
    "text",
    [
        "The function returns `None` when cancelled.",  # pallets/click utils.md:211
        "Use `str(response.url)` if you need a string.",  # encode/httpx compatibility.md:46
        "The class method handles `HEAD` requests.",  # encode/uvicorn server-behavior.md:53
        "Call `print(name + 3)` to fail.",  # fastapi/typer exceptions.md:228
        "The method uses dynamic `import()` calls.",  # simonw/datasette javascript_plugins.rst:390
        "The function `fetch()` loads data.",  # pallets/flask patterns/javascript.rst:17
        "The function `alert()` shows a message.",
    ],
)
def test_builtins_keywords_and_protocol_words_are_not_symbols(text: str) -> None:
    assert all(claim.kind != ClaimKind.symbol for claim in extract(text))


def test_explicit_cross_reference_roles_are_checked_symbols() -> None:
    # pallets/click docs/exceptions.md:110, the sweep's one confirmed true positive.
    text = "Click raises {class}`FileType` if it encounters issues opening the file. See {ref}`options`."
    assert pairs(extract(text)) == [(ClaimKind.symbol, "FileType")]
    assert extract(text)[0].extraction_confidence == pytest.approx(0.9)
    assert pairs(extract("Overridden by {attr}`~Context.help_option_names`.")) == [
        (ClaimKind.symbol, "Context.help_option_names")
    ]
    assert extract("A {class}`str` value.") == []


def test_rst_default_value_expressions_are_not_symbols() -> None:
    # pallets/flask config.rst:231 "Default: ``timedelta(days=31)``"
    assert rst("Lifetime\n========\n\n    Default: ``timedelta(days=31)``\n") == []


# Group 5: user-machine, hypothetical and generic paths.
@pytest.mark.parametrize(
    ("text", "qualifier"),
    [
        ("Set it to `$PyInterpreterDirectory$/black`.", "non-checkout-path"),  # psf/black editors.md:142
        ("Send `multipart/form-data` requests.", "non-checkout-path"),  # httpie/cli README.md:1065
        ("Sessions live in `~/.config/httpie/sessions/<host>/<name>.json`.", "non-checkout-path"),
        ("The global config lives in `~/.config/`.", "non-checkout-path"),  # psf/black the_basics.md:528
        ("Copy that example and paste it to a file `main.py`.", "non-checkout-path"),  # typer options/help.md:25
        ("Create a file named `log_config.json` with:", "non-checkout-path"),  # uvicorn logging.md:127
        ("Deploy the script to `/etc/init.d/datasette` on the host.", "non-checkout-path"),  # datasette deploying
        ("For example, a `text.txt` file with:", "ambiguous-path"),  # typer parameter-types/file.md:30
        ("Let's say you have a script in `my_custom_script.py`.", "ambiguous-path"),  # typer typer-command.md:72
        ("Uploading doesn't require executing `setup.py`.", "ambiguous-path"),  # pypa/twine index.rst:54
        ("Add it to your project's top `__init__.py` file.", "ambiguous-path"),  # pallets/flask gevent.rst:28
        ("It reads a `HISTORY.md` file in the package directory.", "ambiguous-path"),  # sindresorhus/np readme
        ("Configure it in a `.np-config.json` file in the home directory.", "ambiguous-path"),  # np readme:112
    ],
)
def test_reader_owned_paths_are_not_checkout_claims(text: str, qualifier: str) -> None:
    claims = [
        claim for claim in extract(text, names=frozenset({"np", "datasette"})) if claim.kind == ClaimKind.file_path
    ]
    assert claims and all(claim.qualifier == qualifier for claim in claims)


def test_repository_paths_stay_checkable() -> None:
    claim = extract("The file `src/config.py` holds the settings.")[0]
    assert claim.kind == ClaimKind.file_path and claim.qualifier is None


def test_leading_dash_tokens_and_flag_pairs_are_not_paths() -> None:
    # pallets/click arguments.md:101 (`-foo.txt`) and simonw/llm embeddings/cli.md:473 (`-d/--database`)
    assert pairs(extract("To delete `-foo.txt`, use `--`.")) == []
    assert pairs(extract("Use `-d/--database` to choose a file.")) == [(ClaimKind.cli_flag, "--database")]
    assert pairs(extract("Use `--accept/--reject` instead.")) == [(ClaimKind.cli_flag, "--accept")]


def test_console_output_lines_are_not_paths() -> None:
    # fastapi/typer options/name.md:159 (`tar -xvf` output listing)
    claims = extract("```console\n$ tar -xvf myproject.tar.gz\nmyproject/intro.md\nmyproject/first-steps.md\n```\n")
    assert all(claim.kind != ClaimKind.file_path for claim in claims)


# Group 9: flag-token normalisation.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run with ``--host=0.0.0.0``.", [(ClaimKind.cli_flag, "--host")]),  # pallets/flask quickstart.rst:80
        ("Run with ``--app hello:app2``.", [(ClaimKind.cli_flag, "--app")]),  # pallets/flask cli.rst:47
        ("Words are separated by dashes (``-``).", []),  # pallets/flask extensiondev.rst:37
        ("Pass ``-5`` as the value.", []),
    ],
)
def test_rst_flag_literals_drop_values_and_reject_lone_dashes(text: str, expected: list[tuple[ClaimKind, str]]) -> None:
    assert pairs(rst(f"Title\n=====\n\n{text}\n")) == expected


def test_inline_flags_drop_metavars_and_tab_markers() -> None:
    # encode/uvicorn settings.md:149 (`--limit-concurrency <int>`) and typer callback-and-context.md:113
    assert pairs(extract("* `--limit-concurrency <int>` - Maximum connections.")) == [
        (ClaimKind.cli_flag, "--limit-concurrency")
    ]
    assert pairs(extract("Type `--[TAB][TAB]` to complete.")) == []
    assert pairs(extract("Use `-` to read standard input.")) == []  # simonw/llm usage.md:81


# Group 10: only environment-shaped names are environment variables.
@pytest.mark.parametrize(
    "text",
    [
        "Accessing `response.url` returns a `URL` instance.",  # encode/httpx compatibility.md:44
        "You can set `count=True` to count.",  # pallets/click options.md:277
        "Enable it with `include_results=True` here and set `include_results=True`.",  # simonw/llm openai-models
    ],
)
def test_non_environment_tokens_are_not_env_vars(text: str) -> None:
    assert all(claim.kind != ClaimKind.env_var for claim in extract(text))


def test_environment_shaped_set_pattern_still_yields_env_var() -> None:
    assert (ClaimKind.env_var, "APP_DEBUG") in pairs(extract("Set `APP_DEBUG=1` to enable debugging."))


# Groups 11 and 13 (inline parts).
def test_system_paths_are_not_endpoints_and_package_manager_builtins_are_not_scripts() -> None:
    assert all(claim.kind != ClaimKind.endpoint for claim in extract("Install it as `/etc/init.d/datasette`."))
    # vercel/serve contributing.md:94
    assert pairs(extract("Run `pnpm install` to update.")) == []
    assert pairs(extract("Run `pnpm run build` or `yarn lint`.")) == [
        (ClaimKind.package_script, "build"),
        (ClaimKind.package_script, "lint"),
    ]


def test_subcommand_classification_requires_adjacent_wording() -> None:
    # pallets/click commands-and-groups.md:301 ("a subcommand called `run` taking an option called `host`")
    claims = extract("If you have a subcommand called `run` taking an option called `host`, it works.")
    assert pairs(claims) == [(ClaimKind.cli_command, "run")]
