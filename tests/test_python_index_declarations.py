"""Python declarations the sweep of popular repositories showed were missed, as offline synthetic snippets.

Each case reproduces the shape of a real false positive (httpie, twine, flask, uvicorn, black, llm,
datasette, typer, click) and checks that genuine drift next to it is still reported.
"""

from pathlib import Path
from textwrap import dedent

import pytest

from docs_drift.config import Config
from docs_drift.ids import make_claim_id, make_section_id
from docs_drift.index import build_index
from docs_drift.index.base import CodeIndex
from docs_drift.index.python_routes import regex_template
from docs_drift.match.engine import match_claims
from docs_drift.models import Claim, ClaimKind, Extraction, Finding, Symbol, SymbolKind, Verdict


def _index(tmp_path: Path, files: dict[str, str]) -> CodeIndex:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text), encoding="utf-8")
    return build_index(tmp_path, Config(), no_cache=True)


def _claim(kind: ClaimKind, value: str, qualifier: str | None = None) -> Claim:
    return Claim(
        claim_id=make_claim_id("README.md", ["Usage"], kind.value, value),
        kind=kind,
        raw_text=f"`{value}`",
        value=value,
        qualifier=qualifier,
        doc_path="README.md",
        line=4,
        column=1,
        section_id=make_section_id("README.md", ["Usage"]),
        extraction=Extraction.inline_code,
        extraction_confidence=0.95,
    )


def _match(index: CodeIndex, repo: Path, kind: ClaimKind, value: str, qualifier: str | None = None) -> Finding:
    return match_claims([_claim(kind, value, qualifier)], index, repo)[0]


def _flags(index: CodeIndex) -> dict[str, Symbol]:
    return {
        name: symbol for symbol in index.by_kind.get(SymbolKind.cli_flag, []) for name in (symbol.name, *symbol.aliases)
    }


# Group: argparse flags added through wrappers, helpers, groups and dict specs.


def test_argparse_flags_through_helpers_wrapper_groups_and_dict_specs(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            # twine: flags registered on a parser passed into a static helper.
            "settings.py": """
                import argparse
                class Settings:
                    @staticmethod
                    def register_argparse_arguments(parser: argparse.ArgumentParser) -> None:
                        parser.add_argument("-r", "--repository", env="TWINE_REPOSITORY", default="pypi")
                        parser.add_argument("--config-file", default="~/.pypirc")
            """,
            # httpie: a custom ParserSpec whose groups expose add_argument.
            "definition.py": """
                from options import ParserSpec
                options = ParserSpec("http")
                output_options = options.add_group("Output options")
                output_options.add_argument("--output", "-o", dest="output_file")
                output_options.add_argument(dest="url")
            """,
            # Plain argparse argument groups and BooleanOptionalAction.
            "cli.py": """
                import argparse
                parser = argparse.ArgumentParser(prog="tool")
                network = parser.add_argument_group("network")
                network.add_argument("--timeout", type=int)
                parser.add_argument("--color", action=argparse.BooleanOptionalAction)
            """,
            # httpie manager: a data-driven COMMANDS spec turned into add_argument calls in a loop.
            "manager.py": """
                COMMANDS = {"export-args": [{"flags": ["-f", "--format"], "default": "json"}]}
            """,
        },
    )
    flags = _flags(index)
    assert {"-r", "--repository", "--config-file", "--output", "-o", "--timeout", "--format", "-f"} <= set(flags)
    assert {"--color", "--no-color", "--help", "-h"} <= set(flags)
    assert ".root." in flags["--repository"].qualified_name
    # A positional of an unknown owner is not attributed to any command.
    assert not any(symbol.name == "url" for symbol in index.by_kind.get(SymbolKind.cli_argument, []))
    assert any(symbol.name == "TWINE_REPOSITORY" for symbol in index.by_kind[SymbolKind.env_var])
    finding = _match(index, tmp_path, ClaimKind.cli_flag, "-r", qualifier="upload")
    assert finding.verdict == Verdict.ok
    # Genuine drift beside the helpers is still reported.
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--repository-name").verdict in {
        Verdict.missing,
        Verdict.renamed,
    }


