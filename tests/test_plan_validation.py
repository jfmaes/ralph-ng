from pathlib import Path

import pytest

from ralph_ng.plan import load_plan
from ralph_ng.validation import PlanValidationError, validate_plan


def test_valid_plan_passes_validation(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "valid_plan.yaml")

    validate_plan(plan, base_dir=fixtures_dir)


def test_cycle_detection_rejects_plan(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_with_cycle.yaml")

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=fixtures_dir)

    assert "contains a cycle" in str(exc_info.value)


def test_duplicate_ids_reject_plan(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_with_duplicate_ids.yaml")

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=fixtures_dir)

    assert "Duplicate task ID" in str(exc_info.value)


def test_missing_dependencies_reject_plan(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_with_dangling_deps.yaml")

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=fixtures_dir)

    assert "depends on unknown task" in str(exc_info.value)


def test_tasks_without_verification_commands_are_rejected(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_no_verification.yaml")

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=fixtures_dir)

    assert "must define at least one verification command" in str(exc_info.value)


def test_missing_file_references_are_rejected(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_bad_file_refs.yaml")

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=fixtures_dir)

    assert "references missing file" in str(exc_info.value)
