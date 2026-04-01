"""Tests for adapter factory."""

from __future__ import annotations

import pytest

from ralph_ng.adapters.cli_claude_transport import CliClaudeTransport
from ralph_ng.adapters.cli_codex_transport import CliCodexTransport
from ralph_ng.adapters.claude_executor import ClaudeExecutorAdapter
from ralph_ng.adapters.codex_reviewer import CodexReviewerAdapter
from ralph_ng.adapters.factory import FactoryError, build_executor, build_reviewer
from ralph_ng.adapters.structured_reviewer import StructuredReviewerAdapter
from ralph_ng.adapters.transport_executor import TransportExecutorAdapter
from ralph_ng.config import RalphConfig, RoleConfig, RolesConfig


def _config_with_roles(
    executor_provider: str = "claude",
    executor_model: str = "opus",
    reviewer_provider: str = "codex",
    reviewer_model: str = "gpt-5.3-codex",
    reviewer_reasoning_effort: str | None = None,
    reviewer_service_tier: str | None = None,
) -> RalphConfig:
    return RalphConfig(
        roles=RolesConfig(
            executor=RoleConfig(provider=executor_provider, model=executor_model, cli_path=executor_provider),
            reviewer=RoleConfig(
                provider=reviewer_provider,
                model=reviewer_model,
                cli_path=reviewer_provider,
                reasoning_effort=reviewer_reasoning_effort,
                service_tier=reviewer_service_tier,
            ),
        ),
    )


class TestBuildExecutor:
    def test_from_roles_config(self) -> None:
        config = _config_with_roles()
        executor = build_executor(config)
        assert isinstance(executor, TransportExecutorAdapter)
        assert isinstance(executor.transport, CliClaudeTransport)
        assert executor.transport.model == "opus"

    def test_supports_codex_as_executor(self) -> None:
        config = _config_with_roles(executor_provider="codex", executor_model="gpt-5.3-codex")
        executor = build_executor(config)
        assert isinstance(executor, TransportExecutorAdapter)
        assert isinstance(executor.transport, CliCodexTransport)
        assert executor.transport.model == "gpt-5.3-codex"

    def test_legacy_fallback(self) -> None:
        config = RalphConfig()  # no roles section
        executor = build_executor(config)
        assert isinstance(executor, ClaudeExecutorAdapter)
        assert isinstance(executor.transport, CliClaudeTransport)
        assert executor.transport.model == "opus"


class TestBuildReviewer:
    def test_from_roles_config(self) -> None:
        config = _config_with_roles(reviewer_reasoning_effort="high", reviewer_service_tier="fast")
        reviewer = build_reviewer(config)
        assert isinstance(reviewer, StructuredReviewerAdapter)
        assert isinstance(reviewer.transport, CliCodexTransport)
        assert reviewer.transport.model == "gpt-5.3-codex"
        assert reviewer.transport.reasoning_effort == "high"
        assert reviewer.transport.service_tier == "fast"

    def test_supports_claude_as_reviewer(self) -> None:
        config = _config_with_roles(reviewer_provider="claude", reviewer_model="sonnet")
        reviewer = build_reviewer(config)
        assert isinstance(reviewer, StructuredReviewerAdapter)
        assert isinstance(reviewer.transport, CliClaudeTransport)
        assert reviewer.transport.model == "sonnet"

    def test_requires_roles(self) -> None:
        config = RalphConfig()  # no roles
        with pytest.raises(FactoryError, match="roles.reviewer"):
            build_reviewer(config)


class TestTransportErrors:
    def test_unsupported_transport(self) -> None:
        config = RalphConfig(
            roles=RolesConfig(
                executor=RoleConfig(provider="claude", transport="api", model="sonnet"),
            ),
        )
        with pytest.raises(FactoryError, match="not yet supported"):
            build_executor(config)

    def test_unknown_provider(self) -> None:
        config = RalphConfig(
            roles=RolesConfig(
                executor=RoleConfig(provider="unknown", model="x", cli_path="x"),
            ),
        )
        with pytest.raises(FactoryError, match="Unknown provider"):
            build_executor(config)