def test_generated_no_flag_is_unverifiable_but_unrelated_missing_flag_is_reported(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {"definition.py": 'group = spec.add_group("Sessions")\ngroup.add_argument("--session")\n'},
    )
    negated = _match(index, tmp_path, ClaimKind.cli_flag, "--no-session")
    assert negated.verdict == Verdict.unverifiable
    assert "--session" in negated.reasons[0]
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--no-cookies").is_drift


# Group: click/Typer declarations (boolean pairs, Option objects, factories, built-in options, implicit params).


def test_click_boolean_pairs_option_objects_factories_and_builtin_options(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "main.py": """
                import click
                CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])

                @click.command(context_settings=CONTEXT_SETTINGS)
                @click.option("--proxy-headers/--no-proxy-headers", default=True)
                @click.option("--fast/--safe", is_flag=True)
                @click.option(" /--dry", "dry_run")
                @click.version_option()
                def main(proxy_headers, fast, dry_run):
                    pass
            """,
            "flask_cli.py": """
                import click
                _debug_option = click.Option(["--debug/--no-debug"], expose_value=False)
                _app_option = click.Option(["-A", "--app"], metavar="IMPORT")
            """,
            "llm_cli.py": """
                import click
                def schema_option(fn):
                    click.option("schema_input", "--schema", help="Schema DSL")(fn)
                    return fn

                def tool_options(fn):
                    for decorator in (click.option("tools_debug", "--td", "--tools-debug", envvar="LLM_TOOLS_DEBUG"),):
                        fn = decorator(fn)
                    return fn
            """,
        },
    )
    flags = _flags(index)
    expected = {"--proxy-headers", "--no-proxy-headers", "--fast", "--safe", "--dry", "--version", "-h", "--help"}
    assert expected <= set(flags)
    assert {"--debug", "--no-debug", "--app", "-A", "--schema", "--td", "--tools-debug"} <= set(flags)
    assert "schema_input" not in flags
    assert any(symbol.name == "LLM_TOOLS_DEBUG" for symbol in index.by_kind[SymbolKind.env_var])
    # Module-level options are inherited by every subcommand claim.
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--debug", qualifier="run").verdict == Verdict.ok
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--safe").verdict == Verdict.ok
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--turbo").is_drift


def test_rich_cli_real_drift_survives_wider_click_indexing(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "rich_cli/__main__.py": """
                import click
                @click.command()
                @click.argument("resource", metavar="<PATH or TEXT or '-'>", default="")
                @click.option("--print", "-p", "_print", is_flag=True)
                @click.option("--line-numbers", "-n", is_flag=True)
                @click.option("--wrap/--no-wrap", default=True)
                @click.version_option()
                def main(resource, _print, line_numbers, wrap):
                    pass
            """
        },
    )
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--line-number").is_drift
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--p").is_drift
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--no-wrap").verdict == Verdict.ok


def test_click_command_names_follow_click_and_scope_their_flags(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "cli.py": """
                import click
                @click.group()
                def cli():
                    pass

                @cli.command()
                @click.option("--batch-size", type=int)
                def embed_multi(batch_size):
                    pass

                @cli.command()
                def init_data_command():
                    pass

                def register_commands(cli):
                    @cli.group(name="openai")
                    def openai_():
                        pass

                    @openai_.command()
                    @click.option("--key")
                    def endpoint(key):
                        pass

                cli.add_command(init_data_command, name="seed")
            """
        },
    )
    commands = {symbol.name for symbol in index.by_kind[SymbolKind.cli_command]}
    assert {"embed-multi", "init-data", "openai", "endpoint", "seed"} <= commands
    batch = _flags(index)["--batch-size"]
    assert ".embed-multi." in batch.qualified_name
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--batch-size", qualifier="embed-multi").confidence == 0.95


