"""Build text-only fixture repositories with reproducible Git objects and histories."""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


@dataclass(frozen=True)
class Commit:
    """A complete, explicitly authored step in a fixture's history."""

    message: str
    author: str
    email: str
    date: str
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Repository:
    name: str
    commits: tuple[Commit, ...]


def _text(value: str) -> str:
    return dedent(value).strip() + "\n"


def _commit(day: int, message: str, *files: tuple[str, str]) -> Commit:
    authors = (
        ("Ada Chen", "ada@example.invalid"),
        ("Priya Shah", "priya@example.invalid"),
        ("Omar Ali", "omar@example.invalid"),
        ("Mina Park", "mina@example.invalid"),
        ("Leo Silva", "leo@example.invalid"),
    )
    author, email = authors[day - 1]
    return Commit(message, author, email, f"2026-10-{day:02d}T09:00:00+00:00", files)


def _py_cli() -> Repository:
    readme = _text("""
        # py-cli

        The CLI has a `serve` subcommand.
        - `serve` accepts `--port` (default 8000).
        - `serve` accepts `--host` (default 127.0.0.1).
        - The `--workers` default is `4`.
        - Set `APP_DEBUG` to enable debug logging.
        - Use the `migrate` subcommand to upgrade the database.

        Run `make test` to check the project.
    """)
    source = _text("""
        import argparse
        import os

        def main():
            parser = argparse.ArgumentParser(prog="py-cli")
            commands = parser.add_subparsers(dest="command", required=True)
            serve = commands.add_parser("serve")
            serve.add_argument("--port", type=int, default=8000)
            serve.add_argument("--host", default="127.0.0.1")
            serve.add_argument("--workers", type=int, default=4)
            commands.add_parser("migrate")
            debug = os.environ.get("APP_DEBUG", "0")
            return parser.parse_args(), debug
    """)
    initial = _commit(
        1,
        "Document the original CLI",
        ("README.md", readme),
        ("cli.py", source),
        ("Makefile", "test:\n\tpython -m unittest discover\n"),
    )
    source = source.replace('"--port"', '"--listen"')
    port = _commit(2, "Rename port to listen", ("cli.py", source))
    source = source.replace('"--workers", type=int, default=4', '"--workers", type=int, default=8')
    workers = _commit(3, "Increase the worker default", ("cli.py", source))
    source = source.replace(
        'debug = os.environ.get("APP_DEBUG", "0")', 'log_level = os.environ.get("APP_LOG_LEVEL", "INFO")'
    )
    source = source.replace("parser.parse_args(), debug", "parser.parse_args(), log_level")
    env = _commit(4, "Replace debug environment variable with log level", ("cli.py", source))
    source = source.replace('commands.add_parser("migrate")', 'commands.add_parser("db-upgrade")')
    migrate = _commit(5, "Rename the database upgrade subcommand", ("cli.py", source))
    return Repository("py-cli", (initial, port, workers, env, migrate))


def _fastapi_svc() -> Repository:
    readme = _text("""
        # fastapi-svc

        - Read one user with `GET /users/{id}`.
        - Sign in with `POST /login`.
        - Configure the settings field `request_timeout`.
        - The request timeout defaults to `30` seconds.
        - Create the application with `create_app`.
    """)
    source = _text("""
        from fastapi import FastAPI
        from pydantic import BaseModel

        class Settings(BaseModel):
            request_timeout: int = 30

        def create_app():
            app = FastAPI()

            @app.get("/users/{id}")
            def get_user(id: int):
                return {"id": id}

            @app.post("/login")
            def login():
                return {"authenticated": True}

            return app
    """)
    initial = _commit(1, "Document service routes and settings", ("README.md", readme), ("service.py", source))
    source = source.replace('"/users/{id}"', '"/v2/users/{id}"')
    users = _commit(2, "Move user lookup to v2", ("service.py", source))
    source = source.replace('    @app.post("/login")\n    def login():\n        return {"authenticated": True}\n\n', "")
    login = _commit(3, "Remove the login endpoint", ("service.py", source))
    source = source.replace("request_timeout: int = 30", "timeout_seconds: int = 60")
    timeout = _commit(4, "Rename timeout setting and increase its default", ("service.py", source))
    source = source.replace("def create_app():", "def build_app():")
    factory = _commit(5, "Rename the application factory", ("service.py", source))
    prose = Commit(
        "Document the stale behavioural timeout",
        "Ada Chen",
        "ada@example.invalid",
        "2026-10-06T09:00:00+00:00",
        (("README.md", readme + "\nRequests time out after 30 seconds.\n"),),
    )
    return Repository("fastapi-svc", (initial, users, login, timeout, factory, prose))


