"""Verify an exact indexed release through pipx, uv tool or pip in isolated homes."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import venv
from pathlib import Path
from urllib.parse import urlparse


def requirement(index: str, version: str, extras: bool = False) -> str:
    """Pin TestPyPI's own wheel/hash while resolving dependencies only from PyPI."""
    name = "docs-drift[web,graph,llm]" if extras else "docs-drift"
    if index == "pypi":
        return f"{name}=={version}"
    with urllib.request.urlopen(f"https://test.pypi.org/pypi/docs-drift/{version}/json", timeout=30) as response:
        data = json.load(response)
    wheel = next(item for item in data["urls"] if item["filename"].endswith("py3-none-any.whl"))
    url = wheel["url"]
    if urlparse(url).scheme != "https" or urlparse(url).hostname != "test-files.pythonhosted.org":
        raise ValueError("Unexpected TestPyPI artifact host")
    return f"{name} @ {url}#sha256={wheel['digests']['sha256']}"


def verify(method: str, index: str, version: str, builder: Path) -> None:
    """Install in temporary directories; never replace a developer's global tools."""
    with tempfile.TemporaryDirectory(prefix="docs-drift-install-") as temporary:
        root = Path(temporary)
        binaries = root / "bin"
        env = {
            **os.environ,
            "PIPX_HOME": str(root / "pipx"),
            "PIPX_BIN_DIR": str(binaries),
            "PIPX_MAN_DIR": str(root / "man"),
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(binaries),
            "PYTHONUTF8": "1",
        }
        spec = requirement(index, version, method == "pip")
        executable = binaries / ("docs-drift.exe" if os.name == "nt" else "docs-drift")
        if method == "pipx":
            subprocess.run(
                [sys.executable, "-m", "pipx", "install", "--python", sys.executable, spec], env=env, check=True
            )
        elif method == "uv":
            subprocess.run(["uv", "tool", "install", "--python", sys.executable, spec], env=env, check=True)
        else:
            target = root / "venv"
            venv.create(target, with_pip=True)
            directory = target / ("Scripts" if os.name == "nt" else "bin")
            python = directory / ("python.exe" if os.name == "nt" else "python")
            subprocess.run([str(python), "-m", "pip", "install", spec], env=env, check=True)
            executable = directory / executable.name
        installed = subprocess.check_output([str(executable), "--version"], env=env, text=True).strip()
        if installed != version:
            raise ValueError(f"Expected {version}; installed {installed}")
        subprocess.run([sys.executable, str(builder), "--output", str(root / "repos")], check=True, env=env)
        report = root / "scan.json"
        subprocess.run(
            [
                str(executable),
                "scan",
                "--repo",
                str(root / "repos/py-cli"),
                "--format",
                "json",
                "--output",
                str(report),
                "--no-color",
            ],
            cwd=root,
            env=env,
            check=True,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        if data["package_version"] != version or data["schema_version"] != 1 or not data["findings"]:
            raise ValueError("Installed scan contract or fixture findings failed")
        print(f"PASS {sys.platform} Python {sys.version.split()[0]} {method} {index} {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=["pipx", "uv", "pip"], required=True)
    parser.add_argument("--index", choices=["pypi", "testpypi"], required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    verify(args.method, args.index, args.version, Path("tests/fixtures/build_fixtures.py").resolve())


if __name__ == "__main__":
    main()
