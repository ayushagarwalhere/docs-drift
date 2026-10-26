"""Compare imports in source code with declared dependencies; source is parsed, never imported or run."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from tree_sitter import Node

from docs_drift.config import DepsConfig
from docs_drift.deps.manifests import Declared, Ecosystem, Manifest, ManifestSet, node_key, python_key
from docs_drift.deps.names import (
    COMPANIONS,
    IMPORT_TO_DISTRIBUTIONS,
    NON_IMPORTED_NODE,
    STDLIB,
    node_package,
    python_candidates,
    python_non_imported,
)
from docs_drift.limits import MAX_FILE_BYTES
from docs_drift.models import DependencyIssue, DependencyIssueKind, Evidence

Role = Literal["runtime", "dev"]
MAX_EVIDENCE = 3
MAX_CORPUS_BYTES = 8_000_000
_PY_DEV_DIRS = {
    "tests",
    "test",
    "testing",
    "docs",
    "doc",
    "scripts",
    "benchmarks",
    "benchmark",
    "examples",
    "example",
    "tools",
    "fixtures",
    "docs_src",
    "samples",
    "sample",
    "demo",
    "demos",
    "tutorial",
    "tutorials",
}
_PY_DEV_FILES = {"conftest.py", "noxfile.py", "setup.py", "tasks.py", "fabfile.py", "conf.py"}
_JS_DEV_DIRS = {
    "test",
    "tests",
    "__tests__",
    "__mocks__",
    "e2e",
    "cypress",
    "playwright",
    "scripts",
    "stories",
    ".storybook",
    "examples",
    "example",
    "docs",
    "benchmark",
    "benchmarks",
    "fixtures",
    "playground",
}
_JS_DEV_NAME = re.compile(
    r"\.(?:test|spec|stories|story|config|conf|setup)\.[cm]?[jt]sx?$|^\.|^(?:gulpfile|gruntfile|jakefile)\.", re.I
)
_JS_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"}
_LOCKFILES = re.compile(r"(?:\.lock|-lock\.json|-lock\.ya?ml|shrinkwrap\.json)$", re.I)
_CORPUS_NAMES = re.compile(
    r"^(?:Makefile|GNUmakefile|justfile|Procfile|Dockerfile.*|.*\.dockerfile|.*\.sh|.*\.ps1|.*\.ya?ml|"
    r"tox\.ini|\.babelrc.*|\.eslintrc.*|\.prettierrc.*|.*\.json|.*\.ini|.*\.cfg|.*\.(?:vue|svelte|astro|html|mdx))$",
    re.I,
)
_MANIFEST_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "Pipfile", "package.json"}
_DRIVER = re.compile(r"\+([A-Za-z_]\w*)://")
_PATH_STRING = re.compile(r"""["'`]((?:@[\w.-]+/)?[\w.-]+/[^"'`\s]*)["'`]""")
_BUNDLERS = re.compile(
    r"(?<![\w-])(?:rollup|rolldown|tsup|tsdown|esbuild|webpack|unbuild|parcel|@vercel/ncc|ncc|vite build|bun build)"
    r"(?![\w-])"
)
_FIXTURE_DIRS = {"fixtures", "__fixtures__", "__tests__", "test", "tests", "testdata", "playground"}


@dataclass(frozen=True)
class Usage:
    file: str
    line: int
    role: Role
    text: str
    guarded: bool = False
    typing_only: bool = False
    lazy: bool = False


@dataclass
class UsageResult:
    issues: list[DependencyIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_parsed: int = 0


def issue_id(ecosystem: str, kind: str, key: str, manifest: str | None) -> str:
    payload = json.dumps(["dep", 1, ecosystem, kind, key, manifest or ""], separators=(",", ":")).encode()
    return "dep_" + hashlib.sha256(payload).hexdigest()


def make_issue(  # noqa: PLR0913, PLR0917 - issue facts.
    kind: DependencyIssueKind,
    ecosystem: Ecosystem,
    name: str,
    key: str,
    manifest: str | None,
    message: str,
    evidence: list[Evidence],
    confidence: float,
    import_name: str | None = None,
) -> DependencyIssue:
    return DependencyIssue(
        issue_id=issue_id(ecosystem, kind.value, key, manifest),
        kind=kind,
        ecosystem=ecosystem,
        name=name,
        import_name=import_name,
        manifest=manifest,
        message=message,
        evidence=evidence,
        confidence=confidence,
    )


def _python_role(path: str) -> Role:
    pure = PurePosixPath(path)
    if (
        set(pure.parts[:-1]) & _PY_DEV_DIRS
        or pure.name in _PY_DEV_FILES
        or pure.name.startswith("test_")
        or pure.name.endswith("_test.py")
    ):
        return "dev"
    return "runtime"


def _js_role(path: str) -> Role:
    pure = PurePosixPath(path)
    if set(pure.parts[:-1]) & _JS_DEV_DIRS or _JS_DEV_NAME.search(pure.name):
        return "dev"
    return "runtime"


def _read(root: Path, relative: str) -> str | None:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _catches_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    for handler in handlers:
        if handler.type is None:
            return True
        names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for name in names:
            label = name.id if isinstance(name, ast.Name) else name.attr if isinstance(name, ast.Attribute) else ""
            if label in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}:
                return True
    return False


_CONDITIONAL_IMPORT = re.compile(
    r"\b(?:has|is)_|_available\b|\bavailable_|find_spec|installed|version_info|platform|os\.name|\bPY[23]\b|PYPY|WINDOWS",
    re.I,
)
_STRING = re.compile(r"""["']([A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,6})["']""")
_STATEMENT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def python_imports(tree: ast.Module) -> Iterator[tuple[str, int, bool, bool, bool]]:
    """Yield (module, line, guarded, typing_only, lazy) for absolute imports, visiting statements only.

    Imports inside an ImportError handler block (body, fallbacks and else branch) or under a feature,
    version or platform test are optional; `if TYPE_CHECKING:` bodies are type-only.
    """
    stack: list[tuple[ast.AST, bool, bool, bool]] = [(tree, False, False, False)]
    while stack:
        node, guarded, typing_only, lazy = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno, guarded, typing_only, lazy
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module, node.lineno, guarded, typing_only, lazy
            continue
        if isinstance(node, ast.If) and _type_checking(node.test):
            stack.extend((child, guarded, True, lazy) for child in node.body)
            stack.extend((child, guarded, typing_only, lazy) for child in node.orelse)
            continue
        optional_try = isinstance(node, ast.Try) and _catches_import_error(node.handlers)
        feature_test = isinstance(node, ast.If) and bool(_CONDITIONAL_IMPORT.search(ast.unparse(node.test)))
        child_guarded = guarded or optional_try or feature_test
        nested = lazy or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for name in _STATEMENT_FIELDS:
            children = getattr(node, name, None)
            if isinstance(children, list):
                stack.extend((child, child_guarded, typing_only, nested) for child in children)


def _string_references(text: str) -> set[str]:
    """Module-like string literals (plugin lists, INSTALLED_APPS, DB driver URLs) count as weak usage."""
    return set(_STRING.findall(text)) | set(_DRIVER.findall(text))


def _local_python_names(paths: Sequence[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        pure = PurePosixPath(path)
        names.add(pure.stem)
        names.update(pure.parts[:-1])
    return names


def _declared_keys(declarations: list[Declared], groups: set[str]) -> set[str]:
    return {d.key for d in declarations if d.group in groups}


def _ignored(name: str, config: DepsConfig) -> bool:
    return any(fnmatch.fnmatchcase(name.casefold(), pattern.casefold()) for pattern in config.ignore)


def _evidence(usages: list[Usage]) -> list[Evidence]:
    ordered = sorted(usages, key=lambda u: (u.role != "runtime", u.file, u.line))
    return [Evidence(file=u.file, line=u.line, text=u.text) for u in ordered[:MAX_EVIDENCE]]


def _corpus(root: Path, paths: Sequence[str]) -> str:
    """Non-manifest configuration and script text where tools and plugins are often referenced by name."""
    chunks: list[str] = []
    total = 0
    for path in paths:
        pure = PurePosixPath(path)
        # Markdown counts only when it embeds a component script (VitePress, Docusaurus, MDX-style pages).
        markdown = pure.suffix.casefold() == ".md"
        if pure.name in _MANIFEST_FILES or _LOCKFILES.search(pure.name):
            continue
        if not (markdown or _CORPUS_NAMES.match(pure.name)) or pure.name.startswith(("requirements", "constraints")):
            continue
        text = _read(root, path)
        if text is None or (markdown and "<script" not in text):
            continue
        total += len(text)
        if total > MAX_CORPUS_BYTES:
            break
        chunks.append(text)
    return "\n".join(chunks)


def _mentioned(corpus: str, names: set[str]) -> bool:
    return any(re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", corpus, re.I) for name in names if name)


def _analyse_python(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917 - one pass per decision type.
    root: Path, files: list[str], manifests: ManifestSet, config: DepsConfig, corpus: str, result: UsageResult
) -> None:
    python_manifests = [m for m in manifests.manifests if m.ecosystem == "python" and m.dependencies]
    if not python_manifests:
        return
    declarations = [d for m in python_manifests for d in m.dependencies]
    runtime_keys = _declared_keys(declarations, {"runtime", "optional", "peer"})
    any_keys = _declared_keys(declarations, {"runtime", "optional", "peer", "dev", "build"})
    projects = manifests.project_names("python")
    local = _local_python_names(files)
    mapped = {name: python_key(dist) for name, dist in config.import_map.items()}
    usages: dict[str, list[Usage]] = {}
    imports_by_key: dict[str, str] = {}
    used_keys: set[str] = set()
    for relative in files:
        if not manifests.for_path(relative, "python"):
            continue
        text = _read(root, relative)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            result.warnings.append(f"Dependency usage skipped unparsable Python file: {relative}")
            continue
        result.files_parsed += 1
        lines = text.splitlines()
        role = _python_role(relative)
        for reference in _string_references(text):
            used_keys.update(python_candidates(reference) & any_keys)
        for module, line, guarded, typing_only, lazy in python_imports(tree):
            top = module.split(".")[0]
            if top in local:
                # A local module may share a dependency's name (proj/celery.py importing celery): the import
                # still proves a declared package is used, but it is never reported as undeclared.
                used_keys.update(python_candidates(module) & any_keys)
            if top in STDLIB or top in local or top == "__main__" or python_key(top) in projects:
                continue
            candidates = python_candidates(module)
            if top in mapped:
                candidates.add(mapped[top])
            matched = candidates & any_keys
            used_keys.update(matched)
            expected = mapped.get(top) or next(iter(IMPORT_TO_DISTRIBUTIONS.get(top, ())), python_key(top))
            key = sorted(matched)[0] if matched else expected
            imports_by_key.setdefault(key, top)
            source = lines[line - 1].strip() if 0 < line <= len(lines) else module
            usages.setdefault(key, []).append(Usage(relative, line, role, source, guarded, typing_only, lazy))
    for key, items in sorted(usages.items()):
        name = imports_by_key[key]
        if _ignored(key, config) or _ignored(name, config):
            continue
        active = [u for u in items if not u.guarded]
        if not active:
            continue
        runtime = [u for u in active if u.role == "runtime" and not u.typing_only]
        if key not in any_keys:
            if runtime:
                weak = all(u.lazy for u in runtime) or bool(set(COMPANIONS.get(key, ())) & any_keys)
                result.issues.append(
                    make_issue(
                        DependencyIssueKind.undeclared,
                        "python",
                        key,
                        key,
                        python_manifests[0].path,
                        f"`import {name}` is used by runtime code but no manifest declares a distribution for it "
                        f"(expected {key!r} or a mapped name).",
                        _evidence(runtime),
                        0.55 if weak else 0.85,
                        name,
                    )
                )
            else:
                result.issues.append(
                    make_issue(
                        DependencyIssueKind.undeclared_dev,
                        "python",
                        key,
                        key,
                        python_manifests[0].path,
                        f"`import {name}` appears only in tests, tooling or type-checking code and is not declared "
                        "in any dependency group.",
                        _evidence(active),
                        0.55,
                        name,
                    )
                )
        elif runtime and key not in runtime_keys:
            declaration = next(d for d in declarations if d.key == key)
            result.issues.append(
                make_issue(
                    DependencyIssueKind.dev_only,
                    "python",
                    declaration.name,
                    key,
                    declaration.manifest,
                    f"`import {name}` is used by runtime code, but {declaration.name} is declared only as a "
                    f"{declaration.group} dependency in {declaration.manifest}.",
                    _evidence(runtime),
                    0.65,
                    name,
                )
            )
    for manifest in python_manifests:
        if _fixture_manifest(manifest.path):
            continue
        for declaration in manifest.dependencies:
            key = declaration.key
            if declaration.group != "runtime" or key in used_keys or python_non_imported(key):
                continue
            if _ignored(declaration.name, config) or key in projects:
                continue
            aliases = {declaration.name, key, *(n for n, k in mapped.items() if k == key)}
            if _mentioned(corpus, aliases):
                continue
            result.issues.append(
                make_issue(
                    DependencyIssueKind.unused,
                    "python",
                    declaration.name,
                    key,
                    declaration.manifest,
                    f"{declaration.name} is a declared runtime dependency, but no source file imports it and no "
                    "script or configuration names it.",
                    [Evidence(file=declaration.manifest, line=declaration.line, text=declaration.name)],
                    0.7,
                )
            )


def _string_value(node: Node) -> str | None:
    if node.type not in {"string", "template_string"}:
        return None
    text = (node.text or b"").decode("utf-8", errors="replace")
    if node.type == "template_string" and "${" in text:
        return None
    return text[1:-1] if len(text) >= 2 else None  # noqa: PLR2004 - quoted literal.


def _in_type_position(node: Node) -> bool:
    """`typeof import("x")` in a TypeScript type is erased at compile time."""
    parent = node.parent
    for _ in range(3):
        if parent is None:
            return False
        if "type" in parent.type and parent.type not in {"type_assertion", "as_expression"}:
            return True
        parent = parent.parent
    return False


def js_imports(root_node: Node) -> Iterator[tuple[str, int, bool]]:
    """Yield (specifier, line, type_only) for static imports, re-exports, require() and import() calls."""
    stack = [root_node]
    while stack:
        node = stack.pop()
        kind = node.type
        if kind in {"import_statement", "export_statement"}:
            text = (node.text or b"")[:20].decode("utf-8", errors="replace")
            type_only = bool(re.match(r"(?:import|export)\s+type\b", text))
            # Only the `from "..."` source names a module; `export default "ok"` is a value.
            source = node.child_by_field_name("source")
            value = _string_value(source) if source is not None else None
            if value is not None:
                yield value, node.start_point[0] + 1, type_only
            for child in node.children:
                if child.type == "import_require_clause":
                    for grandchild in child.children:
                        value = _string_value(grandchild)
                        if value is not None:
                            yield value, node.start_point[0] + 1, type_only
        elif kind == "call_expression" and not _in_type_position(node):
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if (
                function is not None
                and arguments is not None
                and (function.type == "import" or (function.type == "identifier" and function.text == b"require"))
            ):
                first = next((c for c in arguments.named_children), None)
                value = _string_value(first) if first is not None else None
                if value is not None:
                    yield value, node.start_point[0] + 1, False
        stack.extend(reversed(node.children))


def _ts_aliases(root: Path, manifest_root: str) -> list[str]:
    aliases: list[str] = []
    for name in ("tsconfig.json", "jsconfig.json"):
        text = _read(root, f"{manifest_root}/{name}" if manifest_root != "." else name)
        if text is None:
            continue
        stripped = re.sub(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", "", text, flags=re.S)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        options = data.get("compilerOptions") if isinstance(data, dict) else None
        paths = options.get("paths") if isinstance(options, dict) else None
        if isinstance(paths, dict):
            aliases.extend(str(key).rstrip("*").rstrip("/") for key in paths if str(key).rstrip("*"))
    return aliases


def _js_local(root: Path, manifest_root: str, package: str) -> bool:
    base = root if manifest_root == "." else root / manifest_root
    first = package.split("/", maxsplit=1)[0]
    for parent in (base, base / "src"):
        if (parent / first).is_dir() or any((parent / (first + suffix)).is_file() for suffix in _JS_SUFFIXES):
            return True
    return False


def _types_package(name: str) -> str:
    return "@types/" + (name[1:].replace("/", "__") if name.startswith("@") else name)


def _node_declarations(manifests: list[Manifest]) -> dict[str, list[Declared]]:
    declared: dict[str, list[Declared]] = {}
    for manifest in manifests:
        for item in manifest.dependencies:
            declared.setdefault(item.key, []).append(item)
    return declared


def _bundled(manifest: Manifest) -> bool:
    """Packages built with a bundler legitimately inline devDependencies into their published output."""
    tools = " ".join([manifest.scripts_text, *(d.key for d in manifest.dependencies if d.group == "dev")])
    return bool(_BUNDLERS.search(tools))


def _fixture_manifest(path: str) -> bool:
    """Manifests inside test fixtures describe sample projects, not this package."""
    return bool(set(PurePosixPath(path).parts[:-1]) & _FIXTURE_DIRS)


def _analyse_node(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917 - one pass per decision type.
    root: Path, files: list[str], manifests: ManifestSet, config: DepsConfig, corpus: str, result: UsageResult
) -> None:
    node_manifests = [m for m in manifests.manifests if m.ecosystem == "node" and m.path.endswith("package.json")]
    if not node_manifests:
        return
    from docs_drift.index.ts_parser import parser_for  # noqa: PLC0415 - native grammar loaded only when needed.

    workspace_names = manifests.project_names("node")
    aliases = {m.root: _ts_aliases(root, m.root) for m in node_manifests}
    used: dict[str, set[str]] = {m.path: set() for m in node_manifests}
    has_jsx: dict[str, bool] = {m.path: False for m in node_manifests}
    findings: dict[tuple[str, str], list[Usage]] = {}
    for relative in files:
        chain = [m for m in manifests.for_path(relative, "node") if m.path.endswith("package.json")]
        if not chain or relative.endswith(".d.ts") or ".min." in relative:
            continue
        nearest = chain[0]
        text = _read(root, relative)
        if text is None:
            continue
        suffix = PurePosixPath(relative).suffix.casefold()
        has_jsx[nearest.path] = has_jsx[nearest.path] or suffix in {".jsx", ".tsx"}
        try:
            tree = parser_for("typescript" if "ts" in suffix else "javascript").parse(text.encode("utf-8"))
        except (ValueError, RecursionError, MemoryError):
            result.warnings.append(f"Dependency usage skipped unparsable source file: {relative}")
            continue
        result.files_parsed += 1
        lines = text.splitlines()
        role = _js_role(relative)
        # Package paths in strings (require.resolve, plugin names, asset copies) count as weak usage.
        used[nearest.path].update(
            p for s in _STRING.findall(text) + _PATH_STRING.findall(text) if (p := node_package(s))
        )
        for specifier, line, type_only in js_imports(tree.root_node):
            package = node_package(specifier)
            if package is None or package in workspace_names:
                continue
            if any(specifier == alias or specifier.startswith(alias + "/") for alias in aliases[nearest.root] if alias):
                continue
            used[nearest.path].add(package)
            source = lines[line - 1].strip() if 0 < line <= len(lines) else specifier
            findings.setdefault((nearest.path, package), []).append(
                Usage(relative, line, role, source, typing_only=type_only)
            )
    for (manifest_path, package), items in sorted(findings.items()):
        manifest = next(m for m in node_manifests if m.path == manifest_path)
        chain = [m for m in manifests.for_path(manifest.path, "node") if m.path.endswith("package.json")]
        declared = _node_declarations(chain)
        if _ignored(package, config):
            continue
        groups = {d.group for d in declared.get(node_key(package), [])}
        type_groups = {d.group for d in declared.get(_types_package(package), [])}
        runtime = [u for u in items if u.role == "runtime" and not u.typing_only]
        if not groups:
            if not runtime and type_groups:
                continue
            if _js_local(root, manifest.root, package):
                continue
            kind = DependencyIssueKind.undeclared if runtime else DependencyIssueKind.undeclared_dev
            message = (
                f"`{package}` is imported by runtime code but {manifest.path} does not declare it."
                if runtime
                else f"`{package}` is imported only by tests, tooling or type-only code and is not declared."
            )
            result.issues.append(
                make_issue(
                    kind,
                    "node",
                    package,
                    node_key(package),
                    manifest.path,
                    message,
                    _evidence(runtime or items),
                    0.8 if runtime else 0.55,
                    package,
                )
            )
        elif runtime and groups <= {"dev"} and not manifest.private and not _bundled(manifest):
            result.issues.append(
                make_issue(
                    DependencyIssueKind.dev_only,
                    "node",
                    package,
                    node_key(package),
                    manifest.path,
                    f"`{package}` is imported by runtime code of a published package but is declared only in "
                    "devDependencies.",
                    _evidence(runtime),
                    0.65,
                    package,
                )
            )
    for manifest in node_manifests:
        if _fixture_manifest(manifest.path):
            continue
        for declaration in manifest.dependencies:
            name = declaration.key
            if declaration.group != "runtime" or name in used[manifest.path] or name in NON_IMPORTED_NODE:
                continue
            if name.startswith("@types/") or _ignored(declaration.name, config):
                continue
            if name == "react" and has_jsx[manifest.path]:
                continue
            if _mentioned(manifest.scripts_text, {declaration.name}) or _mentioned(corpus, {declaration.name}):
                continue
            result.issues.append(
                make_issue(
                    DependencyIssueKind.unused,
                    "node",
                    declaration.name,
                    name,
                    manifest.path,
                    f"{declaration.name} is listed in dependencies, but no source file under {manifest.root} imports "
                    "it and no script or configuration names it.",
                    [Evidence(file=manifest.path, line=declaration.line, text=declaration.name)],
                    0.65,
                )
            )


def analyse_usage(root: Path, paths: Sequence[str], manifests: ManifestSet, config: DepsConfig) -> UsageResult:
    """Report undeclared, development-only and unused dependencies with bounded parsing."""
    result = UsageResult()
    python_files = [p for p in paths if p.endswith(".py")]
    js_files = [p for p in paths if PurePosixPath(p).suffix.casefold() in _JS_SUFFIXES]
    truncated = len(python_files) + len(js_files) > config.max_files
    if truncated:
        result.warnings.append(
            f"Dependency usage analysed the first {config.max_files} of {len(python_files) + len(js_files)} source "
            "files and skipped unused-dependency checks; raise [deps] max_files for complete results."
        )
        python_files = python_files[: config.max_files]
        js_files = js_files[: max(0, config.max_files - len(python_files))]
    corpus = _corpus(root, paths)
    _analyse_python(root, python_files, manifests, config, corpus, result)
    _analyse_node(root, js_files, manifests, config, corpus, result)
    if truncated:
        # Unused-dependency claims are unreliable when part of the source was not read.
        result.issues = [i for i in result.issues if i.kind != DependencyIssueKind.unused]
    result.issues.sort(key=lambda i: (i.ecosystem, i.manifest or "", i.kind.value, i.name.casefold()))
    return result