def _node_tool() -> Repository:
    readme = _text("""
        # node-tool

        - Build the release with `npm run build:prod`.
        - The API URL comes from `process.env.API_URL`.
        - The service exposes `GET /health`.

        Preview a deployment:

        ```sh
        npx node-tool deploy --dry-run
        ```
    """)
    package = _text("""
        {
          "name": "node-tool",
          "version": "1.0.0",
          "bin": {"node-tool": "dist/cli.js"},
          "scripts": {"build:prod": "tsc"},
          "dependencies": {"commander": "^12.0.0", "express": "^4.0.0"},
          "devDependencies": {"typescript": "^5.0.0"}
        }
    """)
    source = _text("""
        import { Command } from "commander";
        import express from "express";

        const program = new Command().name("node-tool");
        const apiUrl = process.env.API_URL;
        program.command("deploy").option("--dry-run").action(() => {
          console.log(apiUrl);
        });
        const app = express();
        app.get("/health", (_req, res) => res.send("ok"));
        program.parse();
    """)
    initial = _commit(
        1, "Document the TypeScript tool", ("README.md", readme), ("package.json", package), ("src/cli.ts", source)
    )
    package = package.replace('"build:prod"', '"build"')
    build = _commit(2, "Rename the release build script", ("package.json", package))
    source = source.replace("process.env.API_URL", "process.env.API_BASE_URL")
    env = _commit(3, "Rename the API base URL variable", ("src/cli.ts", source))
    source = source.replace('"/health"', '"/healthz"')
    health = _commit(4, "Rename the health route", ("src/cli.ts", source))
    source = source.replace('.option("--dry-run")', "")
    dry_run = _commit(5, "Remove deployment dry run", ("src/cli.ts", source))
    return Repository("node-tool", (initial, build, env, health, dry_run))


def _clean_repo() -> Repository:
    readme = _text("""
        # clean-repo

        ## Current usage

        Start the service with `clean-tool serve --listen 9000`.
        The `--workers` default is `8`.
        The `APP_LOG_LEVEL` environment variable controls verbosity.
        Run `make test`.

        An example person is `--name alice`; this is example data, not an option of clean-tool.

        ## Migrating from v1

        Historical only: v1 used `--port`; that flag was removed. Use `--listen` today.

        ## Third-party tools

        ```sh
        git commit --amend
        pip install -e .
        ```
    """)
    source = _text("""
        import argparse
        import os

        def main():
            parser = argparse.ArgumentParser(prog="clean-tool")
            serve = parser.add_subparsers(dest="command", required=True).add_parser("serve")
            serve.add_argument("--listen", type=int, default=9000)
            serve.add_argument("--workers", type=int, default=8)
            log_level = os.environ.get("APP_LOG_LEVEL", "INFO")
            return parser.parse_args(), log_level
    """)
    original = "# clean-repo v1\n\nStart with `clean-tool serve --port 9000`.\n"
    initial = _commit(
        1,
        "Document the v1 interface",
        ("README.md", original),
        ("cli.py", source.replace('"--listen"', '"--port"')),
        ("Makefile", "test:\n\tpython -m unittest discover\n"),
    )
    changelog = "# Changelog\n\n## v1 (historical)\n\nThe old release accepted `--port` and `--debug`.\n"
    nested_changelog = "# Archived changelog\n\nVersion 1 described the old `--port` flag.\n"
    current = _commit(
        2,
        "Update docs together with the v2 interface",
        ("README.md", readme),
        ("cli.py", source),
        ("CHANGELOG.md", changelog),
        ("docs/CHANGELOG.md", nested_changelog),
    )
    return Repository("clean-repo", (initial, current))


