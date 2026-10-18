"""Validate the fixed answer key; precision and recall are intentionally not implemented."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Claim(BaseModel):
    """A manually enumerated, located assertion in fixture documentation."""

    model_config = ConfigDict(extra="forbid")

    repo: Literal["py-cli", "fastapi-svc", "node-tool", "clean-repo"]
    doc_path: Annotated[str, Field(min_length=1)]
    line: Annotated[int, Field(ge=1, strict=True)]
    claim_kind: Literal[
        "cli_flag",
        "cli_subcommand",
        "environment_variable",
        "http_endpoint",
        "config_key",
        "function",
        "package_script",
        "shell_command",
        "default_value",
        "make_target",
    ]
    claim_text: Annotated[str, Field(min_length=1)]

    @field_validator("doc_path")
    @classmethod
    def relative_doc_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Use a repository-relative POSIX documentation path without '..'.")
        return value

    def identity(self) -> tuple[str, str, int, str, str]:
        return self.repo, self.doc_path, self.line, self.claim_kind, self.claim_text


class Finding(Claim):
    expected_verdict: Literal["missing", "renamed", "default changed", "invalid command"]
    expected_new: str | int | None
    introduced_by: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

    @model_validator(mode="after")
    def replacement_matches_verdict(self) -> Self:
        if self.expected_verdict == "renamed" and not isinstance(self.expected_new, str):
            raise ValueError("A rename must name its replacement.")
        if self.expected_verdict == "default changed" and self.expected_new is None:
            raise ValueError("A changed default must supply its new value.")
        if self.expected_verdict in {"missing", "invalid command"} and self.expected_new is not None:
            raise ValueError("Missing items and invalid commands must use null for expected_new.")
        return self


class NonFinding(Claim):
    reason: Annotated[str, Field(min_length=1)]


class AnswerKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    findings: Annotated[list[Finding], Field(min_length=1)]
    non_findings: Annotated[list[NonFinding], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_claims(self) -> Self:
        seen: set[tuple[str, str, int, str, str]] = set()
        for claim in (*self.findings, *self.non_findings):
            identity = claim.identity()
            if identity in seen:
                raise ValueError(f"Duplicate or conflicting answer for {identity}.")
            seen.add(identity)
        return self


def load_key(path: Path) -> AnswerKey:
    """Read JSON only; do not run a detector or inspect executable repository code."""
    return AnswerKey.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/answer_key.json"
    )
    args = parser.parse_args(argv)
    try:
        load_key(args.key)
    except (OSError, ValueError) as exc:
        print(
            f"Cannot validate the answer key: {exc}. "
            "Check its path and schema; never alter it to match detector output.",
            file=sys.stderr,
        )
        return 1
    print("Answer key is valid. Precision and recall are not implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
