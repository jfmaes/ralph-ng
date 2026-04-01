"""Tests for role-based config and review config."""

from __future__ import annotations

import pytest

from ralph_ng.config import (
    RalphConfig,
    ReviewChallengeConfig,
    ReviewConfig,
    ReviewEscalationConfig,
    ReviewThresholdsConfig,
    RoleConfig,
    RolesConfig,
    load_config,
)


class TestRoleConfig:
    def test_defaults(self) -> None:
        rc = RoleConfig()
        assert rc.provider == "claude"
        assert rc.transport == "cli"
        assert rc.model == "opus"
        assert rc.cli_path == "claude"
        assert rc.reasoning_effort is None
        assert rc.service_tier is None
        assert rc.base_url is None
        assert rc.api_key_env is None

    def test_codex_role(self) -> None:
        rc = RoleConfig(
            provider="codex",
            model="gpt-5.3-codex",
            cli_path="codex",
            reasoning_effort="high",
            service_tier="fast",
        )
        assert rc.provider == "codex"
        assert rc.reasoning_effort == "high"
        assert rc.service_tier == "fast"

    def test_codex_role_gets_sane_defaults(self) -> None:
        rc = RoleConfig(provider="codex", model="gpt-5.3-codex", cli_path="codex")
        assert rc.reasoning_effort == "medium"
        assert rc.service_tier == "fast"

    def test_empty_provider_rejected(self) -> None:
        with pytest.raises(Exception):
            RoleConfig(provider="  ")


class TestRolesConfig:
    def test_defaults(self) -> None:
        rc = RolesConfig()
        assert rc.executor.provider == "claude"
        assert rc.reviewer.provider == "codex"
        assert rc.reviewer.reasoning_effort == "medium"
        assert rc.reviewer.service_tier == "fast"


class TestReviewConfig:
    def test_defaults(self) -> None:
        rc = ReviewConfig()
        assert rc.enabled is True
        assert rc.stages == ["spec_compliance", "code_quality"]

    def test_thresholds_defaults(self) -> None:
        t = ReviewThresholdsConfig()
        assert t.auto_accept_min_confidence == 0.95
        assert t.challenge_below_confidence == 0.80
        assert t.auto_reject_min_confidence == 0.90
        assert t.escalate_below_confidence == 0.60

    def test_challenge_defaults(self) -> None:
        c = ReviewChallengeConfig()
        assert c.max_rounds_low == 1
        assert c.max_rounds_medium == 2
        assert c.max_rounds_high == 2
        assert c.max_rounds_critical == 3

    def test_escalation_defaults(self) -> None:
        e = ReviewEscalationConfig()
        assert e.on_reviewer_disagreement is True
        assert e.max_rejection_loops == 3

    def test_threshold_bounds(self) -> None:
        with pytest.raises(Exception):
            ReviewThresholdsConfig(auto_accept_min_confidence=1.5)
        with pytest.raises(Exception):
            ReviewThresholdsConfig(auto_accept_min_confidence=-0.1)


class TestRalphConfigBackwardCompat:
    def test_no_roles_section(self) -> None:
        config = RalphConfig()
        assert config.roles is None
        assert config.models.executor == "opus"

    def test_with_roles_section(self) -> None:
        config = RalphConfig(
            roles=RolesConfig(
                executor=RoleConfig(provider="claude", model="opus"),
                reviewer=RoleConfig(
                    provider="codex",
                    model="gpt-5.3-codex",
                    cli_path="codex",
                    reasoning_effort="high",
                    service_tier="fast",
                ),
            ),
        )
        assert config.roles is not None
        assert config.roles.executor.model == "opus"
        assert config.roles.reviewer.provider == "codex"
        assert config.roles.reviewer.reasoning_effort == "high"

    def test_review_section_defaults(self) -> None:
        config = RalphConfig()
        assert config.review.enabled is True


class TestConfigLoadWithRoles:
    def test_load_with_roles(self, tmp_path) -> None:
        config_file = tmp_path / "ralph.toml"
        config_file.write_text("""\
[ralph]
branch_prefix = "ralph/"

[models]
executor = "sonnet"
cli = "claude"

[roles.executor]
provider = "claude"
transport = "cli"
model = "opus"
cli_path = "claude"

[roles.reviewer]
provider = "codex"
transport = "cli"
model = "gpt-5.3-codex"
cli_path = "codex"

[review]
enabled = true
stages = ["spec_compliance", "code_quality"]

[review.thresholds]
auto_accept_min_confidence = 0.90

[review.challenge]
max_rounds_critical = 3

[review.escalation]
max_rejection_loops = 5
""")
        config = load_config(config_file)
        assert config.roles is not None
        assert config.roles.executor.model == "opus"
        assert config.roles.reviewer.provider == "codex"
        assert config.roles.reviewer.reasoning_effort == "medium"
        assert config.roles.reviewer.service_tier == "fast"
        assert config.review.thresholds.auto_accept_min_confidence == 0.90
        assert config.review.challenge.max_rounds_critical == 3
        assert config.review.escalation.max_rejection_loops == 5

    def test_load_without_roles(self, tmp_path) -> None:
        config_file = tmp_path / "ralph.toml"
        config_file.write_text("""\
[ralph]
branch_prefix = "ralph/"

[models]
executor = "sonnet"
cli = "claude"
""")
        config = load_config(config_file)
        assert config.roles is None
        assert config.models.executor == "sonnet"