def test_typer_implicit_parameters_bool_negations_and_off_only_declarations(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "tutorial001.py": """
                import typer
                app = typer.Typer()

                @app.command()
                def main(name: str, age: int = 20, height_meters: float = 1.89, female: bool = True):
                    pass
            """,
            "tutorial004.py": """
                import typer
                app = typer.Typer()

                @app.command()
                def main(in_prod: bool = typer.Option(True, " /--demo", " /-d"), force: bool = typer.Option(False)):
                    pass
            """,
            "first_steps.py": """
                import typer

                def main(ctx: typer.Context, name: str, lastname: str = "", formal: bool = False):
                    pass

                if __name__ == "__main__":
                    typer.run(main)
            """,
        },
    )
    flags = _flags(index)
    assert {"--age", "--height-meters", "--female", "--no-female", "--demo", "-d"} <= set(flags)
    assert {"--force", "--no-force", "--lastname", "--formal", "--no-formal"} <= set(flags)
    assert "--no-demo" not in flags and "--no-age" not in flags and "--ctx" not in flags
    arguments = {symbol.name for symbol in index.by_kind[SymbolKind.cli_argument]}
    assert "name" in arguments
    assert flags["--age"].default == "20"
    assert _match(index, tmp_path, ClaimKind.cli_flag, "--weight").is_drift


# Group: symbol index coverage (class attributes, enum members, annotated constants, params, re-exports).


def test_attributes_enum_members_variables_parameters_reexports_and_submodules(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "pkg/__init__.py": "from .hookspecs import hookimpl\nfrom json import dumps\n",
            "pkg/hookspecs.py": "import pluggy\nhookimpl = pluggy.HookimplMarker('pkg')\n",
            "pkg/tracer.py": "def trace():\n    pass\n",
            "pkg/mode.py": """
                from dataclasses import dataclass
                from enum import Enum, auto

                class Preview(Enum):
                    fmt_off_class_blank_lines = auto()

                class Auth:
                    requires_request_body = False

                    def __init__(self, token: str, *, flag_value: object = None) -> None:
                        self.token = token

                @dataclass
                class ToolCall:
                    tool_call_id: str | None = None

                class Sentinel(Enum):
                    UNSET = object()

                UNSET: Sentinel = Sentinel.UNSET
            """,
        },
    )

    def verdict(value: str) -> Verdict:
        return _match(index, tmp_path, ClaimKind.symbol, value).verdict

    for value in (
        "fmt_off_class_blank_lines",
        "requires_request_body",
        "tool_call_id",
        "token",
        "flag_value",
        "UNSET",
        "Preview.fmt_off_class_blank_lines",
        "pkg.hookimpl",
        "pkg.tracer",
    ):
        assert verdict(value) == Verdict.ok, value
    attribute = next(s for s in index.by_kind[SymbolKind.attribute] if s.name == "requires_request_body")
    assert attribute.default == "False"
    # Third-party names re-exported by a package are not project API, and real drift stays visible.
    assert verdict("pkg.dumps") != Verdict.ok
    assert verdict("FileType") in {Verdict.missing, Verdict.renamed}


def test_config_dict_keys_are_indexed_as_configuration(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "app.py": """
                class ImmutableDict(dict):
                    pass

                class App:
                    default_config = ImmutableDict({"TRAP_HTTP_EXCEPTIONS": False, "SERVER_NAME": None})
                lookup = {"NOT_A_SETTING": 1}
            """
        },
    )
    keys = {symbol.name for symbol in index.by_kind[SymbolKind.config_key]}
    assert {"TRAP_HTTP_EXCEPTIONS", "SERVER_NAME"} <= keys
    assert "NOT_A_SETTING" not in keys


# Group: environment variables declared indirectly.


def test_env_vars_declared_through_keyword_arguments(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "settings.py": """
                import argparse
                from utils import EnvironmentDefault, EnvironmentFlag
                def register(parser):
                    parser.add_argument("-u", "--username", action=EnvironmentDefault, env="TWINE_USERNAME")
                    parser.add_argument("--non-interactive", action=EnvironmentFlag, env="TWINE_NON_INTERACTIVE")
                    parser.add_argument("--mode", env="production")
                import click
                verbose = click.option("--verbose", envvar=["APP_VERBOSE", "VERBOSE"])
            """
        },
    )
    names = {symbol.name for symbol in index.by_kind[SymbolKind.env_var]}
    assert {"TWINE_USERNAME", "TWINE_NON_INTERACTIVE", "APP_VERBOSE", "VERBOSE"} <= names
    assert "production" not in names
    assert _match(index, tmp_path, ClaimKind.env_var, "TWINE_USERNAME").verdict == Verdict.ok
    assert _match(index, tmp_path, ClaimKind.env_var, "TWINE_TOKEN").is_drift


