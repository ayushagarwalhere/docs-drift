"""Read Python and Node package manifests as data: no build backend, setup.py or package script is run."""

from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from packaging.requirements import InvalidRequirement, Requirement

from docs_drift.safe_files import metadata_text

Ecosystem = Literal["python", "node"]
Group = Literal["runtime", "optional", "dev", "build", "peer"]
MAX_INCLUDE_DEPTH = 3
_DEV_WORDS = re.compile(
    r"(?:^|[-_.])(?:dev|develop|test|tests|testing|lint|docs?|ci|typing|types|check|qa)(?:$|[-_.])", re.I
)
_REQUIREMENT_NAMES = re.compile(r"^(?:requirements?|reqs|constraints)[-_.\w]*\.(?:txt|in)$", re.I)
_CONSTRAINT_NAMES = re.compile(r"^constraints", re.I)
MANIFEST_NAMES = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "Pipfile",
    "package.json",
    ".nvmrc",
    ".node-version",
    ".python-version",
    "tox.ini",
}


def python_key(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def node_key(name: str) -> str:
    return name.strip().casefold()


@dataclass(frozen=True)
class Declared:
    """One declared dependency with the manifest line that declares it."""

    ecosystem: Ecosystem
    name: str
    key: str
    spec: str
    group: Group
    manifest: str
    line: int
    extra: str | None = None


@dataclass(frozen=True)
class Runtime:
    """A runtime requirement; pins from version files are weaker than declared ranges."""

    ecosystem: Ecosystem
    spec: str
    manifest: str
    line: int
    authoritative: bool = True


@dataclass
class Manifest:
    ecosystem: Ecosystem
    root: str
    path: str
    project_name: str | None = None
    version: str | None = None
    extras: set[str] = field(default_factory=set)
    dependencies: list[Declared] = field(default_factory=list)
    runtime: list[Runtime] = field(default_factory=list)
    private: bool = False
    scripts_text: str = ""
    extras_known: bool = False
    # An installable project lives here (pyproject.toml, setup.py or setup.cfg), even when nothing is literal.
    package: bool = False


@dataclass
class ManifestSet:
    manifests: list[Manifest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def for_path(self, path: str, ecosystem: Ecosystem | None = None) -> list[Manifest]:
        """Manifests whose directory contains `path`, nearest first."""
        parent = PurePosixPath(path).parent.as_posix()
        selected = [
            m
            for m in self.manifests
            if (ecosystem is None or m.ecosystem == ecosystem)
            and (m.root in {".", parent} or parent.startswith(m.root + "/"))
        ]
        return sorted(selected, key=lambda m: (-len(PurePosixPath(m.root).parts) if m.root != "." else 0, m.path))

    def declared(self, ecosystem: Ecosystem) -> list[Declared]:
        return [d for m in self.manifests if m.ecosystem == ecosystem for d in m.dependencies]

    def project_names(self, ecosystem: Ecosystem) -> set[str]:
        keys = python_key if ecosystem == "python" else node_key
        return {keys(m.project_name) for m in self.manifests if m.ecosystem == ecosystem and m.project_name}


def _line(lines: list[str], pattern: str, start: int = 0) -> int:
    regex = re.compile(pattern, re.I)
    for index in range(max(0, start), len(lines)):
        if regex.search(lines[index]):
            return index + 1
    for index in range(min(start, len(lines))):
        if regex.search(lines[index]):
            return index + 1
    return 1


def _header(lines: list[str], header: str) -> int:
    return _line(lines, r"^\s*\[\s*" + re.escape(header).replace(r"\.", r"\s*\.\s*") + r"\s*\]") - 1


def _requirement(text: str) -> Requirement | None:
    text = text.strip()
    if not text or text.startswith(("-", ".", "/", "git+", "http:", "https:", "file:")) or len(text) > 512:  # noqa: PLR2004
        return None
    try:
        return Requirement(text)
    except InvalidRequirement:
        return None


def _python_dep(  # noqa: PLR0913, PLR0917 - declaration context.
    text: str, group: Group, manifest: str, lines: list[str], start: int, extra: str | None = None
) -> Declared | None:
    requirement = _requirement(text)
    if requirement is None:
        return None
    line = _line(lines, r"""(?:^|["'\s])""" + re.escape(requirement.name) + r"""(?:\s*[\[<>=!~;@"',]|\s*$)""", start)
    return Declared(
        "python",
        requirement.name,
        python_key(requirement.name),
        str(requirement.specifier),
        group,
        manifest,
        line,
        extra,
    )


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _extra_group(name: str) -> Group:
    return "dev" if _DEV_WORDS.search(name) else "optional"


def _pyproject(path: Path, relative: str, manifest: Manifest) -> None:  # noqa: PLR0912 - manifest tables.
    text = metadata_text(path)
    data = tomllib.loads(text)
    lines = text.splitlines()
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    assert isinstance(project, dict)
    manifest.package = manifest.package or bool(
        project or "build-system" in data or (isinstance(data.get("tool"), dict) and "poetry" in data["tool"])
    )
    if isinstance(project.get("name"), str):
        manifest.project_name = project["name"]
    if project:
        dynamic = project.get("dynamic")
        manifest.extras_known = not (isinstance(dynamic, list) and "optional-dependencies" in dynamic)
    if isinstance(project.get("version"), str):
        manifest.version = project["version"]
    requires = project.get("requires-python")
    if isinstance(requires, str):
        manifest.runtime.append(Runtime("python", requires, relative, _line(lines, r"^\s*requires-python\s*=")))
    start = _header(lines, "project")
    for item in _strings(project.get("dependencies")):
        if dep := _python_dep(item, "runtime", relative, lines, start):
            manifest.dependencies.append(dep)
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        start = _header(lines, "project.optional-dependencies")
        for extra, items in optional.items():
            manifest.extras.add(python_key(extra))
            for item in _strings(items):
                if dep := _python_dep(item, _extra_group(extra), relative, lines, start, extra):
                    manifest.dependencies.append(dep)
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        start = _header(lines, "dependency-groups")
        for name, items in groups.items():
            for item in _strings(items):
                if dep := _python_dep(item, "dev", relative, lines, start, str(name)):
                    manifest.dependencies.append(dep)
    build = data.get("build-system")
    if isinstance(build, dict):
        start = _header(lines, "build-system")
        for item in _strings(build.get("requires")):
            if dep := _python_dep(item, "build", relative, lines, start):
                manifest.dependencies.append(dep)
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    assert isinstance(tool, dict)
    uv = tool.get("uv")
    if isinstance(uv, dict):
        start = _header(lines, "tool.uv")
        for item in _strings(uv.get("dev-dependencies")):
            if dep := _python_dep(item, "dev", relative, lines, start):
                manifest.dependencies.append(dep)
    pdm = tool.get("pdm")
    if isinstance(pdm, dict) and isinstance(pdm.get("dev-dependencies"), dict):
        start = _header(lines, "tool.pdm.dev-dependencies")
        for name, items in pdm["dev-dependencies"].items():
            for item in _strings(items):
                if dep := _python_dep(item, "dev", relative, lines, start, str(name)):
                    manifest.dependencies.append(dep)
    poetry = tool.get("poetry")
    if isinstance(poetry, dict):
        _poetry(poetry, relative, lines, manifest)


def _poetry_spec(value: object) -> tuple[str, bool]:
    if isinstance(value, str):
        return value, False
    if isinstance(value, dict):
        version = value.get("version")
        return (version if isinstance(version, str) else ""), bool(value.get("optional"))
    return "", False


def _poetry(poetry: dict[str, Any], relative: str, lines: list[str], manifest: Manifest) -> None:
    if isinstance(poetry.get("name"), str) and not manifest.project_name:
        manifest.project_name = poetry["name"]
        manifest.extras_known = True
    if isinstance(poetry.get("version"), str) and not manifest.version:
        manifest.version = poetry["version"]
    extras = poetry.get("extras")
    if isinstance(extras, dict):
        manifest.extras.update(python_key(name) for name in extras)
    tables: list[tuple[str, object, Group]] = [
        ("tool.poetry.dependencies", poetry.get("dependencies"), "runtime"),
        ("tool.poetry.dev-dependencies", poetry.get("dev-dependencies"), "dev"),
    ]
    groups = poetry.get("group")
    if isinstance(groups, dict):
        for name, group in sorted(groups.items()):
            if isinstance(group, dict):
                tables.append((f"tool.poetry.group.{name}.dependencies", group.get("dependencies"), "dev"))
    for header, table, group in tables:
        if not isinstance(table, dict):
            continue
        start = _header(lines, header)
        for name, value in table.items():
            spec, optional = _poetry_spec(value)
            line = _line(lines, r"^\s*[\"']?" + re.escape(name) + r"[\"']?\s*=", start)
            if name.casefold() == "python":
                if header == "tool.poetry.dependencies" and spec:
                    manifest.runtime.append(Runtime("python", spec, relative, line))
                continue
            manifest.dependencies.append(
                Declared("python", name, python_key(name), spec, "optional" if optional else group, relative, line)
            )


def is_constraints_file(relative: str) -> bool:
    return bool(_CONSTRAINT_NAMES.match(PurePosixPath(relative).name))


def _requirements(  # noqa: PLR0913, PLR0917 - include traversal state.
    path: Path,
    relative: str,
    root: Path,
    manifest: Manifest,
    seen: set[str],
    depth: int = 0,
    *,
    constraint: bool = False,
) -> None:
    if relative in seen or depth > MAX_INCLUDE_DEPTH:
        return
    seen.add(relative)
    text = metadata_text(path)
    lines = text.splitlines()
    pure = PurePosixPath(relative)
    dev = any(_DEV_WORDS.search(part) for part in (*pure.parts[:-1], pure.stem))
    # pip-compile output pins the whole transitive closure; those pins are not direct dependencies.
    # Constraint files (constraints*.txt, or anything named by -c) only pin versions, they install nothing.
    locked = "# via" in text or "--hash=" in text or constraint or is_constraints_file(relative)
    group: Group = "dev" if dev else "optional" if locked else "runtime"
    for number, raw in enumerate(lines, 1):
        content = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        include = re.match(r"^(-r|--requirement|-c|--constraint)\s*=?\s*(\S+)$", content)
        if include:
            target = (path.parent / include[2]).resolve()
            if target.is_relative_to(root.resolve()) and target.is_file():
                _requirements(
                    target,
                    target.relative_to(root.resolve()).as_posix(),
                    root,
                    manifest,
                    seen,
                    depth + 1,
                    constraint=constraint or include[1] in {"-c", "--constraint"},
                )
            continue
        requirement = _requirement(content)
        if requirement is not None:
            manifest.dependencies.append(
                Declared(
                    "python",
                    requirement.name,
                    python_key(requirement.name),
                    str(requirement.specifier),
                    group,
                    relative,
                    number,
                )
            )


def _setup_cfg(path: Path, relative: str, manifest: Manifest) -> None:
    text = metadata_text(path)
    lines = text.splitlines()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    manifest.package = manifest.package or parser.has_section("metadata") or parser.has_section("options")
    if parser.has_option("metadata", "name"):
        manifest.project_name = parser.get("metadata", "name").strip() or None
        manifest.extras_known = True
    if parser.has_option("options", "python_requires"):
        manifest.runtime.append(
            Runtime(
                "python", parser.get("options", "python_requires").strip(), relative, _line(lines, r"python_requires")
            )
        )
    if parser.has_option("options", "install_requires"):
        start = _line(lines, r"^\s*install_requires\s*=")
        for item in parser.get("options", "install_requires").splitlines():
            if dep := _python_dep(item.split(";")[0], "runtime", relative, lines, start - 1):
                manifest.dependencies.append(dep)
    if parser.has_section("options.extras_require"):
        start = _header(lines, "options.extras_require")
        for extra, value in parser.items("options.extras_require"):
            manifest.extras.add(python_key(extra))
            for item in value.splitlines():
                if dep := _python_dep(item.split(";")[0], _extra_group(extra), relative, lines, start, extra):
                    manifest.dependencies.append(dep)


def _literal(node: ast.AST | None, names: Mapping[str, ast.AST]) -> object:
    if isinstance(node, ast.Name) and node.id in names:
        node = names[node.id]
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _setup_py(path: Path, relative: str, manifest: Manifest) -> None:
    """Read literal setup() keywords from the syntax tree; the script itself is never executed."""
    text = metadata_text(path)
    lines = text.splitlines()
    tree = ast.parse(text)
    manifest.package = True
    names = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        expanded = any(keyword.arg is None for keyword in node.keywords)  # setup(**options)
        name = _literal(keywords.get("name"), names)
        if isinstance(name, str):
            manifest.project_name = name
        requires = _literal(keywords.get("python_requires"), names)
        if isinstance(requires, str):
            manifest.runtime.append(Runtime("python", requires, relative, _line(lines, r"python_requires")))
        for item in _strings(_literal(keywords.get("install_requires"), names)):
            if dep := _python_dep(item.split(";")[0], "runtime", relative, lines, 0):
                manifest.dependencies.append(dep)
        extras = _literal(keywords.get("extras_require"), names)
        # Extras are known only from a literal dict, or when no ** expansion could be supplying them.
        manifest.extras_known = isinstance(extras, dict) or ("extras_require" not in keywords and not expanded)
        if isinstance(extras, dict):
            for extra, items in extras.items():
                if not isinstance(extra, str):
                    continue
                manifest.extras.add(python_key(extra.split(":")[0]))
                for item in _strings(items):
                    if dep := _python_dep(item.split(";")[0], _extra_group(extra), relative, lines, 0, extra):
                        manifest.dependencies.append(dep)
        return


def _pipfile(path: Path, relative: str, manifest: Manifest) -> None:
    text = metadata_text(path)
    data = tomllib.loads(text)
    lines = text.splitlines()
    tables: tuple[tuple[str, Group], ...] = (("packages", "runtime"), ("dev-packages", "dev"))
    for table, group in tables:
        values = data.get(table)
        if not isinstance(values, dict):
            continue
        start = _header(lines, table)
        for name, value in values.items():
            spec, _ = _poetry_spec(value)
            line = _line(lines, r"^\s*[\"']?" + re.escape(name) + r"[\"']?\s*=", start)
            spec = "" if spec == "*" else spec
            manifest.dependencies.append(Declared("python", name, python_key(name), spec, group, relative, line))
    requires = data.get("requires")
    if isinstance(requires, dict) and isinstance(requires.get("python_version"), str):
        manifest.runtime.append(
            Runtime(
                "python",
                ">=" + requires["python_version"],
                relative,
                _line(lines, r"python_version"),
                authoritative=False,
            )
        )


def _tox(path: Path, relative: str, manifest: Manifest) -> None:
    text = metadata_text(path)
    lines = text.splitlines()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    for section in parser.sections():
        if section.startswith("testenv") and parser.has_option(section, "deps"):
            start = _header(lines, section)
            for item in parser.get(section, "deps").splitlines():
                if dep := _python_dep(item.split(";")[0], "dev", relative, lines, start):
                    manifest.dependencies.append(dep)
    if parser.has_option("tox", "envlist"):
        versions = sorted(
            (int(major), int(minor))
            for major, minor in re.findall(r"\bpy(\d)(\d{1,2})\b", parser.get("tox", "envlist"))
        )
        if versions:
            major, minor = versions[0]
            manifest.runtime.append(
                Runtime("python", f">={major}.{minor}", relative, _line(lines, r"envlist"), authoritative=False)
            )


def _package_json(path: Path, relative: str, manifest: Manifest) -> None:
    text = metadata_text(path)
    data = json.loads(text.removeprefix(chr(0xFEFF)))
    if not isinstance(data, dict):
        return
    lines = text.splitlines()
    if isinstance(data.get("name"), str):
        manifest.project_name = data["name"]
    if isinstance(data.get("version"), str):
        manifest.version = data["version"]
    manifest.private = data.get("private") is True
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        manifest.scripts_text = "\n".join(str(value) for value in scripts.values())
    engines = data.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        manifest.runtime.append(
            Runtime("node", engines["node"], relative, _line(lines, r'"node"\s*:', _line(lines, r'"engines"\s*:') - 1))
        )
    tables: tuple[tuple[str, Group], ...] = (
        ("dependencies", "runtime"),
        ("devDependencies", "dev"),
        ("peerDependencies", "peer"),
        ("optionalDependencies", "optional"),
    )
    for table, group in tables:
        values = data.get(table)
        if not isinstance(values, dict):
            continue
        start = _line(lines, r'"' + table + r'"\s*:') - 1
        for name, spec in values.items():
            if not isinstance(name, str):
                continue
            line = _line(lines, r'"' + re.escape(name) + r'"\s*:', start)
            manifest.dependencies.append(
                Declared("node", name, node_key(name), spec if isinstance(spec, str) else "", group, relative, line)
            )


def _version_file(path: Path, relative: str, manifest: Manifest) -> None:
    text = metadata_text(path).strip().splitlines()
    value = text[0].strip() if text else ""
    match = re.match(r"^v?(\d+(?:\.\d+){0,2})", value)
    if match:
        manifest.runtime.append(Runtime(manifest.ecosystem, ">=" + match[1], relative, 1, authoritative=False))


def _demote_requirement_pins(manifest: Manifest) -> None:
    """Beside a package's declared dependencies, requirements files are development or CI pins."""
    packaged = any(
        d.group == "runtime" and not _REQUIREMENT_NAMES.match(PurePosixPath(d.manifest).name)
        for d in manifest.dependencies
    )
    if packaged:
        manifest.dependencies = [
            replace(d, group="dev")
            if d.group == "runtime" and _REQUIREMENT_NAMES.match(PurePosixPath(d.manifest).name)
            else d
            for d in manifest.dependencies
        ]


def _is_manifest(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.name in MANIFEST_NAMES:
        return True
    return bool(_REQUIREMENT_NAMES.match(path.name)) or (
        path.parent.name.casefold() == "requirements" and path.suffix in {".txt", ".in"}
    )


def load_manifests(root: Path, paths: Iterable[str]) -> ManifestSet:  # noqa: PLR0912 - one branch per format.
    """Parse every recognised manifest among repository-relative `paths`, isolating failures per file."""
    result = ManifestSet()
    by_key: dict[tuple[str, Ecosystem], Manifest] = {}
    seen_requirements: set[str] = set()
    for relative in sorted(p for p in paths if _is_manifest(p)):
        path = PurePosixPath(relative)
        name = path.name
        ecosystem: Ecosystem = "node" if name in {"package.json", ".nvmrc", ".node-version"} else "python"
        directory = path.parent
        if directory.name.casefold() == "requirements":
            directory = directory.parent
        directory_text = directory.as_posix()
        key = (directory_text, ecosystem)
        manifest = by_key.get(key)
        if manifest is None:
            manifest = Manifest(ecosystem, directory_text, relative)
            by_key[key] = manifest
        if name in {"pyproject.toml", "package.json"} or (
            is_constraints_file(manifest.path) and not is_constraints_file(relative)
        ):
            manifest.path = relative
        absolute = root / relative
        try:
            if name == "pyproject.toml":
                _pyproject(absolute, relative, manifest)
            elif name == "setup.cfg":
                _setup_cfg(absolute, relative, manifest)
            elif name == "setup.py":
                _setup_py(absolute, relative, manifest)
            elif name == "Pipfile":
                _pipfile(absolute, relative, manifest)
            elif name == "tox.ini":
                _tox(absolute, relative, manifest)
            elif name == "package.json":
                _package_json(absolute, relative, manifest)
            elif name in {".nvmrc", ".node-version", ".python-version"}:
                _version_file(absolute, relative, manifest)
            else:
                _requirements(absolute, relative, root, manifest, seen_requirements)
        except (OSError, ValueError, SyntaxError, RecursionError, configparser.Error, tomllib.TOMLDecodeError) as exc:
            result.warnings.append(f"Dependency manifest skipped: {relative}: {type(exc).__name__}: {exc}")
    for manifest in by_key.values():
        _demote_requirement_pins(manifest)
    # Package manifests stay even when nothing in them is literal, so lookups stop at the nearest project.
    result.manifests = [
        m for _, m in sorted(by_key.items()) if m.dependencies or m.runtime or m.project_name or m.extras or m.package
    ]
    return result
