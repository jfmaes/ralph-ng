"""Validation helpers for ralph plans."""

from __future__ import annotations

from collections import Counter, deque
from pathlib import Path

from .plan import Plan, Task, load_plan


class PlanValidationError(ValueError):
    """Raised when semantic validation of a plan fails."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def _task_lookup(plan: Plan) -> dict[str, Task]:
    lookup: dict[str, Task] = {}
    for task in plan.tasks:
        lookup.setdefault(task.id, task)
    return lookup


def _collect_duplicate_id_errors(plan: Plan) -> list[str]:
    counts = Counter(task.id for task in plan.tasks)
    return [f"Duplicate task ID: {task_id}" for task_id, count in counts.items() if count > 1]


def _collect_dangling_dependency_errors(plan: Plan) -> list[str]:
    task_ids = {task.id for task in plan.tasks}
    errors: list[str] = []
    for task in plan.tasks:
        for dependency in task.depends_on:
            if dependency not in task_ids:
                errors.append(f"Task {task.id} depends on unknown task {dependency}")
    return errors


def _collect_verification_errors(plan: Plan) -> list[str]:
    errors: list[str] = []
    for task in plan.tasks:
        if not task.verification_commands:
            errors.append(f"Task {task.id} must define at least one verification command")
    return errors


def _collect_cycle_errors(plan: Plan) -> list[str]:
    task_ids = [task.id for task in plan.tasks]
    unique_task_ids = set(task_ids)
    task_lookup = _task_lookup(plan)

    in_degree = {task_id: 0 for task_id in unique_task_ids}
    adjacency = {task_id: [] for task_id in unique_task_ids}
    for task in plan.tasks:
        for dependency in task.depends_on:
            if dependency not in unique_task_ids:
                continue
            adjacency[dependency].append(task.id)
            in_degree[task.id] += 1

    queue = deque(task_id for task_id, degree in in_degree.items() if degree == 0)
    visited: list[str] = []
    while queue:
        current = queue.popleft()
        visited.append(current)
        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(visited) == len(unique_task_ids):
        return []

    remaining = sorted(task_id for task_id, degree in in_degree.items() if degree > 0)
    return [
        "Task dependency graph contains a cycle involving: "
        + ", ".join(f"{task_id} ({task_lookup[task_id].title})" for task_id in remaining)
    ]


def _collect_file_reference_errors(plan: Plan, base_dir: str | Path) -> list[str]:
    root = Path(base_dir).resolve()
    errors: list[str] = []

    for task in plan.tasks:
        for expected_file in task.expected_files:
            expected_path = Path(expected_file)
            resolved_path = (root / expected_path).resolve()
            if expected_path.is_absolute() or not resolved_path.is_relative_to(root):
                errors.append(
                    f"Task {task.id} references unsafe expected file path {expected_file}"
                )
    return errors


def validate_plan(plan: Plan, base_dir: str | Path = ".") -> None:
    """Run semantic validation against a loaded plan."""

    errors: list[str] = []
    errors.extend(_collect_duplicate_id_errors(plan))
    errors.extend(_collect_dangling_dependency_errors(plan))
    errors.extend(_collect_verification_errors(plan))
    errors.extend(_collect_cycle_errors(plan))
    errors.extend(_collect_file_reference_errors(plan, base_dir))

    if errors:
        raise PlanValidationError(errors)


def validate_plan_file(path: str | Path, base_dir: str | Path = ".") -> Plan:
    """Load and validate a plan file."""

    plan = load_plan(path)
    validate_plan(plan, base_dir=base_dir)
    return plan
