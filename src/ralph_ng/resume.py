"""Resume-state reconciliation for interrupted runs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .fsm import TaskState
from .git_manager import GitManager
from .plan import Plan, Task
from .state import StateStore, TaskStateRecord


class ResumeAction(StrEnum):
    CONTINUE = "continue"
    BLOCKED_HUMAN = "blocked_human"
    COMPLETE = "complete"


class ResumeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ResumeAction
    task_id: str | None = None
    resume_state: str | None = None
    reason: str


class ResumeManager:
    """Determine whether a run can safely continue."""

    def __init__(self, *, store: StateStore, git_manager: GitManager) -> None:
        self.store = store
        self.git_manager = git_manager

    def resume(self, run_id: str, plan: Plan) -> ResumeDecision:
        task_states = self.store.list_task_states(run_id)

        for task in plan.tasks:
            record = task_states.get(task.id)
            if record is None:
                continue
            if record.state in {TaskState.BLOCKED_SYSTEM, TaskState.BLOCKED_HUMAN}:
                return self._resume_blocked_task(run_id, task, record)

        next_task = self._find_next_resumable_task(plan, task_states)
        if next_task is None:
            return ResumeDecision(
                action=ResumeAction.COMPLETE,
                reason="No resumable tasks remain for this run.",
            )

        existing = task_states.get(next_task.id)
        attempt_count = 0 if existing is None else existing.attempt_count
        self.store.transition_task(
            run_id,
            next_task.id,
            TaskState.READY,
            attempt_count=attempt_count,
            last_error=None,
        )
        return ResumeDecision(
            action=ResumeAction.CONTINUE,
            task_id=next_task.id,
            resume_state=TaskState.READY,
            reason="Workspace is clean and the next task is ready to continue.",
        )

    def _resume_blocked_task(
        self,
        run_id: str,
        task: Task,
        record: TaskStateRecord,
    ) -> ResumeDecision:
        latest_attempt = self.store.get_latest_attempt(run_id, task.id)
        patch_path = (
            None
            if latest_attempt is None or latest_attempt.patch_path is None
            else Path(latest_attempt.patch_path)
        )

        if not self.git_manager.is_clean():
            if patch_path is not None and not self.git_manager.detect_drift(patch_path):
                self.store.transition_task(
                    run_id,
                    task.id,
                    TaskState.REVIEW,
                    attempt_count=record.attempt_count,
                    last_error=None,
                )
                return ResumeDecision(
                    action=ResumeAction.CONTINUE,
                    task_id=task.id,
                    resume_state=TaskState.REVIEW,
                    reason="Workspace matches the last patch snapshot; resuming review.",
                )

            message = "Workspace has diverged from the last patch snapshot and needs human attention."
            self.store.transition_task(
                run_id,
                task.id,
                TaskState.BLOCKED_HUMAN,
                attempt_count=record.attempt_count,
                last_error=message,
            )
            return ResumeDecision(
                action=ResumeAction.BLOCKED_HUMAN,
                task_id=task.id,
                reason=message,
            )

        self.store.transition_task(
            run_id,
            task.id,
            TaskState.READY,
            attempt_count=record.attempt_count,
            last_error=None,
        )
        return ResumeDecision(
            action=ResumeAction.CONTINUE,
            task_id=task.id,
            resume_state=TaskState.READY,
            reason="Workspace is clean; task can continue from a fresh actionable state.",
        )

    def _find_next_resumable_task(
        self,
        plan: Plan,
        task_states: dict[str, TaskStateRecord],
    ) -> Task | None:
        completed = {
            task_id
            for task_id, record in task_states.items()
            if record.state == TaskState.DONE
        }

        for task in plan.tasks:
            record = task_states.get(task.id)
            state = TaskState.PENDING if record is None else TaskState(record.state)
            if state in {TaskState.DONE, TaskState.FAILED_EXHAUSTED}:
                continue
            if state in {TaskState.PENDING, TaskState.READY} and all(
                dependency in completed for dependency in task.depends_on
            ):
                return task
        return None
