"""Validate release tags, archives and installed-wheel tests without publishing."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

TAG = re.compile(r"v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?:-rc\.(?P<rc>[1-9]\d*))?")
MAX_RECORDING_BYTES = 1_000_000
REQUIRED = (
    "policy.py",
    "portfolio.py",
    "report/portfolio.html",
    "index/queries/typescript.scm",
    "index/queries/javascript.scm",
    "graph/queries/evidence_path.cypher",
    "report/report.html",
    "report/partials/body.html",
    "report/partials/script.js",
    "report/partials/report.css",
    "report/vendor/cytoscape.min.js",
    "report/vendor/CYTOSCAPE-LICENSE",
    "schemas/scan-result.v1.json",
    "schemas/sarif-schema-2.1.0.json",
    "schemas/SARIF-LICENSE.md",
    "schemas/SARIF-NOTICES.txt",
    "llm/prompts/verify_v1.md",
    "llm/prompts/rewrite_v1.md",
    "web/templates/landing.html",
    "web/static/demo.js",
    "web/static/demo.css",
    "web/examples/py-cli.json",
)


def tag_version(tag: str) -> str:
    """Accept SemVer stable/RC tags and return the normalized Python version."""
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Use v0.1.0 or v0.1.0-rc.1; moving v0 and submission tags are not releases.")
    return match["version"] + ("rc" + match["rc"] if match["rc"] else "")


def notes(changelog: str, version: str) -> str:
    """Extract the exact base-version entry, never the Unreleased section."""
    base = re.sub(r"rc\d+$", "", version)
    match = re.search(rf"^## \[{re.escape(base)}\][^\n]*\n(.*?)(?=^## |^\[Unreleased\]:|\Z)", changelog, re.M | re.S)
    if not match:
        raise ValueError(f"Missing changelog section for {base}")
    heading = f"Release candidate {version}; not the stable release.\n\n" if "rc" in version else ""
    return heading + match[0].strip() + "\n"


def check_archives(dist: Path, expected: str | None = None) -> str:
    """Fail on omitted runtime files, accidental corpora or misleading metadata."""
    wheels, sources = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Use a fresh dist directory containing exactly one wheel and one sdist.")
    with ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        missing = {"docs_drift/" + name for name in REQUIRED} - names
        if missing:
            raise ValueError(f"Wheel missing runtime resources: {sorted(missing)}")
        metadata = Parser().parsestr(wheel.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode())
        version = str(metadata["Version"])
        if expected and version != expected:
            raise ValueError(f"Tag expects {expected}; wheel contains {version}")
        body = metadata.get_payload()
        if not isinstance(body, str) or "raw.githubusercontent.com/ayushagarwalhere/docs-drift/" not in body:
            raise ValueError("Missing PyPI README or absolute image URLs")
        if re.search(r"!\[[^\]]*\]\((?!https?://)", body):
            raise ValueError("PyPI README still contains a relative image")
        if not metadata.get_all("Project-URL") or not metadata.get_all("Classifier") or not metadata["Keywords"]:
            raise ValueError("Incomplete public metadata")
    with tarfile.open(sources[0]) as source:
        files = [member for member in source.getmembers() if member.isfile()]
        paths = {member.name.partition("/")[2] for member in files}
        if not {"tests/fixtures/build_fixtures.py", "tests/test_cli.py", "scripts/demo_setup.py"} <= paths:
            raise ValueError("sdist missing tests or fixture/demo builder")
        for member in files:
            path = member.name.partition("/")[2]
            if any(part in path.split("/") for part in (".git", ".venv", ".docs-drift", "node_modules")):
                raise ValueError(f"Local/generated data in sdist: {path}")
            if path.startswith(("tests/fixtures/repos/", "benchmark/runs/")):
                raise ValueError(f"Generated repositories in sdist: {path}")
            if path.endswith((".cast", ".mp4", ".gif", ".vsix")) and member.size > MAX_RECORDING_BYTES:
                raise ValueError(f"Recording exceeds one MB: {path}")
    print(json.dumps({"wheel": wheels[0].name, "sdist": sources[0].name, "version": version, "content_checks": "pass"}))
    return version


def stage_tests(root: Path, destination: Path) -> None:
    """Copy tracked/test inputs, with no src tree or editable install path."""
    destination.mkdir(parents=True, exist_ok=False)
    raw = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    for name in set(os.fsdecode(raw).split("\0")):
        if not name or name.split("/")[0] in {"src", ".git", "dist"}:
            continue
        source = root / name
        if source.is_file() and not source.is_symlink():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "notes", "stage-tests"])
    parser.add_argument("--tag")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "stage-tests":
        if args.out is None:
            parser.error("--out required")
        stage_tests(Path.cwd(), args.out)
    elif args.command == "check":
        check_archives(args.dist, tag_version(args.tag) if args.tag else None)
    else:
        if not args.tag or args.out is None:
            parser.error("--tag and --out required")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            notes(Path("CHANGELOG.md").read_text(encoding="utf-8"), tag_version(args.tag)), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
