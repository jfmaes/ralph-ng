from pathlib import Path

import pytest

from ralph_ng.plan import Plan, Task, load_plan
from ralph_ng.validation import PlanValidationError, validate_plan


def _task(expected_files: list[str]) -> Task:
    return Task(
        id="task-1",
        title="Create file",
        description="Create a new file.",
        acceptance_criteria=["File exists"],
        verification_commands=["test -f src/new.py"],
        expected_files=expected_files,
    )


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


def test_missing_relative_file_references_are_allowed(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "plan_bad_file_refs.yaml")

    validate_plan(plan, base_dir=fixtures_dir)


def test_new_expected_files_can_be_created_by_the_task(tmp_path: Path) -> None:
    plan = Plan(tasks=[_task(["src/new.py"])])

    validate_plan(plan, base_dir=tmp_path)


@pytest.mark.parametrize("expected_file", ["/tmp/outside.txt", "../outside.txt"])
def test_expected_files_must_stay_inside_base_dir(
    tmp_path: Path, expected_file: str
) -> None:
    outside_file = tmp_path.parent / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    absolute_expected_file = str(outside_file) if expected_file.startswith("/") else expected_file
    plan = Plan(tasks=[_task([absolute_expected_file])])

    with pytest.raises(PlanValidationError) as exc_info:
        validate_plan(plan, base_dir=tmp_path)

    assert "unsafe expected file path" in str(exc_info.value)
