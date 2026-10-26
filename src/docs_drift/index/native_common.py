"""Shared, read-only helpers for the Go and Rust CLI indexers.

Both indexers parse source with tree-sitter grammars that ship as wheels, never
compile or execute repository code, and read only bounded manifest text
(``go.mod`` and ``Cargo.toml``). Everything here is deliberately conservative:
when a name, default or executable cannot be read as a literal, callers abstain
instead of guessing.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import tree_sitter_go
import tree_sitter_rust
from tree_sitter import Language, Node, Parser

from docs_drift.ids import make_symbol_id
from docs_drift.limits import MAX_FILE_BYTES
from docs_drift.models import Symbol, SymbolKind

_PARSERS: dict[str, Parser] = {}
# Directories whose programs are fixtures, samples or generated inputs rather
# than the shipped command line. Their flags must not vouch for documentation.
NON_PRODUCT_DIRS = frozenset(
    {
        ".git",
        "vendor",
        "node_modules",
        "testdata",
        "tests",
        "test",
        "examples",
        "example",
        "_examples",
        "benches",
        "fuzz",
        "target",
        "third_party",
        "xtask",
    }
)
MAX_MANIFESTS = 2000
MAX_WALK_FILES = 50_000


_LANGUAGES: dict[str, Language] = {}


def language_object(language: str) -> Language:
    """The installed Go or Rust grammar; nothing is downloaded or compiled at runtime."""
    if language not in _LANGUAGES:
        capsule = tree_sitter_go.language() if language == "go" else tree_sitter_rust.language()
        _LANGUAGES[language] = Language(capsule)
    return _LANGUAGES[language]


def parser_for(language: str) -> Parser:
    """Reuse native Go/Rust parsers whose grammars ship with installed wheels."""
    if language not in _PARSERS:
        _PARSERS[language] = Parser(language_object(language))
    return _PARSERS[language]


def in_error(node: Node) -> bool:
    """True inside tree-sitter error recovery, where shapes are guesses rather than source facts."""
    current: Node | None = node
    while current is not None:
        if current.type == "ERROR" or current.is_missing:
            return True
        current = current.parent
    return False


def is_product_path(relative: str) -> bool:
    """Return False for tests, fixtures, sample programs and repository tooling (dot directories, xtask)."""
    parts = PurePosixPath(relative).parts[:-1]
    return not any(part in NON_PRODUCT_DIRS or part.startswith(".") for part in parts) and not relative.endswith(
        "_test.go"
    )


def node_text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def line_of(node: Node) -> int:
    return node.start_point[0] + 1


def walk(node: Node) -> Iterator[Node]:
    """Pre-order traversal that never descends into tree-sitter error recovery regions."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR" or current.is_missing:
            continue
        yield current
        stack.extend(reversed(current.children))


_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}


def _unescape(body: str) -> str | None:
    if "\\" not in body:
        return body
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1 : index + 2]
        if nxt not in _SIMPLE_ESCAPES:
            # Unicode and hex escapes are legal but never needed for flag names.
            return None
        out.append(_SIMPLE_ESCAPES[nxt])
        index += 2
    return "".join(out)


def string_literal(node: Node | None) -> str | None:
    """Decode one Go or Rust string literal; anything else (or interpolation) is not a literal."""
    if node is None:
        return None
    if node.type in {"literal_element", "parenthesized_expression"} and node.named_child_count == 1:
        return string_literal(node.named_children[0])
    text = node_text(node)
    if node.type == "raw_string_literal":
        if text.startswith("`") and text.endswith("`") and len(text) >= 2:  # noqa: PLR2004 - Go raw string.
            return text[1:-1]
        match = re.fullmatch(r'r(#*)"(.*)"\1', text, re.S)
        return match.group(2) if match else None
    if node.type in {"interpreted_string_literal", "string_literal"} and len(text) >= 2 and text[0] == text[-1] == '"':  # noqa: PLR2004
        return _unescape(text[1:-1])
    return None


def char_literal(node: Node | None) -> str | None:
    if node is None or node.type not in {"char_literal", "rune_literal"}:
        return None
    text = node_text(node)
    if len(text) == 3 and text[0] == text[-1] == "'":  # noqa: PLR2004 - quote, char, quote.
        return text[1]
    return None


