"""Read-only documentation and repository context that matching uses to weigh evidence.

Claims carry only a value and a location. Several false positives come from facts that
sit next to the claim instead: a path-like heading the path is relative to, an include
directive naming the real file, or a third-party program that owns the flag. This module
reads the documentation text (bounded, never executed) and answers those questions.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections import defaultdict
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from docs_drift.index.base import CodeIndex
from docs_drift.limits import MAX_FILE_BYTES, MAX_LINE_CHARS
from docs_drift.models import Claim, SymbolKind
from docs_drift.safe_files import metadata_text

# Programs that are never this project's own CLI unless packaging metadata says so.
THIRD_PARTY_PROGRAMS = frozenset(
    {
        "apt",
        "apt-get",
        "aws",
        "az",
        "bash",
        "black",
        "brew",
        "bun",
        "bunx",
        "cargo",
        "cat",
        "choco",
        "clang",
        "conda",
        "coverage",
        "curl",
        "daphne",
        "deno",
        "dnf",
        "docker",
        "docker-compose",
        "echo",
        "env",
        "flake8",
        "fly",
        "gcc",
        "gcloud",
        "gh",
        "git",
        "go",
        "gradle",
        "grep",
        "gunicorn",
        "hatch",
        "helm",
        "heroku",
        "hypercorn",
        "isort",
        "java",
        "jq",
        "just",
        "kubectl",
        "ls",
        "make",
        "mamba",
        "mkdocs",
        "mvn",
        "mypy",
        "nginx",
        "node",
        "nox",
        "npm",
        "npx",
        "openssl",
        "pdm",
        "pip",
        "pip3",
        "pipx",
        "playwright",
        "pnpm",
        "podman",
        "poetry",
        "pre-commit",
        "psql",
        "py",
        "pylint",
        "pytest",
        "python",
        "python3",
        "rsync",
        "ruff",
        "rustc",
        "scp",
        "sed",
        "sh",
        "sphinx-build",
        "sqlite3",
        "ssh",
        "sudo",
        "tar",
        "terraform",
        "tox",
        "twine",
        "uv",
        "uvicorn",
        "uvx",
        "vercel",
        "wget",
        "winget",
        "xargs",
        "yarn",
        "zsh",
    }
)
# Names distinctive enough to identify a program in prose without code formatting.
# Ordinary English words such as "make", "black" or "just" only count inside code spans.
_PROSE_PROGRAMS = frozenset(
    {
        "conda",
        "curl",
        "daphne",
        "docker",
        "flake8",
        "git",
        "gunicorn",
        "heroku",
        "hypercorn",
        "isort",
        "kubectl",
        "mkdocs",
        "mypy",
        "nginx",
        "npm",
        "npx",
        "openssl",
        "pip",
        "pipx",
        "playwright",
        "pnpm",
        "poetry",
        "pylint",
        "pytest",
        "ruff",
        "tar",
        "tox",
        "uvicorn",
        "wget",
        "yarn",
    }
)
# A wrapper hands the command line to another program; that program owns later flags.
_NEXT_TOKEN_WRAPPERS = frozenset({"npx", "uvx", "bunx", "sudo", "env", "time", "nohup", "exec", "xargs"})
_RUN_WRAPPERS = {
    "uv": frozenset({"run"}),
    "poetry": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "hatch": frozenset({"run"}),
    "pipx": frozenset({"run"}),
    "npm": frozenset({"exec", "x"}),
    "pnpm": frozenset({"exec", "dlx"}),
    "yarn": frozenset({"exec", "dlx"}),
}
_INTERPRETERS = frozenset({"python", "python3", "py", "node", "deno", "bun"})
_PROMPT = re.compile(r"^\s*(?:\$|>|%|PS>)\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+:.])\1{2,}\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+")
_CODE_SPAN = re.compile(r"`+([^`]+?)`+")
_PATH_TOKEN = re.compile(r"[\w.@~+-]+(?:/[\w.@+-]+)+/?")
_CONTEXT_LINES_BEFORE = 4
_INCLUDE_WINDOW = 3
_MIN_DEPENDENCY_CHARS = 3


_URL = re.compile(r"\]\([^)]*\)|https?://\S+")
_SENTENCE_END = re.compile(r"[.!?:](?=\s)")


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def _current_sentences(text: str, offset: int) -> tuple[str, int]:
    """Trim context to the claim's sentence and the one before it, keeping the claim's offset valid."""
    ends = [match.end() for match in _SENTENCE_END.finditer(text)]
    before = [end for end in ends if end <= offset]
    start = before[-2] if len(before) > 1 else 0
    stop = next((end for end in ends if end > offset), len(text))
    return text[start:stop], offset - start


@dataclass(frozen=True)
class Owner:
    """The program a command-line token is passed to."""

    kind: str  # "project", "third-party", "script" or "unknown"
    name: str


def _read_text(repo: Path, relative: str) -> list[str] | None:
    try:
        target = repo / relative
        if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            return None
        if not target.resolve().is_relative_to(repo.resolve()):
            return None
        return [line[:MAX_LINE_CHARS] for line in target.read_text(encoding="utf-8", errors="replace").splitlines()]
    except (OSError, ValueError):
        return None


def _requirement_name(value: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", value)
    return match.group(1) if match else None


def _env_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _python_metadata(repo: Path, names: set[str], dependencies: set[str]) -> None:
    try:
        data = tomllib.loads(metadata_text(repo / "pyproject.toml"))
    except (OSError, tomllib.TOMLDecodeError):
        return
    project = data.get("project", {})
    if not isinstance(project, dict):
        return
    if isinstance(project.get("name"), str):
        names.add(project["name"])
    scripts = project.get("scripts", {})
    if isinstance(scripts, dict):
        names.update(str(name) for name in scripts)
    requirements: list[object] = list(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            requirements.extend(group if isinstance(group, list) else [])
    groups = data.get("dependency-groups", {})
    if isinstance(groups, dict):
        for group in groups.values():
            requirements.extend(group if isinstance(group, list) else [])
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
        requirements.extend(str(name) for name in poetry["dependencies"])
    for requirement in requirements:
        name = _requirement_name(requirement) if isinstance(requirement, str) else None
        if name:
            dependencies.add(name)


def _node_metadata(repo: Path, names: set[str], dependencies: set[str]) -> None:
    try:
        data = json.loads(metadata_text(repo / "package.json"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("name"), str):
        names.add(data["name"].rsplit("/", 1)[-1])
    bin_value = data.get("bin")
    if isinstance(bin_value, dict):
        names.update(str(name) for name in bin_value)
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            dependencies.update(str(name).rsplit("/", 1)[-1] for name in value)


def _setup_cfg_bins(repo: Path, names: set[str]) -> None:
    parser = ConfigParser()
    try:
        parser.read_string(metadata_text(repo / "setup.cfg"))
        if parser.has_option("options.entry_points", "console_scripts"):
            names.update(
                line.split("=", 1)[0].strip()
                for line in parser.get("options.entry_points", "console_scripts").splitlines()
                if "=" in line
            )
    except (OSError, ValueError):
        pass


@dataclass
class MatchContext:
    """Lazily computed, per-scan facts shared by every claim in one matching pass."""

    repo: Path
    index: CodeIndex
    _docs: dict[str, list[str] | None] = field(default_factory=dict)
    _headings: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    _commands: dict[str, list[tuple[int, list[str]]]] = field(default_factory=dict)
    _names: set[str] | None = None
    _dependencies: set[str] | None = None
    _basenames: dict[str, list[str]] | None = None

    # ----- repository facts -------------------------------------------------------------

    def _metadata(self) -> None:
        names: set[str] = set()
        dependencies: set[str] = set()
        _python_metadata(self.repo, names, dependencies)
        _node_metadata(self.repo, names, dependencies)
        _setup_cfg_bins(self.repo, names)
        names.update(
            symbol.name for symbol in self.index.by_kind.get(SymbolKind.cli_command, []) if symbol.parent_id is None
        )
        self._names = {name for name in names if name}
        own = {_env_name(name) for name in self._names}
        self._dependencies = {
            _env_name(name)
            for name in dependencies
            if len(_env_name(name)) >= _MIN_DEPENDENCY_CHARS and _env_name(name) not in own
        }

    @property
    def project_names(self) -> set[str]:
        """Distribution, package and executable names that identify this project."""
        if self._names is None:
            self._metadata()
        return self._names or set()

    @property
    def dependency_env_prefixes(self) -> set[str]:
        """Declared dependency names in environment-variable spelling (``python-dotenv`` -> ``PYTHON_DOTENV``)."""
        if self._dependencies is None:
            self._metadata()
        return self._dependencies or set()

    def paths_with_basename(self, name: str) -> list[str]:
        if self._basenames is None:
            grouped: dict[str, list[str]] = defaultdict(list)
            for path in sorted(self.index.path_lookup):
                grouped[PurePosixPath(path).name].append(path)
            self._basenames = dict(grouped)
        return self._basenames.get(name, [])

    # ----- documentation text -----------------------------------------------------------

    def lines(self, doc_path: str) -> list[str] | None:
        if doc_path not in self._docs:
            self._docs[doc_path] = _read_text(self.repo, doc_path)
        return self._docs[doc_path]

    def _heading_list(self, doc_path: str) -> list[tuple[int, str]]:
        if doc_path not in self._headings:
            lines = self.lines(doc_path) or []
            found: list[tuple[int, str]] = []
            for number, line in enumerate(lines, start=1):
                match = _HEADING.match(line)
                if match:
                    found.append((number, match.group(1)))
                elif (
                    number > 1
                    and _RST_UNDERLINE.match(line)
                    and lines[number - 2].strip()
                    and len(line.strip()) >= len(lines[number - 2].strip())
                ):
                    found.append((number - 1, lines[number - 2].strip()))
            self._headings[doc_path] = found
        return self._headings[doc_path]

    def heading_directories(self, claim: Claim) -> list[str]:
        """Directories named by the nearest preceding heading, innermost first (``source/utilities/``)."""
        headings = [text for number, text in self._heading_list(claim.doc_path) if number < claim.line]
        if not headings:
            return []
        text = headings[-1].strip()
        spans = _CODE_SPAN.findall(text)
        value = (spans[0] if len(spans) == 1 else text).strip().strip("`").removeprefix("./")
        if not value or " " in value:
            return []
        if not value.endswith("/") and value.rstrip("/") not in self.index.path_lookup:
            return []
        directory = PurePosixPath(value.rstrip("/"))
        return [parent.as_posix() for parent in (directory, *directory.parents) if parent.as_posix() != "."]

    def nearby_paths(self, claim: Claim) -> list[str]:
        """Path-like tokens written within a few lines of the claim, such as include directives."""
        lines = self.lines(claim.doc_path) or []
        start = max(0, claim.line - 1 - _INCLUDE_WINDOW)
        window = lines[start : claim.line + _INCLUDE_WINDOW]
        return [match.group(0).rstrip("/") for line in window for match in _PATH_TOKEN.finditer(line)]

    # ----- command ownership ------------------------------------------------------------

    def _command_lines(self, doc_path: str) -> list[tuple[int, list[str]]]:
        if doc_path not in self._commands:
            found: list[tuple[int, list[str]]] = []
            for number, line in enumerate(self.lines(doc_path) or [], start=1):
                # Inline commands such as ``np --test-script=x`` show ownership as well as code blocks do.
                found.extend(
                    (number, tokens)
                    for span in _CODE_SPAN.findall(line)
                    if len(span.split()) > 1 and (tokens := self._command_tokens(span))
                )
                text = _PROMPT.sub("", line).strip()
                if not text or text[0] in "`#*[|<":
                    continue
                tokens = self._command_tokens(text)
                if tokens:
                    found.append((number, tokens))
            self._commands[doc_path] = found
        return self._commands[doc_path]

    def _command_tokens(self, text: str) -> list[str]:
        text = _PROMPT.sub("", text).strip()
        first = text.split(maxsplit=1)[0] if text else ""
        if first not in THIRD_PARTY_PROGRAMS | self.project_names and not first.startswith(("./", "scripts/", "bin/")):
            return []
        try:
            return shlex.split(text, comments=True)
        except ValueError:
            return text.split()

    def _classify(self, program: str) -> Owner:
        if program in self.project_names:
            return Owner("project", program)
        if program in THIRD_PARTY_PROGRAMS:
            return Owner("third-party", program)
        if "/" in program and (self.repo / program.removeprefix("./")).is_file():
            return Owner("script", program.removeprefix("./"))
        return Owner("unknown", program)

    def _module_owner(self, module: str) -> Owner:
        top = module.split(".", 1)[0].replace("-", "_")
        modules = {name.replace("-", "_") for name in self.project_names} | {
            symbol.qualified_name.split(".", 1)[0] for symbol in self.index.by_kind.get(SymbolKind.cli_command, [])
        }
        if top in modules:
            return Owner("project", module)
        return Owner("third-party" if module in THIRD_PARTY_PROGRAMS else "unknown", module)

    def owners_in_command(self, tokens: list[str]) -> list[Owner]:
        """Return, for each token, the program that receives it (``uv run llm --help`` -> ``llm``)."""
        owners: list[Owner] = []
        current = Owner("unknown", "")
        expect_program = True
        module = False
        for token in tokens:
            if expect_program:
                wrapper_option = bool(owners) and token.startswith("-") and not module
                if wrapper_option or (current.name == "env" and "=" in token):
                    owners.append(current)
                    continue
                current = self._module_owner(token) if module else self._classify(token)
                owners.append(current)
                expect_program = token in _NEXT_TOKEN_WRAPPERS
                module = False
                continue
            owners.append(current)
            if token in _RUN_WRAPPERS.get(current.name, frozenset()):
                expect_program = True
            elif current.name in _INTERPRETERS and token == "-m":
                expect_program = module = True
            elif current.name in _INTERPRETERS and not token.startswith("-"):
                # ``python script.py --flag``: later flags belong to the script, not the interpreter.
                current = self._classify(token)
        return owners

    def token_owners(self, claim: Claim, token: str) -> tuple[list[Owner], bool]:
        """Owners of ``token`` wherever a command line in this document passes it.

        The boolean is true when the claim's own line is one of those command lines.
        """
        own: list[Owner] = []
        other: list[Owner] = []
        for number, tokens in self._command_lines(claim.doc_path):
            owners = self.owners_in_command(tokens)
            for value, owner in zip(tokens, owners, strict=True):
                if value.split("=", 1)[0] == token and value != tokens[0]:
                    (own if number == claim.line else other).append(owner)
        return (own, True) if own else (other, False)

    def _sentence(self, claim: Claim) -> tuple[str, int] | None:
        lines = self.lines(claim.doc_path)
        if not lines or claim.line > len(lines):
            return None
        index = claim.line - 1
        parts: list[str] = []
        if _LIST_ITEM.match(lines[index]):
            # A bullet's meaning comes from the item itself and the lead-in sentence of its list.
            cursor = index - 1
            while cursor >= 0 and (
                _LIST_ITEM.match(lines[cursor]) or lines[cursor].startswith((" ", "\t")) or not lines[cursor].strip()
            ):
                cursor -= 1
                if index - cursor > 3 * _CONTEXT_LINES_BEFORE:
                    break
            if cursor >= 0 and lines[cursor].rstrip().endswith(":"):
                parts.append(lines[cursor])
        else:
            cursor = index - 1
            while cursor >= 0 and lines[cursor].strip() and index - cursor <= _CONTEXT_LINES_BEFORE:
                parts.insert(0, lines[cursor])
                cursor -= 1
        offset = sum(len(part) + 1 for part in parts) + max(0, claim.column - 1)
        parts.append(lines[index])
        return "\n".join(parts), offset

    def prose_owner(self, claim: Claim) -> Owner | None:
        """The program named closest before the claim in its sentence (or, failing that, after it)."""
        sentence = self._sentence(claim)
        if sentence is None:
            return None
        text, offset = _current_sentences(*sentence)
        token = claim.value.split(maxsplit=1)[0]
        mentions: list[tuple[int, Owner]] = []
        project = {name.casefold() for name in self.project_names}
        for match in _CODE_SPAN.finditer(text):
            words = match.group(1).split()
            first = words[0].casefold() if words else ""
            # A complete example command (`npm test`) only speaks for the options it contains.
            if len(words) > 1 and token not in {word.split("=", 1)[0] for word in words}:
                continue
            if first in project:
                mentions.append((match.start(), Owner("project", first)))
            elif first in THIRD_PARTY_PROGRAMS:
                mentions.append((match.start(), Owner("third-party", first)))
        prose = _URL.sub(_blank, _CODE_SPAN.sub(_blank, text))
        for match in re.finditer(r"[A-Za-z][\w-]*", prose):
            word = match.group(0).casefold()
            if word in project:
                mentions.append((match.start(), Owner("project", word)))
            elif word in _PROSE_PROGRAMS:
                mentions.append((match.start(), Owner("third-party", word)))
        mentions = [item for item in mentions if not (offset <= item[0] < offset + len(claim.value) + 2)]
        before = [item for item in mentions if item[0] < offset]
        if before:
            return max(before, key=lambda item: item[0])[1]
        after = [item for item in mentions if item[0] > offset]
        return min(after, key=lambda item: item[0])[1] if after else None

    def third_party_owner(self, claim: Claim, token: str) -> str | None:
        """Name the non-project program that receives this flag or subcommand, when the document shows one.

        Command lines in the document decide first; a sentence naming another tool decides otherwise.
        A repository script without an indexed CLI declaration counts too: it forwards its arguments.
        """
        if self.lines(claim.doc_path) is None:
            return None
        owners, on_claim_line = self.token_owners(claim, token)
        if owners:
            foreign = [owner for owner in owners if self._foreign(owner)]
            if len(foreign) == len(owners):
                return foreign[0].name
            if on_claim_line or any(owner.kind == "project" for owner in owners):
                return None
        owner = self.prose_owner(claim)
        return owner.name if owner is not None and owner.kind == "third-party" else None

    def _foreign(self, owner: Owner) -> bool:
        return owner.kind == "third-party" or (owner.kind == "script" and not self.index.by_file.get(owner.name))

    def installed_plugin(self, claim: Claim, command: str) -> str | None:
        """A plugin package installed earlier in the document that provides ``command``."""
        lines = self.lines(claim.doc_path) or []
        pattern = re.compile(r"\binstall\s+(?:-\S+\s+)*([\w.-]*-" + re.escape(command) + r")\b")
        for line in lines[: max(0, claim.line - 1)]:
            match = pattern.search(line)
            if match:
                return match.group(1)
        return None
