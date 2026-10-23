"""Verify the exact generated-hash exception used by the history secret scan."""

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any


def verify_document(data: Any) -> int:
    """Recompute grouping hashes; never print a mismatched value or source text."""
    count = 0
    if isinstance(data, dict):
        if data.get("group_key") is not None:
            if not isinstance(data.get("kind"), str) or not isinstance(data.get("value"), str):
                raise ValueError("Grouping hash has no claim kind/value.")
            parts = [data.get(key) or "" for key in ("kind", "value", "qualifier")]
            if not all(isinstance(part, str) for part in parts):
                raise ValueError("Invalid grouping hash input.")
            expected = hashlib.sha256("\0".join(parts).encode()).hexdigest()
            if data["group_key"] != expected:
                raise ValueError("Grouping hash does not match its claim fields.")
            count += 1
        count += sum(verify_document(value) for value in data.values())
    elif isinstance(data, list):
        count += sum(verify_document(value) for value in data)
    return count


def verify_repository(repo: Path, *, history: bool = False) -> tuple[int, int]:
    config = tomllib.loads((repo / ".gitleaks.toml").read_text(encoding="utf-8"))
    rule = next(rule for rule in config["rules"] if rule["id"] == "generic-api-key")
    patterns = rule["allowlists"][0]["paths"]

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8")

    names = git("log", "--all", "--format=", "--name-only", "--", "*.json").splitlines()
    files = sorted({name for name in names if any(re.fullmatch(pattern, name) for pattern in patterns)})
    documents, groups = 0, 0
    for name in files:
        path = repo / name
        if path.is_file():
            groups += verify_document(json.loads(path.read_text(encoding="utf-8")))
            documents += 1
    if history and files:
        objects = git("rev-list", "--objects", "--all", "--", *files)
        for row in objects.splitlines():
            sha, _, name = row.partition(" ")
            if name in files:
                try:
                    groups += verify_document(json.loads(git("show", sha)))
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Invalid generated hash in {name}, blob {sha}.") from exc
                documents += 1
    return documents, groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    documents, groups = verify_repository(args.repo, history=args.history)
    print(f"Verified {groups} generated hashes across {documents} current/historical JSON documents.")


if __name__ == "__main__":
    main()
