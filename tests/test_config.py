from pathlib import Path

import pytest

from ralph_ng.config import ConfigLoadError, load_config


def test_loads_valid_config(fixtures_dir: Path) -> None:
    config = load_config(fixtures_dir / "valid_ralph.toml")

    assert config.ralph.branch_prefix == "ralph/test/"
    assert config.ralph.auto_push is True
    assert config.ralph.auto_escalate == 4
    assert config.ralph.auto_escalate_model == "opus"
    assert config.models.executor == "haiku"
    assert config.models.cli == "claude"
    assert config.timeouts.executor_call_seconds == 900
    assert config.git.protected_branches == ["main", "release"]


def test_applies_defaults_for_omitted_sections(fixtures_dir: Path) -> None:
    config = load_config(fixtures_dir / "minimal_ralph.toml")

    assert config.ralph.branch_prefix == "ralph/"
    assert config.ralph.auto_push is False
    assert config.ralph.max_retries_per_task == 5
    assert config.ralph.auto_escalate is None
    assert config.ralph.auto_escalate_model == "opus"
    assert config.models.executor == "opus"
    assert config.timeouts.verification_command_seconds == 120
    assert config.git.branch_strategy == "managed"
    assert config.git.required_branch == "main"
    assert config.git.force_push is False


def test_loads_current_branch_strategy(tmp_path: Path) -> None:
    config_path = tmp_path / "ralph.toml"
    config_path.write_text(
        """
[git]
branch_strategy = "current"
required_branch = "release"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.git.branch_strategy == "current"
    assert config.git.required_branch == "release"


def test_missing_config_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.ralph.max_retries_per_task == 5
    assert config.ralph.auto_escalate is None
    assert config.ralph.auto_escalate_model == "opus"
    assert config.models.executor == "opus"
    assert config.git.branch_strategy == "managed"
    assert config.git.required_branch == "main"
    assert config.git.protected_branches == ["main", "master"]


def test_invalid_config_is_rejected(fixtures_dir: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(fixtures_dir / "invalid_ralph.toml")

    assert "Invalid config" in str(exc_info.value)


def test_invalid_branch_strategy_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "ralph.toml"
    config_path.write_text(
        """
[git]
branch_strategy = "detached"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(config_path)

    assert "branch_strategy" in str(exc_info.value)


def test_disable_plugins_and_slash_commands_defaults_false(tmp_path: Path) -> None:
    config_path = tmp_path / "ralph.toml"
    config_path.write_text(
        """
[roles.executor]
provider = "claude"
model = "opus"
cli_path = "claude"

[roles.reviewer]
provider = "codex"
model = "gpt-5.3-codex"
cli_path = "codex"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.roles is not None
    assert config.roles.executor.disable_plugins_and_slash_commands is False
    assert config.roles.reviewer.disable_plugins_and_slash_commands is False
    assert config.self_healing.disable_plugins_and_slash_commands is False


def test_disable_plugins_and_slash_commands_propagates(tmp_path: Path) -> None:
    config_path = tmp_path / "ralph.toml"
    config_path.write_text(
        """
[roles.executor]
provider = "claude"
model = "opus"
cli_path = "claude"
disable_plugins_and_slash_commands = true

[roles.reviewer]
provider = "codex"
model = "gpt-5.3-codex"
cli_path = "codex"
disable_plugins_and_slash_commands = true

[self_healing]
disable_plugins_and_slash_commands = true
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.roles is not None
    assert config.roles.executor.disable_plugins_and_slash_commands is True
    assert config.roles.reviewer.disable_plugins_and_slash_commands is True
    assert config.self_healing.disable_plugins_and_slash_commands is True
