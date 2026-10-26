"""JavaScript CLI frameworks seen in the popular-repository sweep.

Minimal offline reproductions of sindresorhus/np (meow) and vercel/serve (arg), each paired with a case
that must still be reported.
"""

import json
from pathlib import Path

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.models import Symbol, SymbolKind
from docs_drift.scanning import run_scan

NP_CLI = """#!/usr/bin/env node
import meow from 'meow';

const cli = meow(`
\tUsage
\t  $ np <version>

\tCommands
\t  release-draft          Open a prefilled GitHub release form

\tOptions
\t  --any-branch           Allow publishing from any branch
`, {
\timportMeta: import.meta,
\tflags: {
\t\tanyBranch: {
\t\t\ttype: 'boolean',
\t\t},
\t\treleaseNotes: {
\t\t\ttype: 'boolean',
\t\t},
\t\ttag: {
\t\t\ttype: 'string',
\t\t},
\t\ttestScript: {
\t\t\ttype: 'string',
\t\t\tdefault: 'test',
\t\t},
\t\tdryRun: {
\t\t\ttype: 'boolean',
\t\t\taliases: ['preview'],
\t\t\tshortFlag: 'd',
\t\t},
\t\t'2fa': {
\t\t\ttype: 'boolean',
\t\t},
\t},
});

if (cli.input.at(0) === 'release-draft') {
\tconsole.log('draft');
}
"""

SERVE_CLI = """import parseArgv from 'arg';

const parseEndpoint = (value: string) => value;

const options = {
  '--help': Boolean,
  '--version': Boolean,
  '--listen': [parseEndpoint] as [typeof parseEndpoint],
  '--no-clipboard': Boolean,
  '-h': '--help',
  '-v': '--version',
  '-l': '--listen',
  '-p': '--listen',
};

export const parseArguments = () => parseArgv(options);
"""


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _np(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    package = {"name": "np", "bin": "./source/cli.js", "dependencies": {"meow": "^14", "cosmiconfig": "^10"}}
    return _repo(
        tmp_path,
        {"package.json": json.dumps(package), "source/cli-implementation.js": NP_CLI, **(extra or {})},
    )


def _flags(symbols: list[Symbol]) -> dict[str, Symbol]:
    return {s.name: s for s in symbols if s.kind == SymbolKind.cli_flag}


def test_meow_flags_become_kebab_flags_with_negations_and_aliases(tmp_path: Path) -> None:
    flags = _flags(build_index(_np(tmp_path), Config(), no_cache=True).symbols)
    assert {"--any-branch", "--release-notes", "--tag", "--test-script", "--dry-run", "--2fa"} <= set(flags)
    assert "--no-release-notes" in flags["--release-notes"].aliases
    assert "--no-2fa" in flags["--2fa"].aliases
    assert {"--preview", "-d", "--no-dry-run"} <= set(flags["--dry-run"].aliases)
    # Only booleans negate.
    assert "--no-tag" not in flags["--tag"].aliases
    assert flags["--test-script"].default == "test"
    # meow always answers --help and --version.
    assert flags["--help"].derived and flags["--version"].derived


def test_meow_help_commands_and_input_dispatch_are_commands(tmp_path: Path) -> None:
    symbols = build_index(_np(tmp_path), Config(), no_cache=True).symbols
    commands = [s for s in symbols if s.kind == SymbolKind.cli_command and s.name == "release-draft"]
    assert len(commands) == 1 and commands[0].parent_id is not None
    arguments = [s for s in symbols if s.kind == SymbolKind.cli_argument]
    assert [(s.name, s.qualified_name) for s in arguments] == [("version", "source.cli-implementation.root.version")]


def test_meow_flag_names_are_config_keys_when_a_config_loader_is_used(tmp_path: Path) -> None:
    symbols = build_index(_np(tmp_path), Config(), no_cache=True).symbols
    keys = {s.name: s for s in symbols if s.kind == SymbolKind.config_key and s.framework == "meow-config"}
    assert {"testScript", "anyBranch", "releaseNotes"} <= set(keys)
    assert not keys["testScript"].authoritative


def test_meow_flags_are_not_config_keys_without_a_config_loader(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "package.json": json.dumps({"name": "np", "bin": "cli.js", "dependencies": {"meow": "^14"}}),
            "cli.js": NP_CLI,
        },
    )
    symbols = build_index(repo, Config(), no_cache=True).symbols
    assert not [s for s in symbols if s.kind == SymbolKind.config_key and s.framework == "meow-config"]


def test_arg_spec_keys_are_flags_and_string_values_are_aliases(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "package.json": json.dumps({"name": "serve", "bin": {"serve": "./build/main.js"}}),
            "source/utilities/cli.ts": SERVE_CLI,
        },
    )
    flags = _flags(build_index(repo, Config(), no_cache=True).symbols)
    assert set(flags) == {"--help", "--version", "--listen", "--no-clipboard"}
    assert flags["--help"].aliases == ["-h"]
    assert set(flags["--listen"].aliases) == {"-l", "-p"}
    # arg has no implicit negation: `--no-clipboard` is the literal flag.
    assert flags["--no-clipboard"].aliases == []


def test_commander_and_yargs_gain_implicit_help_flags(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "package.json": json.dumps({"name": "tool", "bin": {"tool": "cli.js"}}),
            "cli.js": "const { Command } = require('commander');\nconst p = new Command();\n"
            "p.version('1.0.0');\np.option('--dry-run', 'dry run');\n",
            "other.js": "const yargs = require('yargs');\nyargs(process.argv).help(false).argv;\n",
        },
    )
    symbols = build_index(repo, Config(), no_cache=True).symbols
    by_file: dict[str, set[str]] = {}
    for item in symbols:
        if item.kind == SymbolKind.cli_flag:
            by_file.setdefault(item.file, set()).add(item.name)
    assert {"--help", "--version", "--dry-run"} <= by_file["cli.js"]
    assert "--help" not in by_file.get("other.js", set())
    assert "--version" in by_file["other.js"]


def test_documented_meow_and_arg_flags_resolve_and_unknown_flags_remain(tmp_path: Path) -> None:
    repo = _np(
        tmp_path,
        {
            "readme.md": (
                "# np\n\n"
                "```sh\nnp --help\nnp --any-branch --tag=beta\nnp patch --any-branch\nnp --skip-everything\n```\n\n"
                "Use `--no-release-notes` to skip notes, `--any-branch` to publish from any "
                "branch, `--preview` to dry run and `--test-script` to pick a script.\n\n"
                "The `--skip-everything` flag does not exist.\n"
            ),
        },
    )
    result = run_scan(repo, Config(), history=False, no_cache=True)
    verdicts = {(f.claim.kind.value, f.claim.value): f.verdict.value for f in result.findings}
    for flag in ("--help", "--no-release-notes", "--any-branch", "--preview", "--test-script"):
        assert verdicts.get(("cli_flag", flag)) == "ok", (flag, verdicts)
    assert verdicts[("code_command", "np --help")] == "ok"
    assert verdicts[("code_command", "np --any-branch --tag=beta")] == "ok"
    # `patch` fills the `<version>` positional from the usage line; it is not an unknown subcommand.
    assert verdicts[("code_command", "np patch --any-branch")] == "ok"
    assert verdicts[("cli_flag", "--skip-everything")] == "missing"
    assert verdicts[("code_command", "np --skip-everything")] == "invalid_command"
