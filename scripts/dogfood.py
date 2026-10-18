"""Check all current own-source/docs files in an isolated staged checkout, without changing the user's index."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".docs-drift/dogfood/path with spaces/repository"


def prepare() -> Path:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if not (TARGET / ".git").exists():
        # Copy history into a checkout owned by this process. No global trust or
        # source index changes are needed when Windows owners differ.
        if TARGET.resolve() != (ROOT / ".docs-drift/dogfood/path with spaces/repository").resolve():
            raise ValueError("Unexpected dogfood destination")
        shutil.copytree(ROOT / ".git", TARGET / ".git", symlinks=True)
        subprocess.run(
            ["git", "-C", str(TARGET), "-c", f"core.hooksPath={os.devnull}", "reset", "--hard", "HEAD"],
            check=True,
            capture_output=True,
        )
    raw = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    current_paths = {
        os.fsdecode(value)
        for value in raw.split(b"\0")
        if value and (ROOT / os.fsdecode(value)).is_file() and not (ROOT / os.fsdecode(value)).is_symlink()
    }
    previous_paths = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=TARGET,
        capture_output=True,
        check=True,
    ).stdout
    for value in previous_paths.split(b"\0"):
        if value and os.fsdecode(value) not in current_paths:
            stale = TARGET / os.fsdecode(value)
            if stale.is_file() and stale.resolve().is_relative_to(TARGET.resolve()):
                stale.unlink()
    for value in raw.split(b"\0"):
        if not value:
            continue
        relative = os.fsdecode(value)
        source = ROOT / relative
        if source.is_file() and not source.is_symlink():
            target = TARGET / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    subprocess.run(
        ["git", "-C", str(TARGET), "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "add", "--all"],
        check=True,
        capture_output=True,
    )
    return TARGET


def main() -> None:
    repo = prepare()
    output = ROOT / ".docs-drift/dogfood-output"
    output.mkdir(parents=True, exist_ok=True)
    count = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    )
    earliest = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    commands = [
        ["scan", "--format", "json", "--output", str(output / "scan.json")],
        ["insights", "--format", "json", "--output", str(output / "insights.json")],
        ["impact", "--base", "HEAD~10", "--format", "json", "--output", str(output / "impact-last10.json")],
        ["impact", "--base", earliest, "--format", "json", "--output", str(output / "impact-available.json")],
        ["fix", "--patch", str(output / "fix.patch")],
        ["check", "--quiet"],
    ]
    reports = []
    for args in commands:
        command = [
            sys.executable,
            "-c",
            "from docs_drift.cli import app; app()",
            *args,
            "--repo",
            str(repo),
            "--no-color",
        ]
        print("dogfood", " ".join(args), flush=True)
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        reports.append({"args": args, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        print("exit", result.returncode, flush=True)
    (output / "commands.json").write_text(
        json.dumps({"available_commits": count, "repo": repo.as_posix(), "commands": reports}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
