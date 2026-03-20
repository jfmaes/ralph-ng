"""Task scheduling against validated plans and persisted state."""

from __future__ import annotations

from .fsm import TaskState
from .plan import Plan, Task
from .state import StateStore


ACTIVE_STATES = {TaskState.IN_PROGRESS, TaskState.REVIEW, TaskState.REPAIR}


class Scheduler:
    """Select the next executable task for a run."""

    def __init__(self, plan: Plan, store: StateStore, run_id: str) -> None:
        self.plan = plan
        self.store = store
        self.run_id = run_id

    def get_task_state(self, task_id: str) -> TaskState:
        record = self.store.get_task_state(self.run_id, task_id)
        if record is None:
            return TaskState.PENDING
        return TaskState(record.state)

    def next_ready_task(self) -> Task | None:
        for task in self.plan.tasks:
            if self.get_task_state(task.id) == TaskState.REPAIR:
                return task
        for task in self.plan.tasks:
            state = self.get_task_state(task.id)
            if state not in {TaskState.PENDING, TaskState.READY}:
                continue
            if all(self.get_task_state(dependency) == TaskState.DONE for dependency in task.depends_on):
                return task
        return None

    def is_plan_complete(self) -> bool:
        return all(self.get_task_state(task.id) == TaskState.DONE for task in self.plan.tasks)

    def is_deadlocked(self) -> bool:
        if self.is_plan_complete():
            return False
        if self.next_ready_task() is not None:
            return False
        return not any(self.get_task_state(task.id) in ACTIVE_STATES for task in self.plan.tasks)