def repositories() -> tuple[Repository, ...]:
    """The script is the fixture specification, independent of the answer key and detector."""
    return _py_cli(), _fastapi_svc(), _node_tool(), _clean_repo()


def _environment() -> dict[str, str]:
    # Ignore caller index/worktree/object-format/config/identity settings and user filters.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def _git(repo: Path, env: dict[str, str], *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            *args,
        ],
        input=data,
        check=True,
        capture_output=True,
        env=env,
    ).stdout


def _build_repo(root: Path, specification: Repository) -> tuple[str, ...]:
    path = root / specification.name
    path.mkdir()
    env = _environment()
    _git(path, env, "init", "--quiet", "--template=", "--initial-branch=main", "--object-format=sha1")
    (path / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    history: list[str] = []
    for commit in specification.commits:
        for filename, contents in commit.files:
            destination = path / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents.encode("utf-8"))
        env.update(
            GIT_AUTHOR_NAME=commit.author,
            GIT_AUTHOR_EMAIL=commit.email,
            GIT_AUTHOR_DATE=commit.date,
            GIT_COMMITTER_NAME=commit.author,
            GIT_COMMITTER_EMAIL=commit.email,
            GIT_COMMITTER_DATE=commit.date,
        )
        _git(path, env, "add", "--all")
        names = _git(path, env, "ls-files", "-z")
        _git(path, env, "update-index", "--chmod=-x", "-z", "--stdin", data=names)
        tree = _git(path, env, "write-tree").decode("ascii").strip()
        parents = ("-p", history[-1]) if history else ()
        # Plumbing writes real commits without invoking local hooks, signing, or fixture code.
        sha = _git(path, env, "commit-tree", tree, *parents, data=(commit.message + "\n").encode()).decode().strip()
        _git(path, env, "update-ref", "refs/heads/main", sha)
        history.append(sha)
    return tuple(history)


def build_fixtures(output: Path) -> dict[str, tuple[str, ...]]:
    """Create new repositories only; never overwrite or delete an existing destination."""
    specifications = repositories()
    for specification in specifications:
        if (output / specification.name).exists():
            raise FileExistsError(f"{output / specification.name} already exists. Choose a fresh output directory.")
    output.mkdir(parents=True, exist_ok=True)
    return {specification.name: _build_repo(output, specification) for specification in specifications}


def build_monorepo(output: Path, packages: int = 20, files_per_package: int = 100) -> None:
    """Create a synthetic workspace for cold/warm scan benchmarks without executing generated code."""
    output.mkdir(parents=True, exist_ok=False)
    (output / "package.json").write_text('{"private":true,"workspaces":["packages/*"]}\n', encoding="utf-8")
    for number in range(packages):
        name = f"package-{number:02}"
        root = output / "packages" / name
        root.mkdir(parents=True)
        (root / "pyproject.toml").write_text(f'[project]\nname="{name}"\n', encoding="utf-8")
        (root / "README.md").write_text("# Usage\n\nUse `--port` (default 8000).\n", encoding="utf-8")
        (root / "cli.py").write_text(
            'import argparse\np=argparse.ArgumentParser()\np.add_argument("--port", default=8000)\n', encoding="utf-8"
        )
        for index in range(files_per_package - 1):
            (root / f"module_{index:03}.py").write_text(
                f'"""Generated benchmark source {index}."""\n'
                f"def calculate_{index}(value):\n    return value + {index}\n",
                encoding="utf-8",
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "repos")
    args = parser.parse_args(argv)
    try:
        histories = build_fixtures(args.output)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"Could not build fixtures: {exc}. Check Git is installed and choose a fresh writable directory.",
            file=sys.stderr,
        )
        return 1
    for name, history in histories.items():
        print(f"{name}: {history[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
