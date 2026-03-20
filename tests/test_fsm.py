from pathlib import Path

import pytest

from ralph_ng.fsm import InvalidTransitionError, TaskFSM, TaskState
from ralph_ng.state import StateStore


def test_valid_transitions_succeed_and_persist(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=["TASK-001"])
    fsm = TaskFSM(store)

    fsm.transition("run-001", "TASK-001", TaskState.READY)
    fsm.transition("run-001", "TASK-001", TaskState.IN_PROGRESS, attempt_count=1)
    fsm.transition("run-001", "TASK-001", TaskState.REVIEW)
    fsm.transition("run-001", "TASK-001", TaskState.REPAIR)
    fsm.transition("run-001", "TASK-001", TaskState.IN_PROGRESS, attempt_count=2)
    record = fsm.transition("run-001", "TASK-001", TaskState.REVIEW)

    assert record.state == TaskState.REVIEW
    assert record.attempt_count == 2


def test_invalid_transition_raises(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=["TASK-001"])
    fsm = TaskFSM(store)

    with pytest.raises(InvalidTransitionError):
        fsm.transition("run-001", "TASK-001", TaskState.REVIEW)


def test_terminal_states_cannot_transition(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=["TASK-001"])
    fsm = TaskFSM(store)
    fsm.transition("run-001", "TASK-001", TaskState.READY)
    fsm.transition("run-001", "TASK-001", TaskState.IN_PROGRESS)
    fsm.transition("run-001", "TASK-001", TaskState.REVIEW)
    fsm.transition("run-001", "TASK-001", TaskState.DONE)

    with pytest.raises(InvalidTransitionError):
        fsm.transition("run-001", "TASK-001", TaskState.REVIEW)


def test_blocked_states_can_resume(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=["TASK-001"])
    fsm = TaskFSM(store)
    fsm.transition("run-001", "TASK-001", TaskState.READY)
    fsm.transition("run-001", "TASK-001", TaskState.BLOCKED_SYSTEM, last_error="rate limit")

    record = fsm.transition("run-001", "TASK-001", TaskState.IN_PROGRESS)

    assert record.state == TaskState.IN_PROGRESS
