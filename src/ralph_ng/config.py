"""Configuration loading for ralph-ng."""

from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigLoadError(ValueError):
    """Raised when ralph.toml cannot be loaded or validated."""


class RalphSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: str = "ralph/"
    auto_push: bool = False
    max_retries_per_task: int = Field(default=3, ge=1)
    max_ratchet_checks_per_task: int = Field(default=10, ge=1)

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("branch_prefix must not be empty")
        return value


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor: str = "sonnet"
    evaluator: str = "opus"
    cli: str = "claude"

    @field_validator("executor", "evaluator", "cli")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model names must not be empty")
        return value


class TimeoutsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_call_seconds: int = Field(default=600, ge=1)
    verification_command_seconds: int = Field(default=120, ge=1)
    evaluator_call_seconds: int = Field(default=300, ge=1)


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    force_push: bool = False

    @field_validator("protected_branches")
    @classmethod
    def validate_protected_branches(cls, branches: list[str]) -> list[str]:
        if not branches:
            raise ValueError("protected_branches must not be empty")
        if any(not branch.strip() for branch in branches):
            raise ValueError("protected_branches entries must not be empty")
        return branches

    @field_validator("force_push")
    @classmethod
    def validate_force_push(cls, value: bool) -> bool:
        if value:
            raise ValueError("force_push must remain false in v0.1")
        return value


class RalphConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ralph: RalphSection = Field(default_factory=RalphSection)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    git: GitConfig = Field(default_factory=GitConfig)


def load_config(path: str | Path = "ralph.toml") -> RalphConfig:
    """Load a TOML config file, applying defaults for omitted sections."""

    config_path = Path(path)
    if not config_path.exists():
        return RalphConfig()

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigLoadError(f"Invalid TOML in {config_path}: {exc}") from exc

    try:
        return RalphConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigLoadError(f"Invalid config in {config_path}: {exc}") from exc
