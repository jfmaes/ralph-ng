"""Resume-state reconciliation for interrupted runs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .events import EventLogger
from .fsm import TaskState
from .git_manager import GitManager
from .plan import Plan, Task
from .state import StateStore, TaskStateRecord
from .utils import make_human_intervention_payload, parse_human_intervention_payload


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

    def __init__(
        self,
        *,
        store: StateStore,
        git_manager: GitManager,
        events: EventLogger | None = None,
    ) -> None:
        self.store = store
        self.git_manager = git_manager
        self.events = events

    def resume(self, run_id: str, plan: Plan) -> ResumeDecision:
        task_states = self.store.list_task_states(run_id)

        rollback_recovery = self._recover_from_head_rollback(run_id, plan, task_states)
        if rollback_recovery is not None:
            return rollback_recovery

        for task in plan.tasks:
            record = task_states.get(task.id)
            if record is None:
                continue
            if record.state in {TaskState.BLOCKED_SYSTEM, TaskState.BLOCKED_HUMAN}:
                return self._resume_blocked_task(run_id, task, record)

        active = [
            (task, record)
            for task in plan.tasks
            for record in [task_states.get(task.id)]
            if record is not None and record.state in {TaskState.IN_PROGRESS, TaskState.REVIEW, TaskState.REPAIR}
        ]
        if len(active) > 1:
            task_ids = ", ".join(task.id for task, _ in active)
            return ResumeDecision(
                action=ResumeAction.BLOCKED_HUMAN,
                reason=(
                    "Multiple active tasks remain in the run state. Human cleanup is required "
                    f"before resume can continue safely: {task_ids}."
                ),
            )
        if len(active) == 1:
            task, record = active[0]
            return self._resume_active_task(run_id, task, record)

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

    def _resume_active_task(
        self,
        run_id: str,
        task: Task,
        record: TaskStateRecord,
    ) -> ResumeDecision:
        state = TaskState(record.state)

        if state is TaskState.REVIEW:
            self.store.transition_task(
                run_id,
                task.id,
                TaskState.REVIEW,
                attempt_count=record.attempt_count,
                last_error=record.last_error,
            )
            return ResumeDecision(
                action=ResumeAction.CONTINUE,
                task_id=task.id,
                resume_state=TaskState.REVIEW,
                reason="Resuming reviewer work for the interrupted task.",
            )

        if state is TaskState.REPAIR:
            self.store.transition_task(
                run_id,
                task.id,
                TaskState.REPAIR,
                attempt_count=record.attempt_count,
                last_error=record.last_error,
            )
            return ResumeDecision(
                action=ResumeAction.CONTINUE,
                task_id=task.id,
                resume_state=TaskState.REPAIR,
                reason="Resuming repair flow for the interrupted task.",
            )

        if state is TaskState.IN_PROGRESS:
            if not self.git_manager.is_clean():
                # Dirty workspace from interrupted executor — reset to last commit
                # and let the executor re-implement the task from scratch.  The LLM
                # can handle partial code, but a clean slate is safer and avoids
                # confusion about which changes were intentional.
                self.git_manager.reset_to_head_clean()

            restarted_attempt_count = max(record.attempt_count - 1, 0)
            self.store.transition_task(
                run_id,
                task.id,
                TaskState.READY,
                attempt_count=restarted_attempt_count,
                last_error=None,
            )
            reason = (
                "Reset interrupted executor changes and restarting the task."
                if not self.git_manager.is_clean()
                else "Restarting the interrupted executor attempt from a clean workspace."
            )
            return ResumeDecision(
                action=ResumeAction.CONTINUE,
                task_id=task.id,
                resume_state=TaskState.READY,
                reason=reason,
            )

        return ResumeDecision(
            action=ResumeAction.BLOCKED_HUMAN,
            task_id=task.id,
            reason=f"Cannot safely resume task {task.id} from state {record.state}.",
        )

    def _recover_from_head_rollback(
        self,
        run_id: str,
        plan: Plan,
        task_states: dict[str, TaskStateRecord],
    ) -> ResumeDecision | None:
        if self.events is None:
            return None
        if not self.git_manager.is_clean():
            return None

        current_head = self.git_manager.current_commit()
        accepted_events = [
            event
            for event in self.events.read_events()
            if event.event_type == "task.accepted" and event.data.get("run_id") == run_id
        ]
        if not accepted_events:
            return None

        accepted_by_task = {
            event.task_id: str(event.data.get("commit", "")).strip()
            for event in accepted_events
            if event.task_id is not None and str(event.data.get("commit", "")).strip()
        }

        completed_ids: list[str] = []
        matched = False
        for task in plan.tasks:
            commit = accepted_by_task.get(task.id)
            if commit is None:
                break
            completed_ids.append(task.id)
            if commit == current_head:
                matched = True
                break

        if not matched:
            return None

        next_ready_task_id: str | None = None
        completed_set = set(completed_ids)
        for task in plan.tasks:
            if task.id in completed_set:
                continue
            if all(dep in completed_set for dep in task.depends_on):
                next_ready_task_id = task.id
                break

        for task in plan.tasks:
            existing = task_states.get(task.id)
            if task.id in completed_set:
                self.store.transition_task(
                    run_id,
                    task.id,
                    TaskState.DONE,
                    attempt_count=0 if existing is None else max(existing.attempt_count, 1),
                    last_error=None,
                )
            elif task.id == next_ready_task_id:
                self.store.transition_task(
                    run_id,
                    task.id,
                    TaskState.READY,
                    attempt_count=0,
                    last_error=None,
                )
            else:
                self.store.transition_task(
                    run_id,
                    task.id,
                    TaskState.PENDING,
                    attempt_count=0,
                    last_error=None,
                )

        if next_ready_task_id is None:
            return ResumeDecision(
                action=ResumeAction.COMPLETE,
                reason="Branch matches the last accepted task commit and no resumable tasks remain.",
            )

        return ResumeDecision(
            action=ResumeAction.CONTINUE,
            task_id=next_ready_task_id,
            resume_state=TaskState.READY,
            reason=(
                "Detected that the branch was rolled back to an earlier accepted task commit. "
                f"Reconstructed run state and will continue from {next_ready_task_id}."
            ),
        )

    def _resume_blocked_task(
        self,
        run_id: str,
        task: Task,
        record: TaskStateRecord,
    ) -> ResumeDecision:
        payload = (
            None if record.last_error is None else parse_human_intervention_payload(record.last_error)
        )
        if payload is not None and payload.get("metadata", {}).get("reason") == "interrupted_executor_dirty_workspace":
            self.git_manager.reset_to_head_clean()
            restarted_attempt_count = max(record.attempt_count - 1, 0)
            self.store.transition_task(
                run_id,
                task.id,
                TaskState.READY,
                attempt_count=restarted_attempt_count,
                last_error=None,
            )
            return ResumeDecision(
                action=ResumeAction.CONTINUE,
                task_id=task.id,
                resume_state=TaskState.READY,
                reason="Discarded interrupted executor changes and restarted the task from HEAD.",
            )

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

            message = make_human_intervention_payload(
                summary=f"Workspace diverged while resuming {task.id}.",
                what_failed=(
                    "Current workspace changes do not match the last patch snapshot for this run, "
                    "so Ralph cannot safely continue automatically."
                ),
                expected_human_actions=[
                    "Inspect the working tree diff and reconcile it with the last accepted patch.",
                    "Keep the intended task changes and remove or stash unrelated drift.",
                    f"Run `ralph resume --run-id {run_id}` once the workspace matches the intended state.",
                ],
                metadata={"task_id": task.id, "reason": "resume_workspace_drift"},
            )
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
                reason="Workspace has diverged from the last patch snapshot and needs human attention.",
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