def kebab(name: str) -> str:
    """Convert snake_case or CamelCase identifiers to kebab-case like clap's default."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name))
    return "-".join(part.casefold() for part in words.strip("_").split("_") if part)


def convert_case(name: str, style: str | None) -> str | None:
    """Apply a clap/structopt ``rename_all`` style; unknown styles return None (abstain)."""
    words = [
        part
        for part in re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)).split("_")
        if part
    ]
    lower = [word.casefold() for word in words]
    styles = {
        None: "-".join(lower),
        "kebab-case": "-".join(lower),
        "kebab": "-".join(lower),
        "snake_case": "_".join(lower),
        "snake": "_".join(lower),
        "SCREAMING_SNAKE_CASE": "_".join(word.upper() for word in words),
        "screaming_snake": "_".join(word.upper() for word in words),
        "UPPER": "".join(word.upper() for word in words),
        "upper": "".join(word.upper() for word in words),
        "lower": "".join(lower),
        "lowercase": "".join(lower),
        "camelCase": lower[0] + "".join(word.capitalize() for word in lower[1:]) if lower else "",
        "PascalCase": "".join(word.capitalize() for word in lower),
        "verbatim": name,
    }
    return styles.get(style) or None


def make_symbol(  # noqa: PLR0913 - static declaration context.
    repo: Path,
    path: Path,
    kind: SymbolKind,
    qualified: str,
    line: int,
    *,
    name: str,
    language: str,
    framework: str | None,
    aliases: list[str] | None = None,
    default: str | None = None,
    derived: bool = False,
    authoritative: bool = True,
    partial_prefix: bool = False,
) -> Symbol:
    relative = path.relative_to(repo).as_posix()
    unique_aliases = [alias for alias in dict.fromkeys(aliases or []) if alias and alias != name]
    return Symbol(
        symbol_id=make_symbol_id(kind, qualified, relative),
        kind=kind,
        name=name,
        qualified_name=qualified,
        aliases=unique_aliases,
        default=default,
        file=relative,
        line=max(1, line),
        language=language,
        framework=framework,
        derived=derived,
        authoritative=authoritative,
        partial_prefix=partial_prefix,
    )


def unresolved_parent(symbol: Symbol) -> str:
    """A stable placeholder parent for a subcommand whose parent is declared elsewhere.

    Command validation treats a ``cli_command`` without a parent as an executable root; a
    subcommand registered from another file must not be mistaken for one.
    """
    return make_symbol_id(SymbolKind.cli_command, f"{symbol.qualified_name}#unresolved-parent", symbol.file)


def scope_prefix(repo: Path, path: Path) -> str:
    """A dot-free per-file prefix so command scopes stay distinct qualified-name segments."""
    relative = path.relative_to(repo).as_posix()
    return relative.rsplit(".", 1)[0].replace(".", "_")


def dedupe(symbols: list[Symbol]) -> list[Symbol]:
    seen: set[str] = set()
    result = []
    for symbol in symbols:
        if symbol.symbol_id in seen:
            continue
        seen.add(symbol.symbol_id)
        result.append(symbol)
    return result


def read_source(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def repo_files(repo: Path, suffixes: tuple[str, ...], names: tuple[str, ...] = ()) -> list[Path]:
    """Bounded walk for repository context files (manifests, sibling packages)."""
    result: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in NON_PRODUCT_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(suffixes) or name in names:
                result.append(Path(root) / name)
                if len(result) >= MAX_WALK_FILES:
                    return result
    return result


def env_prefix_symbol(  # noqa: PLR0913 - static declaration context.
    repo: Path, path: Path, prefix: str, line: int, *, language: str, framework: str
) -> Symbol:
    """Record a dynamic environment prefix: names under it are unverifiable, never missing."""
    return make_symbol(
        repo,
        path,
        SymbolKind.env_var,
        f"{scope_prefix(repo, path)}.env-prefix:{prefix}*",
        line,
        name=f"{prefix}*",
        language=language,
        framework=framework,
        derived=True,
        authoritative=False,
        partial_prefix=True,
    )


@dataclass
class Crate:
    """One Cargo package and the executables it builds."""

    root: str
    package: str | None
    bins: dict[str, str] = field(default_factory=dict)  # bin name -> source path relative to the crate root


def cargo_crates(repo: Path) -> list[Crate]:
    """Read Cargo package and ``[[bin]]`` names; implicit ``src/main.rs`` and ``src/bin`` bins included."""
    crates: list[Crate] = []
    for manifest in repo_files(repo, (), ("Cargo.toml",))[:MAX_MANIFESTS]:
        text = read_source(manifest)
        if text is None:
            continue
        try:
            data = tomllib.loads(text.decode("utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            continue
        package = data.get("package")
        if not isinstance(package, dict):
            continue
        name = package.get("name") if isinstance(package.get("name"), str) else None
        crate_dir = manifest.parent
        crate = Crate(root=crate_dir.relative_to(repo).as_posix(), package=name)
        explicit = data.get("bin")
        if isinstance(explicit, list):
            for item in explicit:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    default_path = "src/main.rs" if item["name"] == name else f"src/bin/{item['name']}.rs"
                    declared = item.get("path")
                    source = declared if isinstance(declared, str) else default_path
                    crate.bins[item["name"]] = PurePosixPath(source).as_posix().removeprefix("./")
        autobins = package.get("autobins", True) is not False
        if (
            name
            and (crate_dir / "src" / "main.rs").is_file()
            and not any(path == "src/main.rs" for path in crate.bins.values())
        ):
            crate.bins.setdefault(name, "src/main.rs")
        bin_dir = crate_dir / "src" / "bin"
        if autobins and bin_dir.is_dir():
            for child in sorted(bin_dir.iterdir()):
                if child.suffix == ".rs" and child.is_file():
                    crate.bins.setdefault(child.stem, f"src/bin/{child.name}")
                elif child.is_dir() and (child / "main.rs").is_file():
                    crate.bins.setdefault(child.name, f"src/bin/{child.name}/main.rs")
        crates.append(crate)
    return crates


def crate_for(relative: str, crates: list[Crate]) -> Crate | None:
    owners = [crate for crate in crates if crate.root == "." or relative.startswith(crate.root + "/")]
    return max(owners, key=lambda crate: 0 if crate.root == "." else len(crate.root), default=None)


def rust_executable(relative: str, crates: list[Crate]) -> tuple[str | None, list[str]]:
    """Return the executable a Rust source file builds into (if unambiguous) and the crate's other names."""
    crate = crate_for(relative, crates)
    if crate is None:
        return None, []
    inside = relative if crate.root == "." else relative.removeprefix(crate.root + "/")
    for bin_name, source in crate.bins.items():
        if source == inside or (source.endswith("/main.rs") and inside.startswith(source.rsplit("/", 1)[0] + "/")):
            return bin_name, [crate.package] if crate.package and crate.package != bin_name else []
    if inside.startswith("src/bin/"):
        return None, []
    if len(crate.bins) == 1:
        only = next(iter(crate.bins))
        return only, [crate.package] if crate.package and crate.package != only else []
    return None, []


def go_module_name(repo: Path, directory: Path) -> str | None:
    """Last import-path element of the nearest ``go.mod`` module (major version suffix removed)."""
    current = directory
    while True:
        manifest = current / "go.mod"
        if manifest.is_file():
            text = read_source(manifest)
            if text is None:
                return None
            match = re.search(r"(?m)^\s*module\s+\"?([^\s\"]+)", text.decode("utf-8", errors="replace"))
            if not match:
                return None
            parts = [part for part in match.group(1).split("/") if part]
            while len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]):
                parts.pop()
            return parts[-1] if parts else None
        if current == repo or repo not in current.parents:
            return None
        current = current.parent


def go_executables(repo: Path, main_dirs: set[Path]) -> set[str]:
    """``go build`` names a program after its directory; the module root uses the module path."""
    names: set[str] = set()
    for directory in main_dirs:
        if (directory / "go.mod").is_file():
            module = go_module_name(repo, directory)
            if module:
                names.add(module)
        else:
            names.add(directory.name)
    return names
