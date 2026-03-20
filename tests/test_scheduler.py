from pathlib import Path

from ralph_ng.fsm import TaskFSM, TaskState
from ralph_ng.plan import Plan
from ralph_ng.scheduler import Scheduler
from ralph_ng.state import StateStore


def make_plan() -> Plan:
    return Plan.model_validate(
        {
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "First",
                    "description": "Start here",
                    "depends_on": [],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest tests/test_first.py -x"],
                    "expected_files": ["src/first.py"],
                },
                {
                    "id": "TASK-002",
                    "title": "Second",
                    "description": "Depends on first",
                    "depends_on": ["TASK-001"],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest tests/test_second.py -x"],
                    "expected_files": ["src/second.py"],
                },
                {
                    "id": "TASK-003",
                    "title": "Third",
                    "description": "Depends on first",
                    "depends_on": ["TASK-001"],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest tests/test_third.py -x"],
                    "expected_files": ["src/third.py"],
                },
            ]
        }
    )


def test_scheduler_selects_first_ready_task_in_plan_order(tmp_path: Path) -> None:
    plan = make_plan()
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    scheduler = Scheduler(plan, store, "run-001")
    fsm = TaskFSM(store)

    assert scheduler.next_ready_task() is not None
    assert scheduler.next_ready_task().id == "TASK-001"

    fsm.transition("run-001", "TASK-001", TaskState.READY)
    fsm.transition("run-001", "TASK-001", TaskState.IN_PROGRESS)
    fsm.transition("run-001", "TASK-001", TaskState.REVIEW)
    fsm.transition("run-001", "TASK-001", TaskState.DONE)

    assert scheduler.next_ready_task() is not None
    assert scheduler.next_ready_task().id == "TASK-002"


def test_scheduler_detects_plan_completion(tmp_path: Path) -> None:
    plan = make_plan()
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    scheduler = Scheduler(plan, store, "run-001")

    for task in plan.tasks:
        store.transition_task("run-001", task.id, "done")

    assert scheduler.next_ready_task() is None
    assert scheduler.is_plan_complete() is True
    assert scheduler.is_deadlocked() is False


def test_scheduler_detects_deadlock_when_no_tasks_are_ready(tmp_path: Path) -> None:
    plan = make_plan()
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    scheduler = Scheduler(plan, store, "run-001")

    store.transition_task("run-001", "TASK-001", "failed_exhausted")

    assert scheduler.next_ready_task() is None
    assert scheduler.is_plan_complete() is False
    assert scheduler.is_deadlocked() is True


def test_scheduler_is_not_deadlocked_while_task_is_active(tmp_path: Path) -> None:
    plan = make_plan()
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    scheduler = Scheduler(plan, store, "run-001")

    store.transition_task("run-001", "TASK-001", "in_progress")

    assert scheduler.is_deadlocked() is False
