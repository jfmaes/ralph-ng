from pathlib import Path

import yaml

from ralph_ng.adapters.factory import build_executor, build_reviewer
from ralph_ng.config import load_config
from ralph_ng.ratchet import RatchetOverlay
from ralph_ng.validation import validate_plan_file


def test_example_config_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    config = load_config(root / "ralph.toml.example")

    assert config.models.executor == "opus"
    assert config.git.protected_branches == ["main", "master"]


def test_example_config_builds_required_adapters() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    config = load_config(root / "ralph.toml.example")

    build_executor(config)
    build_reviewer(config)


def test_example_plan_validates() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    plan = validate_plan_file(root / "ralph-plan.example.yaml", base_dir=root)

    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "TASK-001"
    assert plan.tasks[1].depends_on == ["TASK-001"]


def test_example_ratchet_overlay_validates() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    overlay = RatchetOverlay.model_validate(
        yaml.safe_load((root / "ratchet-overlay.example.yaml").read_text(encoding="utf-8"))
    )

    assert overlay.task_id == "TASK-001"
    assert overlay.verification_commands == ["pytest tests/test_regression.py -x"]


def test_mixed_provider_config_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    config = load_config(root / "ralph.toml.mixed-provider.example")

    assert config.roles is not None
    assert config.roles.executor.provider == "claude"
    assert config.roles.reviewer.provider == "codex"
    assert config.roles.reviewer.reasoning_effort == "medium"
    assert config.roles.reviewer.service_tier == "fast"
    assert config.review.enabled is True
