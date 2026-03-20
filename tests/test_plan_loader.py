from pathlib import Path

import pytest

from ralph_ng.plan import PlanLoadError, load_plan


def test_valid_plan_loads(fixtures_dir: Path) -> None:
    plan = load_plan(fixtures_dir / "valid_plan.yaml")

    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "TASK-001"
    assert plan.tasks[1].depends_on == ["TASK-001"]


def test_non_string_ids_are_rejected(fixtures_dir: Path) -> None:
    with pytest.raises(PlanLoadError) as exc_info:
        load_plan(fixtures_dir / "plan_with_numeric_ids.yaml")

    assert "Invalid plan" in str(exc_info.value)


def test_yaml_anchors_and_aliases_are_rejected(fixtures_dir: Path) -> None:
    with pytest.raises(PlanLoadError) as exc_info:
        load_plan(fixtures_dir / "plan_with_anchors.yaml")

    assert "anchors and aliases are not allowed" in str(exc_info.value)


def test_missing_required_fields_are_rejected(fixtures_dir: Path) -> None:
    with pytest.raises(PlanLoadError) as exc_info:
        load_plan(fixtures_dir / "plan_missing_fields.yaml")

    assert "Invalid plan" in str(exc_info.value)


def test_safe_load_rejects_python_object_tags(tmp_path: Path) -> None:
    malicious_plan = tmp_path / "malicious.yaml"
    malicious_plan.write_text(
        "tasks:\n"
        "  - id: \"TASK-001\"\n"
        "    title: !!python/object/apply:os.system [\"echo unsafe\"]\n"
        "    description: test\n"
        "    depends_on: []\n"
        "    acceptance_criteria: [\"done\"]\n"
        "    verification_commands: [\"pytest -q\"]\n"
        "    expected_files: [\"project/src/existing_module.py\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(PlanLoadError) as exc_info:
        load_plan(malicious_plan)

    assert "Invalid YAML" in str(exc_info.value)