def test_proxy_variables_read_through_urllib_getproxies(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {"utils.py": "import urllib.request\ndef proxies():\n    return urllib.request.getproxies()\n"},
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        assert _match(index, tmp_path, ClaimKind.env_var, name).verdict == Verdict.ok
    assert _match(index, tmp_path, ClaimKind.env_var, "SOCKS_PROXY_URL").is_drift
    bare = _index(tmp_path / "bare", {"utils.py": "def proxies():\n    return {}\n"})
    assert not bare.by_kind.get(SymbolKind.env_var)


# Group: subcommands registered dynamically.


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "pyproject.toml",
            """
            [project]
            name = "twine"
            [project.scripts]
            twine = "twine.__main__:main"
            [project.entry-points."twine.registered_commands"]
            check = "twine.commands.check:main"
            upload = "twine.commands.upload:main"
            [project.entry-points.pytest11]
            plugin = "twine.plugin"
            """,
        ),
        (
            "setup.cfg",
            """
            [metadata]
            name = twine
            [options.entry_points]
            console_scripts =
                twine = twine.__main__:main
            twine.registered_commands =
                check = twine.commands.check:main
                upload = twine.commands.upload:main
            pytest11 =
                plugin = twine.plugin
            """,
        ),
    ],
)
def test_entry_point_command_groups_register_subcommands(tmp_path: Path, name: str, text: str) -> None:
    index = _index(tmp_path, {name: text})
    commands = {symbol.name: symbol for symbol in index.by_kind.get(SymbolKind.cli_command, [])}
    assert {"upload", "check"} <= set(commands)
    assert "plugin" not in commands
    assert commands["upload"].parent_id is not None
    assert _match(index, tmp_path, ClaimKind.cli_command, "upload").verdict == Verdict.ok
    assert _match(index, tmp_path, ClaimKind.cli_command, "register").is_drift


# Group: regex routes, Starlette Route and add_route.


def test_regex_template_normalisation() -> None:
    assert regex_template(r"/(\.(?P<format>json))?$") == "/"
    assert regex_template(r"/(?P<database>[^\/\.]+)/(?P<table>[^\/\.]+)/-/autocomplete$") == (
        "/{database}/{table}/-/autocomplete"
    )
    assert regex_template(r"^/items/(?P<id>\d+)/$") == "/items/{id}/"


def test_regex_and_call_registered_routes_match_templates_and_concrete_urls(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        {
            "app.py": r"""
                from starlette.routing import Route

                class Datasette:
                    def routes(self):
                        routes = []

                        def add_route(view, regex):
                            routes.append((regex, view))

                        add_route(IndexView, r"/(\.(?P<format>json))?$")
                        add_route(TableView, r"/(?P<database>[^\/\.]+)/(?P<table>[^\/\.]+)(\.(?P<format>\w+))?$")
                        add_route(QueryView, r"/(?P<database>[^\/\.]+)/-/query(\.(?P<format>\w+))?$")
                        add_route(AutoView, r"/(?P<database>[^\/\.]+)/(?P<table>[^\/\.]+)/-/autocomplete$")
                        return routes

                routes = [Route("/health", endpoint=health, methods=["GET"])]
            """,
            # blackd: aiohttp route definitions.
            "blackd.py": """
                from aiohttp import web
                app = web.Application()
                app.add_routes([web.post("/format", handle)])
            """,
        },
    )

    def verdict(value: str) -> Verdict:
        return _match(index, tmp_path, ClaimKind.endpoint, value).verdict

    for value in (
        "/",
        "/<database>/<table>/-/autocomplete",
        "GET /fixtures/facetable.json?_extra=count,count_truncated",
        "GET /fixtures/-/query.json?sql=select+1+as+one&_extra=database",
        "GET /health",
        "POST /format",
    ):
        assert verdict(value) == Verdict.ok, value
    assert verdict("POST /health") == Verdict.missing
    assert verdict("GET /format") == Verdict.missing
    assert verdict("/fixtures/facetable/-/unknown/extra") in {Verdict.missing, Verdict.renamed}
