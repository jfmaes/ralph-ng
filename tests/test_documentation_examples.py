from pathlib import Path

import yaml

from ralph_ng.adapters.base import EvaluatorVerdict
from ralph_ng.config import load_config
from ralph_ng.ratchet import RatchetOverlay
from ralph_ng.validation import validate_plan_file


def test_example_config_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    config = load_config(root / "ralph.toml.example")

    assert config.models.executor == "sonnet"
    assert config.git.protected_branches == ["main", "master"]


def test_example_plan_validates() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    plan = validate_plan_file(root / "ralph-plan.example.yaml", base_dir=root)

    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "TASK-001"
    assert plan.tasks[1].depends_on == ["TASK-001"]


def test_example_evaluator_verdicts_validate() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    accept = EvaluatorVerdict.model_validate_json(
        (root / "evaluator-verdict.accept.json").read_text(encoding="utf-8")
    )
    reject = EvaluatorVerdict.model_validate_json(
        (root / "evaluator-verdict.reject.json").read_text(encoding="utf-8")
    )

    assert accept.decision == "accept"
    assert reject.decision == "reject"
    assert reject.new_verification_commands == ["pytest tests/test_regression.py -x"]


def test_example_ratchet_overlay_validates() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "examples"

    overlay = RatchetOverlay.model_validate(
        yaml.safe_load((root / "ratchet-overlay.example.yaml").read_text(encoding="utf-8"))
    )

    assert overlay.task_id == "TASK-001"
    assert overlay.verification_commands == ["pytest tests/test_regression.py -x"]
