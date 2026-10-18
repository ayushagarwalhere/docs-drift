"""Configuration source selection, validation, and unknown-key warnings."""

from pathlib import Path

import pytest

from docs_drift.config import Config, ConfigError, ConfigWarning, load_config


def test_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.docs == ["README.md", "docs/**/*.md", "*.md"]
    assert config.exclude == [
        "node_modules/**",
        ".venv/**",
        "vendor/**",
        "CHANGELOG.md",
        "**/CHANGELOG.md",
    ]
    assert config.languages == "auto"
    assert config.ignore == []
    assert config.min_confidence == 0.6
    assert config.llm.enabled is False
    assert config.llm.model is None
    assert config.history.max_commits == 2000


def test_explicit_config_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "chosen.toml"
    explicit.write_text('docs = ["chosen.md"]\n', encoding="utf-8")
    (tmp_path / ".docs-drift.toml").write_text('docs = ["local.md"]\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.docs-drift]\ndocs = ["project.md"]\nmin_confidence = 0.9\n', encoding="utf-8"
    )
    config = load_config(tmp_path, explicit)
    assert config.docs == ["chosen.md"]
    assert config.min_confidence == 0.6  # Sources are selected, not merged.


def test_local_config_wins_over_pyproject(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text('docs = ["local.md"]\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("invalid TOML", encoding="utf-8")
    assert load_config(tmp_path).docs == ["local.md"]


def test_pyproject_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n[tool.unrelated]\noption = 1\n'
        '[tool.docs-drift]\ndocs = ["guide.md"]\nlanguages = ["python", "typescript"]\n'
        'ignore = ["legacy.*"]\nmin_confidence = 0.75\n'
        '[tool.docs-drift.llm]\nenabled = true\nmodel = "example-model"\n'
        "[tool.docs-drift.history]\nmax_commits = 50\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.docs == ["guide.md"]
    assert config.languages == ["python", "typescript"]
    assert config.ignore == ["legacy.*"]
    assert config.min_confidence == 0.75
    assert config.llm.enabled is True
    assert config.llm.model == "example-model"
    assert config.history.max_commits == 50


def test_pyproject_without_section_uses_defaults(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\n', encoding="utf-8")
    assert load_config(tmp_path) == Config()


def test_empty_local_config_shadows_pyproject(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.docs-drift]\nmin_confidence = 0.9\n", encoding="utf-8")
    assert load_config(tmp_path).min_confidence == 0.6


def test_unknown_keys_warn_at_each_level(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text(
        "typo = 1\n[llm]\nunknown = true\n[history]\noops = 1\n", encoding="utf-8"
    )
    with pytest.warns(ConfigWarning) as captured:
        config = load_config(tmp_path)
    messages = [str(item.message) for item in captured]
    assert any("Config.typo" in message for message in messages)
    assert any("LLMConfig.unknown" in message for message in messages)
    assert any("HistoryConfig.oops" in message for message in messages)
    assert config == Config()


@pytest.mark.parametrize("text", ["invalid TOML", "min_confidence = 2", "[history]\nmax_commits = -1"])
def test_bad_selected_config_does_not_fall_back(tmp_path: Path, text: str) -> None:
    config_path = tmp_path / ".docs-drift.toml"
    config_path.write_text(text, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.docs-drift]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"Fix the file or path|Correct the listed fields"):
        load_config(tmp_path)


def test_missing_explicit_config_does_not_fall_back(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="Fix the file or path"):
        load_config(tmp_path, tmp_path / "missing.toml")


def test_non_utf8_config_has_actionable_error(tmp_path: Path) -> None:
    config_path = tmp_path / ".docs-drift.toml"
    config_path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="Fix the file or path"):
        load_config(tmp_path)


def test_relative_explicit_path_uses_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "chosen.toml").write_text('docs = ["relative.md"]\n', encoding="utf-8")
    assert load_config(repo, Path("chosen.toml")).docs == ["relative.md"]


def test_default_lists_are_independent() -> None:
    first = Config()
    first.docs.append("extra.md")
    assert "extra.md" not in Config().docs
