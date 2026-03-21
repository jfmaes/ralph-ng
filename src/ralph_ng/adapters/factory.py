"""Adapter factory — builds executor and reviewer from config.

Uses role-based config ([roles]) when present, falls back to legacy
[models] config for the executor.
"""

from __future__ import annotations

from ..config import RalphConfig, RoleConfig
from .base import ExecutorAdapter, ReviewerAdapter
from .claude_executor import ClaudeExecutorAdapter
from .cli_claude_transport import CliClaudeTransport
from .cli_codex_transport import CliCodexTransport
from .codex_reviewer import CodexReviewerAdapter
from .streaming import LiveActivityTracker
from .transport import Transport


class FactoryError(ValueError):
    """Raised when adapter construction fails."""


def _build_transport(
    role_config: RoleConfig,
    *,
    tracker: LiveActivityTracker | None = None,
) -> Transport:
    """Build a Transport from role config."""
    if role_config.transport != "cli":
        raise FactoryError(
            f"Transport '{role_config.transport}' is not yet supported. "
            "Only 'cli' is available in v0.2."
        )

    if role_config.provider == "claude":
        return CliClaudeTransport(
            model=role_config.model,
            cli_path=role_config.cli_path,
            tracker=tracker,
        )
    elif role_config.provider == "codex":
        return CliCodexTransport(
            model=role_config.model,
            cli_path=role_config.cli_path,
            reasoning_effort=role_config.reasoning_effort,
            service_tier=role_config.service_tier,
            tracker=tracker,
        )
    else:
        raise FactoryError(
            f"Unknown provider '{role_config.provider}'. "
            "Supported: 'claude', 'codex'."
        )


def build_executor(
    config: RalphConfig,
    *,
    tracker: LiveActivityTracker | None = None,
) -> ExecutorAdapter:
    """Build an executor adapter from config.

    Uses [roles.executor] if present, otherwise falls back to [models].
    """
    if config.roles is not None:
        transport = _build_transport(config.roles.executor, tracker=tracker)
        return ClaudeExecutorAdapter(transport=transport)

    # Legacy fallback: build from [models] section
    transport = CliClaudeTransport(
        model=config.models.executor,
        cli_path=config.models.cli,
        tracker=tracker,
    )
    return ClaudeExecutorAdapter(transport=transport)


def build_reviewer(
    config: RalphConfig,
    *,
    tracker: LiveActivityTracker | None = None,
) -> ReviewerAdapter:
    """Build a reviewer adapter from config.

    Uses [roles.reviewer] if present. Raises if no role config provides
    a reviewer.
    """
    if config.roles is not None:
        transport = _build_transport(config.roles.reviewer, tracker=tracker)
        return CodexReviewerAdapter(transport=transport, tracker=tracker)

    raise FactoryError(
        "Reviewer requires [roles.reviewer] config. "
        "Add a [roles] section to ralph.toml to enable review."
    )
