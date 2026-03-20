from pathlib import Path

import pytest

from ralph_ng.config import ConfigLoadError, load_config


def test_loads_valid_config(fixtures_dir: Path) -> None:
    config = load_config(fixtures_dir / "valid_ralph.toml")

    assert config.ralph.branch_prefix == "ralph/test/"
    assert config.ralph.auto_push is True
    assert config.models.executor == "haiku"
    assert config.models.evaluator == "opus"
    assert config.models.cli == "claude"
    assert config.timeouts.executor_call_seconds == 900
    assert config.git.protected_branches == ["main", "release"]


def test_applies_defaults_for_omitted_sections(fixtures_dir: Path) -> None:
    config = load_config(fixtures_dir / "minimal_ralph.toml")

    assert config.ralph.branch_prefix == "ralph/"
    assert config.ralph.auto_push is False
    assert config.models.executor == "sonnet"
    assert config.timeouts.verification_command_seconds == 120
    assert config.git.force_push is False


def test_missing_config_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.models.executor == "sonnet"
    assert config.git.protected_branches == ["main", "master"]


def test_invalid_config_is_rejected(fixtures_dir: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(fixtures_dir / "invalid_ralph.toml")

    assert "Invalid config" in str(exc_info.value)
