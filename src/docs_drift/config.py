"""Validated configuration loaded exclusively from TOML text."""

import tomllib
import warnings
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigWarning(UserWarning):
    """An unrecognized configuration key was ignored."""


class ConfigError(ValueError):
    """A selected configuration source could not be loaded."""


class ConfigModel(BaseModel):
    """Ignore unknown keys after making them visible to the user."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def warn_unknown_keys(cls, data: object) -> object:
        if isinstance(data, dict):
            for key in data:
                if key not in cls.model_fields:
                    warnings.warn(
                        f"Unknown configuration key '{cls.__name__}.{key}'; ignored. Check the spelling or remove it.",
                        ConfigWarning,
                        stacklevel=2,
                    )
        return data


class LLMConfig(ConfigModel):
    """Optional prose assessment settings."""

    enabled: bool = False
    model: str | None = None


class HistoryConfig(ConfigModel):
    """Limits for static git history inspection."""

    max_commits: int = Field(default=2000, ge=0)


class Config(ConfigModel):
    """Schema shared by .docs-drift.toml and [tool.docs-drift]."""

    docs: list[str] = Field(default_factory=lambda: ["README.md", "docs/**/*.md", "*.md"])
    # Changelogs describe the past on purpose, so old claims there are not drift.
    exclude: list[str] = Field(
        default_factory=lambda: [
            "node_modules/**",
            ".venv/**",
            "vendor/**",
            "CHANGELOG.md",
            "**/CHANGELOG.md",
        ]
    )
    languages: Literal["auto"] | list[str] = "auto"
    ignore: list[str] = Field(default_factory=list)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read configuration at {path}: {exc}. Fix the file or path.") from exc


def load_config(repo: Path | None = None, config_path: Path | None = None) -> Config:
    """Select one source by precedence; omitted fields use schema defaults.

    Explicit paths are relative to the process working directory. Implicit
    .docs-drift.toml and pyproject.toml paths are relative to the selected repo.
    A broken selected source is an error, never a reason to silently fall back.
    """
    root = Path.cwd() if repo is None else repo
    source: Path | None = None
    data: object = {}
    if config_path is not None:
        source = config_path
        data = _read_toml(source)
    elif (root / ".docs-drift.toml").exists():
        source = root / ".docs-drift.toml"
        data = _read_toml(source)
    elif (root / "pyproject.toml").exists():
        source = root / "pyproject.toml"
        project = _read_toml(source)
        tool = project.get("tool", {})
        if not isinstance(tool, dict):
            raise ConfigError(f"Invalid configuration in {source}: make [tool] a TOML table.")
        data = tool.get("docs-drift", {})
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {source}: {exc}. Correct the listed fields.") from exc
