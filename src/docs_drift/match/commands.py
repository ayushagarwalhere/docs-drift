"""Static, non-executing checks for project-owned shell command examples."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from configparser import ConfigParser
from pathlib import Path

from docs_drift.index.base import CodeIndex
from docs_drift.models import Claim, Evidence, Finding, SymbolKind, Verdict
from docs_drift.safe_files import metadata_text

MIN_WRAPPER_TOKENS = 3
MIN_SUBCOMMAND_TOKENS = 3


def _project_bins(repo: Path) -> set[str]:
    names: set[str] = set()
    pyproject = repo / "pyproject.toml"
    try:
        data = tomllib.loads(metadata_text(pyproject))
        scripts = data.get("project", {}).get("scripts", {})
        if isinstance(scripts, dict):
            names.update(str(name) for name in scripts)
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        pass
    package = repo / "package.json"
    try:
        data = json.loads(metadata_text(package))
        bin_value = data.get("bin", {})
        if isinstance(bin_value, str) and isinstance(data.get("name"), str):
            names.add(data["name"])
        if isinstance(bin_value, dict):
            names.update(str(name) for name in bin_value)
        scripts = data.get("scripts", {})
        if isinstance(scripts, dict):
            names.update(str(name) for name in scripts)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    setup_cfg = ConfigParser()
    try:
        setup_cfg.read_string(metadata_text(repo / "setup.cfg"))
        if setup_cfg.has_option("options.entry_points", "console_scripts"):
            names.update(
                line.split("=", 1)[0].strip()
                for line in setup_cfg.get("options.entry_points", "console_scripts").splitlines()
                if "=" in line
            )
    except (OSError, ValueError):
        pass
    return names


def _tokens(claim: Claim) -> list[str]:
    try:
        return shlex.split(claim.value, posix=True)
    except ValueError:
        return []


def _unknown_tokens(tokens: list[str], command_names: set[str], flag_names: set[str]) -> list[str]:
    subcommand = None
    unknown: list[str] = []
    previous_option = False
    for token in tokens:
        if token == "--":
            # Subsequent values are positional data even when they start with '-'.
            break
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in flag_names:
                unknown.append(flag)
            previous_option = "=" not in token
        elif token in command_names:
            subcommand = token
            previous_option = False
        elif subcommand is None and token not in {"--", "-"}:
            # Paths, numbers and option values are not evidence of an unknown command.
            # Without option arity metadata, abstain from interpreting an option's next value.
            if command_names and not previous_option and re.fullmatch(r"[A-Za-z][\w-]*", token):
                unknown.append(token)
                subcommand = token
            previous_option = False
    return unknown


def validate_command(claim: Claim, index: CodeIndex, repo: Path) -> Finding:  # noqa: PLR0911
    """Validate one command only when its root is independently tied to this project."""
    tokens = _tokens(claim)
    base_evidence = [Evidence(file=claim.doc_path, line=claim.line, text=claim.raw_text)]
    if not tokens:
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=base_evidence,
            confidence=0.2,
            reasons=["The documented command could not be tokenized safely."],
        )
    scripts = index.by_kind.get(SymbolKind.package_script, [])
    targets = index.by_kind.get(SymbolKind.make_target, [])
    wrapper = tokens[0].casefold()
    if wrapper in {"make", "just"} and len(tokens) > 1:
        target = next((symbol for symbol in targets if symbol.name == tokens[1]), None)
        if target:
            return Finding(
                claim=claim,
                matched_symbol=target,
                verdict=Verdict.ok,
                evidence=[*base_evidence, Evidence(file=target.file, line=target.line, text=target.name)],
                confidence=0.9,
                reasons=[f"Project target {target.name} exists."],
            )
        return Finding(
            claim=claim,
            verdict=Verdict.invalid_command,
            evidence=base_evidence,
            confidence=0.85,
            reasons=[f"Project target {tokens[1]} was not found."],
        )
    executable_index = 0
    if wrapper in {"npm", "pnpm", "yarn"} and len(tokens) >= MIN_SUBCOMMAND_TOKENS and tokens[1] == "run":
        script = next((symbol for symbol in scripts if symbol.name == tokens[2]), None)
        if script:
            return Finding(
                claim=claim,
                matched_symbol=script,
                verdict=Verdict.ok,
                evidence=[*base_evidence, Evidence(file=script.file, line=script.line, text=script.name)],
                confidence=0.9,
                reasons=[f"Package script {script.name} exists."],
            )
        return Finding(
            claim=claim,
            verdict=Verdict.invalid_command,
            evidence=base_evidence,
            confidence=0.85,
            reasons=[f"Package script {tokens[2]} was not found."],
        )
    if wrapper in {"npx", "uv", "poetry", "npm", "pnpm", "yarn"} and len(tokens) >= MIN_WRAPPER_TOKENS:
        executable_index = 2
        if wrapper == "npx" and len(tokens) > 1:
            executable_index = 1
    if wrapper in {"python", "python3"} and len(tokens) >= MIN_SUBCOMMAND_TOKENS and tokens[1] == "-m":
        executable_index = 2
    executable = tokens[executable_index]
    project_path = (
        (repo / executable.removeprefix("./")).is_file() if "/" in executable or "\\" in executable else False
    )
    cli_symbols = index.by_kind.get(SymbolKind.cli_command, [])
    cli_roots = [symbol for symbol in cli_symbols if symbol.parent_id is None]
    root_matches = [symbol for symbol in cli_roots if executable == symbol.name or executable in symbol.aliases]
    known_modules = {symbol.qualified_name.split(".", 1)[0] for symbol in cli_symbols}
    module_match = executable.replace("-", "_") in known_modules
    belongs = executable in _project_bins(repo) or bool(root_matches) or module_match or project_path
    if not belongs:
        return Finding(
            claim=claim,
            verdict=Verdict.unverifiable,
            evidence=base_evidence,
            confidence=0.15,
            reasons=[f"Command {executable} is not identified as a project command; third-party commands are skipped."],
        )
    command_names = {symbol.name for symbol in cli_symbols}
    flag_names = {
        name for symbol in index.by_kind.get(SymbolKind.cli_flag, []) for name in (symbol.name, *symbol.aliases)
    }
    unknown = _unknown_tokens(tokens[executable_index + 1 :], command_names, flag_names)
    if unknown:
        offset = claim.value.find(unknown[0])
        return Finding(
            claim=claim,
            verdict=Verdict.invalid_command,
            evidence=[
                *base_evidence,
                Evidence(
                    file=claim.doc_path,
                    line=claim.line,
                    text=f"{claim.value}\n{' ' * max(0, offset)}^",
                ),
            ],
            confidence=0.8,
            reasons=[f"Project command contains unknown command or flag: {', '.join(unknown)}."],
        )
    return Finding(
        claim=claim,
        matched_symbol=root_matches[0] if root_matches else None,
        verdict=Verdict.ok,
        evidence=[
            *base_evidence,
            *[Evidence(file=item.file, line=item.line, text=item.name) for item in root_matches[:1]],
        ],
        confidence=0.75,
        reasons=["The project command and documented CLI tokens are statically consistent."],
    )


def validate_commands(claims: list[Claim], index: CodeIndex, repo: Path) -> list[Finding]:
    return [validate_command(claim, index, repo) for claim in claims if claim.kind.value == "code_command"]
