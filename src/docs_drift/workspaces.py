"""Discover workspace manifests as data, without running package managers."""

import json
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from docs_drift.config import Config
from docs_drift.index.base import source_files
from docs_drift.models import Package
from docs_drift.safe_files import metadata_text


def _data(path: Path) -> dict[str, Any]:
    try:
        text = metadata_text(path)
        value = (
            tomllib.loads(text)
            if path.suffix == ".toml"
            else yaml.safe_load(text)
            if path.suffix in {".yml", ".yaml"}
            else json.loads(text)
        )
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def detect_packages(repo: Path, files: list[Path] | None = None) -> list[Package]:  # noqa: PLR0912 - manifest formats.
    """Recognize npm/pnpm/yarn, uv, Cargo, Go, Nx and Turbo package roots."""
    files = source_files(repo, Config().exclude) if files is None else files
    roots: dict[str, Package] = {}
    for path in files:
        relative = path.parent.relative_to(repo).as_posix()
        data = _data(path) if path.name in {"package.json", "pyproject.toml", "Cargo.toml", "project.json"} else {}
        name, language, commands = None, "unknown", []
        if path.name == "package.json":
            name, language = data.get("name", path.parent.name), "javascript"
            bins = data.get("bin", {})
            commands = list(_mapping(data.get("scripts"))) + (list(bins) if isinstance(bins, dict) else [str(name)])
        elif path.name == "pyproject.toml":
            project = _mapping(data.get("project"))
            name, language = project.get("name", path.parent.name), "python"
            commands = list(_mapping(project.get("scripts")))
        elif path.name == "Cargo.toml":
            name, language = _mapping(data.get("package")).get("name", path.parent.name), "rust"
            commands = [
                str(item["name"]) for item in _items(data.get("bin")) if isinstance(item, dict) and "name" in item
            ]
        elif path.name == "go.mod":
            name, language = path.parent.name, "go"
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("module "):
                    name = line.split()[1]
                    break
        elif path.name == "project.json":
            name, language = data.get("name", path.parent.name), "javascript"
            commands = list(_mapping(data.get("targets")))
        if name:
            roots[relative] = Package(name=str(name), root=relative, language=language, commands=sorted(commands))
    # Workspace members can lack a manifest (Nx folders and virtual uv/Cargo roots).
    patterns: list[str] = []
    npm = _data(repo / "package.json").get("workspaces", [])
    patterns.extend(_items(npm.get("packages")) if isinstance(npm, dict) else _items(npm))
    patterns.extend(_items(_data(repo / "pnpm-workspace.yaml").get("packages")))
    tool = _mapping(_data(repo / "pyproject.toml").get("tool"))
    workspace = _mapping(_mapping(tool.get("uv")).get("workspace"))
    patterns.extend(_items(workspace.get("members")))
    patterns.extend(_items(_mapping(_data(repo / "Cargo.toml").get("workspace")).get("members")))
    for pattern in patterns:
        if not isinstance(pattern, str) or pattern.startswith(("!", "/")) or ".." in PurePosixPath(pattern).parts:
            continue
        for path in repo.glob(pattern):
            if path.is_dir() and not path.is_symlink() and path.resolve().is_relative_to(repo):
                relative = path.relative_to(repo).as_posix()
                roots.setdefault(relative, Package(name=path.name, root=relative, language="unknown"))
    roots.setdefault(".", Package(name=repo.name, root=".", language="unknown"))
    return sorted(roots.values(), key=lambda p: p.root)


def package_for(path: str, packages: list[Package]) -> Package:
    # Repository paths are already normalized POSIX text. Avoid constructing
    # pathlib parent objects for every symbol and package comparison on Windows.
    return max(
        (p for p in packages if p.root == "." or path.startswith(p.root + "/")),
        key=lambda p: 0 if p.root == "." else len(p.root),
    )


def docs_patterns(repo: Path) -> list[str]:
    """Include content of detected docs sites and package READMEs by default."""
    patterns = ["**/README.md"]
    markers = [
        "docusaurus.config.js",
        "docusaurus.config.ts",
        "mkdocs.yml",
        "mkdocs.yaml",
        "mint.json",
        "docs.json",
        "conf.py",
        "next.config.js",
        "next.config.mjs",
    ]
    files = source_files(repo, Config().exclude)
    for path in files:
        if path.name in markers or ".vitepress" in path.parts:
            root = path.parent.relative_to(repo).as_posix()
            prefix = "" if root == "." else root + "/"
            patterns.extend(prefix + pattern for pattern in ["**/*.md", "**/*.mdx", "**/*.rst"])
    return sorted(set(patterns))
