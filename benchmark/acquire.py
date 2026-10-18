"""Pin public GitHub inputs and acquire bounded, never-executed source snapshots."""

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from docs_drift.web.cleanup import remove_workspace
from docs_drift.web.git import checkout, disk_bytes, resolve
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".docs-drift/repositories"
SELECTED = [
    ("pre-commit/pre-commit", "argparse"),
    ("simonw/sqlite-utils", "click"),
    ("fastapi/fastapi-cli", "Typer"),
    ("fastapi/full-stack-fastapi-template", "FastAPI"),
    ("pallets/flask", "Flask"),
    ("django-oscar/django-oscar", "Django"),
    ("tj/commander.js", "commander"),
    ("yargs/yargs", "yargs"),
    ("expressjs/express", "Express"),
    ("dubinc/dub", "Next.js, monorepo"),
    ("encode/httpx", "docs site"),
    ("withastro/astro", "TypeScript, monorepo"),
]


def pin(item: tuple[str, str]) -> dict[str, Any]:
    name, ecosystem = item
    url = "https://github.com/" + name
    location = DATA / name.replace("/", "--")
    location.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, headers={"Accept": "application/vnd.github+json"}) as client:
        metadata = client.get("https://api.github.com/repos/" + name)
        metadata.raise_for_status()
        info = metadata.json()
    sha = resolve(validate_repository(url), location, Settings(), time.monotonic() + 60)
    return {
        "name": name,
        "repo_url": url,
        "sha": sha,
        "ecosystem": ecosystem,
        "default_branch": info["default_branch"],
        "stars": info["stargazers_count"],
        "pushed_at": info["pushed_at"],
        "github_size_kib": info["size"],
        "local_path": str((location / "repository").relative_to(ROOT)).replace("\\", "/"),
    }


def clone(item: dict[str, Any], *, sweep: bool = False) -> dict[str, Any]:
    work = ROOT / item["local_path"]
    if (work / ".git").exists():
        import subprocess  # noqa: PLC0415 - acquisition verification.

        head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(work), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head.returncode == 0 and head.stdout.strip() == item["sha"]:
            return {**item, "acquisition_status": "existing", "verified_checkout": True}
        if work.parent.parent.resolve() != DATA.resolve():
            raise ValueError("Refusing to replace a checkout outside the acquisition root")
        remove_workspace(DATA, work.parent)
        work.parent.mkdir(parents=True)
    settings = Settings(max_bytes=500_000_000, max_files=150_000, history_commits=1 if sweep else 2000)
    start = time.perf_counter()
    try:
        checkout(
            validate_repository(item["repo_url"]),
            item["sha"],
            work.parent,
            settings,
            time.monotonic() + 180,
            blob_filter="blob:limit=100000",
            depth_only=True,
        )
        return {
            **item,
            "acquisition_status": "done",
            "clone_seconds": time.perf_counter() - start,
            "checkout_bytes": disk_bytes(work),
            "history": f"depth <= {settings.history_commits}; blob:limit=100000; sparse docs/source",
        }
    except Exception as exc:
        return {
            **item,
            "acquisition_status": "failed",
            "failure": f"{type(exc).__name__}: {exc}",
            "clone_seconds": time.perf_counter() - start,
        }


def select_sweep() -> list[dict[str, Any]]:
    selected: list[tuple[str, str]] = []
    rng = random.Random(20261021)
    with httpx.Client(timeout=60) as client:
        for language, count in [("Python", 8), ("JavaScript", 8), ("TypeScript", 7), ("Go", 7)]:
            response = client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": f"stars:>1000 language:{language} archived:false fork:false",
                    "sort": "stars",
                    "per_page": 100,
                },
            )
            response.raise_for_status()
            frame = response.json()["items"]
            (ROOT / f"benchmark/search-{language.lower()}.json").write_text(
                json.dumps([{"name": i["full_name"], "stars": i["stargazers_count"]} for i in frame], indent=2) + "\n"
            )
            selected.extend((i["full_name"], language) for i in rng.sample(frame, count))
            time.sleep(2)
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(pin, selected))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--clone", action="store_true")
    parser.add_argument("--extra", nargs="*")
    args = parser.parse_args()
    manifest = ROOT / ("benchmark/sweep-repos.json" if args.sweep else "benchmark/repos.json")
    if args.extra:
        manifest = ROOT / "benchmark/performance-repos.json"
    if manifest.exists():
        repos = json.loads(manifest.read_text(encoding="utf-8"))
    elif args.sweep:
        repos = select_sweep()
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            repos = list(pool.map(pin, [(n, "performance") for n in args.extra] if args.extra else SELECTED))
        manifest.write_text(json.dumps(repos, indent=2) + "\n")
    manifest.write_text(json.dumps(repos, indent=2) + "\n")
    if args.clone:
        for index, repo in enumerate(repos):
            print("acquiring", repo["name"], flush=True)
            repos[index] = clone(repo, sweep=args.sweep)
            manifest.write_text(json.dumps(repos, indent=2) + "\n")
            print(repos[index]["acquisition_status"], repos[index].get("clone_seconds"), flush=True)


if __name__ == "__main__":
    main()
