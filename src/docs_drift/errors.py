"""Friendly CLI boundary, with local debug logs for unexpected failures."""

import sys
import traceback
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from typer import _click as click
from typer._click import Context
from typer.core import TyperCommand, TyperGroup
from typer.exceptions import Abort, Exit

from docs_drift.llm.privacy import mask_secrets


class DocsDriftError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


COMMON_VALUES = {"--repo", "--config", "--format", "--output", "--output-json", "--output-sarif"}
COMMON_FLAGS = {"--verbose", "-v", "--quiet", "-q", "--no-color", "--debug", "--show-authors"}


class Command(TyperCommand):
    def get_help(self, ctx: Context) -> str:
        path = " ".join(ctx.command_path.split()[1:])
        argument = " <id>" if self.name in {"explain", "ignore"} else " --yes" if self.name == "init" else ""
        self.epilog = self.epilog or f"Example: docs-drift {path}{argument} --repo ."
        return (
            super().get_help(ctx)
            + "\nCommon options: --repo PATH, --format FORMAT, --output PATH, -q/--quiet, -v/--verbose,"
            " --no-color, --debug.\n"
        )


class Group(TyperGroup):
    def get_help(self, ctx: Context) -> str:
        path = " ".join(ctx.command_path.split()[1:])
        leaf = "update" if path.endswith("baseline") else "clear" if path.endswith("cache") else "scan"
        self.epilog = self.epilog or f"Example: docs-drift {path} {leaf} --repo ."
        return super().get_help(ctx)

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if ctx.parent is not None:
            return super().parse_args(ctx, args)
        # Global options have identical meaning before and after any subcommand.
        common, rest = [], []
        i = 0
        while i < len(args):
            token = args[i]
            key = token.split("=", 1)[0]
            if token == "--":
                rest.extend(args[i:])
                break
            if key in COMMON_VALUES or key in COMMON_FLAGS:
                common.append(token)
                if key in COMMON_VALUES and "=" not in token and i + 1 < len(args):
                    i += 1
                    common.append(args[i])
            else:
                rest.append(token)
            i += 1
        ctx.meta["help_only"] = "--help" in rest
        return super().parse_args(ctx, common + rest)

    def invoke(self, ctx: Context) -> Any:
        try:
            destination = ctx.params.get("output")
            quiet = ctx.params.get("quiet", False)
            capture = ctx.parent is None and ctx._protected_args[:1] != ["lsp"] and bool(destination or quiet)
            buffer = StringIO()
            with redirect_stdout(buffer) if capture else nullcontext():
                value = super().invoke(ctx)
            # Scan renderers write destinations directly. Other commands share
            # this boundary so --output and --quiet have the same meaning.
            if capture and destination and buffer.getvalue():
                target = Path(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(buffer.getvalue(), encoding="utf-8", newline="\n")
            return value
        except Exit:
            if capture and buffer.getvalue():
                # A failed --quiet check must still explain its findings.
                sys.stdout.write(buffer.getvalue())
            raise
        except (click.ClickException, Abort):
            raise
        except Exception as exc:
            if ctx.parent is not None:
                raise
            debug = ctx.params.get("debug", False)
            root = Path(ctx.params.get("repo", ".")).resolve()
            if isinstance(exc, DocsDriftError):
                message = str(exc) + (f"\nHint: {exc.hint}" if exc.hint else "")
            elif isinstance(exc, (OSError, ValueError)):
                friendly = DocsDriftError(
                    str(exc), "Check the selected paths, file permissions and configuration, then retry."
                )
                message = f"{friendly}\nHint: {friendly.hint}"
            else:
                log = root / ".docs-drift" / "last-error.log"
                try:
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(mask_secrets(traceback.format_exc()), encoding="utf-8")
                    location = str(log)
                except OSError:
                    location = "unavailable (repository is not writable)"
                message = (
                    f"The scan could not finish ({type(exc).__name__}).\n"
                    f"Debug log file: last-error.log\nLocation: {location}\n"
                    "Retry with --debug; report this at https://github.com/ayushagarwalhere/docs-drift/issues/new"
                )
            if debug:
                traceback.print_exc(file=sys.stderr)
            raise click.ClickException(message) from None
