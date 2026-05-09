"""Configuration loading for ralph-ng."""

from __future__ import annotations

from typing import Literal
from pathlib import Path
import tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigLoadError(ValueError):
    """Raised when ralph.toml cannot be loaded or validated."""


class RalphSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: str = "ralph/"
    auto_push: bool = False
    max_retries_per_task: int = Field(default=5, ge=1)
    max_ratchet_checks_per_task: int = Field(default=10, ge=1)
    auto_escalate: int | None = Field(default=None, ge=1)
    auto_escalate_model: str = "opus"

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("branch_prefix must not be empty")
        return value

    @field_validator("auto_escalate_model")
    @classmethod
    def validate_auto_escalate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("auto_escalate_model must not be empty")
        return value


class ModelsConfig(BaseModel):
    """Legacy model config — used as fallback when [roles] is absent."""

    model_config = ConfigDict(extra="forbid")

    executor: str = "opus"
    cli: str = "claude"

    @field_validator("executor", "cli")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model names must not be empty")
        return value


class TimeoutsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_call_seconds: int = Field(default=600, ge=1)
    verification_command_seconds: int = Field(default=120, ge=1)
    reviewer_call_seconds: int = Field(default=300, ge=1)


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_strategy: Literal["managed", "current"] = "managed"
    required_branch: str = "main"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    force_push: bool = False

    @field_validator("required_branch")
    @classmethod
    def validate_required_branch(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required_branch must not be empty")
        return value

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


class RoleConfig(BaseModel):
    """Per-role (executor/reviewer) adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "claude"
    transport: str = "cli"  # "cli" now, "api" in v0.3
    model: str = "opus"
    cli_path: str = "claude"
    reasoning_effort: str | None = None
    service_tier: str | None = None
    # When true, the CLI is launched with plugin / slash-command surfaces
    # disabled so the role sees a vanilla provider. For claude this maps to
    # ``--disable-slash-commands``; for codex it maps to ``--disable plugins``
    # (codex skills loaded from ``~/.codex/skills`` are environmental and
    # remain the operator's responsibility — sandbox HOME if you need that).
    disable_plugins_and_slash_commands: bool = False
    # Reserved for v0.3 API transport
    base_url: str | None = None
    api_key_env: str | None = None

    @field_validator("provider", "transport", "model", "cli_path")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @field_validator("reasoning_effort", "service_tier")
    @classmethod
    def validate_optional_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @model_validator(mode="after")
    def apply_codex_defaults(self) -> RoleConfig:
        if self.provider == "codex":
            if self.reasoning_effort is None:
                self.reasoning_effort = "medium"
            if self.service_tier is None:
                self.service_tier = "fast"
        return self


class RolesConfig(BaseModel):
    """Role-based adapter selection (v0.2+)."""

    model_config = ConfigDict(extra="forbid")

    executor: RoleConfig = Field(default_factory=lambda: RoleConfig(
        provider="claude", model="opus", cli_path="claude",
    ))
    reviewer: RoleConfig = Field(default_factory=lambda: RoleConfig(
        provider="codex",
        model="gpt-5.3-codex",
        cli_path="codex",
        reasoning_effort="medium",
        service_tier="fast",
    ))


class ReviewThresholdsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_accept_min_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    challenge_below_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    auto_reject_min_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    challenge_reject_below: float = Field(default=0.70, ge=0.0, le=1.0)
    escalate_below_confidence: float = Field(default=0.60, ge=0.0, le=1.0)


class ReviewChallengeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds_low: int = Field(default=1, ge=1)
    max_rounds_medium: int = Field(default=2, ge=1)
    max_rounds_high: int = Field(default=2, ge=1)
    max_rounds_critical: int = Field(default=3, ge=1)


class ReviewEscalationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_reviewer_disagreement: bool = True
    on_all_rounds_below_confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    on_critical_below_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    on_security_path_below_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    max_rejection_loops: int = Field(default=3, ge=1)


class ReviewRiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_paths: list[str] = Field(default_factory=list)
    high_risk_paths: list[str] = Field(default_factory=list)


class ReviewConfig(BaseModel):
    """Adversarial review configuration (v0.2+)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    stages: list[str] = Field(default_factory=lambda: ["spec_compliance", "code_quality"])
    thresholds: ReviewThresholdsConfig = Field(default_factory=ReviewThresholdsConfig)
    challenge: ReviewChallengeConfig = Field(default_factory=ReviewChallengeConfig)
    escalation: ReviewEscalationConfig = Field(default_factory=ReviewEscalationConfig)
    risk: ReviewRiskConfig = Field(default_factory=ReviewRiskConfig)


class SelfHealingConfig(BaseModel):
    """Self-healing diagnostic session configuration (Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    diagnosis_provider: str = "claude"
    diagnosis_model: str = "opus"
    diagnosis_cli_path: str = "claude"
    # Mirrors RoleConfig.disable_plugins_and_slash_commands for the
    # diagnosis CLI. When true the diagnosis subprocess is launched with
    # the same plugin/slash-command suppression as the executor/reviewer.
    disable_plugins_and_slash_commands: bool = False
    max_diagnosis_per_task: int = Field(default=1, ge=0)
    max_auto_fix_actions_per_task: int = Field(default=2, ge=0)
    diagnosis_timeout_seconds: int = Field(default=120, ge=1)

    @field_validator("diagnosis_provider", "diagnosis_model", "diagnosis_cli_path")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class RalphConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ralph: RalphSection = Field(default_factory=RalphSection)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    roles: RolesConfig | None = None
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    self_healing: SelfHealingConfig = Field(default_factory=SelfHealingConfig)


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
