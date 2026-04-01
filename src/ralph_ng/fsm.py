"""Task lifecycle finite-state machine."""

from __future__ import annotations

from enum import StrEnum

from .state import StateStore, TaskStateRecord


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    REPAIR = "repair"
    DONE = "done"
    BLOCKED_HUMAN = "blocked_human"
    BLOCKED_SYSTEM = "blocked_system"
    FAILED_EXHAUSTED = "failed_exhausted"


class InvalidTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""


ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {
        TaskState.READY,
        TaskState.BLOCKED_HUMAN,
        TaskState.BLOCKED_SYSTEM,
    },
    TaskState.READY: {
        TaskState.IN_PROGRESS,
        TaskState.BLOCKED_HUMAN,
        TaskState.BLOCKED_SYSTEM,
    },
    TaskState.IN_PROGRESS: {
        TaskState.READY,
        TaskState.REVIEW,
        TaskState.BLOCKED_HUMAN,
        TaskState.BLOCKED_SYSTEM,
    },
    TaskState.REVIEW: {
        TaskState.READY,
        TaskState.DONE,
        TaskState.REPAIR,
        TaskState.FAILED_EXHAUSTED,
        TaskState.BLOCKED_HUMAN,
        TaskState.BLOCKED_SYSTEM,
    },
    TaskState.REPAIR: {
        TaskState.READY,
        TaskState.IN_PROGRESS,
        TaskState.REVIEW,
        TaskState.BLOCKED_HUMAN,
        TaskState.BLOCKED_SYSTEM,
    },
    TaskState.BLOCKED_HUMAN: {
        TaskState.PENDING,
        TaskState.READY,
        TaskState.IN_PROGRESS,
        TaskState.REVIEW,
        TaskState.REPAIR,
    },
    TaskState.BLOCKED_SYSTEM: {
        TaskState.PENDING,
        TaskState.READY,
        TaskState.IN_PROGRESS,
        TaskState.REVIEW,
        TaskState.REPAIR,
    },
    TaskState.DONE: set(),
    TaskState.FAILED_EXHAUSTED: {
        TaskState.PENDING,
    },
}


class TaskFSM:
    """Validate and persist task state transitions."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def get_state(self, run_id: str, task_id: str) -> TaskState:
        record = self.store.get_task_state(run_id, task_id)
        if record is None:
            return TaskState.PENDING
        return TaskState(record.state)

    def transition(
        self,
        run_id: str,
        task_id: str,
        new_state: TaskState,
        *,
        attempt_count: int | None = None,
        last_error: str | None = None,
    ) -> TaskStateRecord:
        current_state = self.get_state(run_id, task_id)
        if new_state != current_state and new_state not in ALLOWED_TRANSITIONS[current_state]:
            raise InvalidTransitionError(
                f"Invalid transition for {task_id}: {current_state} -> {new_state}"
            )
        return self.store.transition_task(
            run_id,
            task_id,
            str(new_state),
            attempt_count=attempt_count,
            last_error=last_error,
        )
