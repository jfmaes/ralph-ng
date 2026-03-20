"""Main orchestration loop for ralph-ng."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path

from .adapters.base import (
    EvaluatorAdapter,
    EvaluatorResult,
    ExecutorAdapter,
    ExecutorResult,
    InvocationStatus,
)
from .config import RalphConfig
from .events import EventLogger
from .fsm import TaskFSM, TaskState
from .git_manager import GitManager
from .plan import Plan, Task
from .prompt_builder import PromptBuilder
from .ratchet import RatchetManager
from .review_bundle import ReviewBundleBuilder
from .scheduler import Scheduler
from .state import StateStore
from .verification import VerificationRunner


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED_HUMAN = "blocked_human"
    BLOCKED_SYSTEM = "blocked_system"
    FAILED_EXHAUSTED = "failed_exhausted"


@dataclass(slots=True)
class RunPaths:
    run_root: Path
    overlays_dir: Path
    patches_dir: Path
    prompts_dir: Path
    reviews_dir: Path
    command_logs_dir: Path


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    reason: str


class OrchestratorError(RuntimeError):
    """Raised when orchestration cannot start safely."""


class Orchestrator:
    """Coordinate task execution, verification, review, and persistence."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        plan: Plan,
        plan_path: str | Path,
        config: RalphConfig,
        state_root: str | Path,
        store: StateStore,
        events: EventLogger,
        git_manager: GitManager,
        prompt_builder: PromptBuilder,
        executor: ExecutorAdapter,
        evaluator: EvaluatorAdapter,
        verification_runner: VerificationRunner,
        review_bundle_builder: ReviewBundleBuilder,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.plan = plan
        self.plan_path = Path(plan_path)
        self.config = config
        self.state_root = Path(state_root)
        self.store = store
        self.events = events
        self.git_manager = git_manager
        self.prompt_builder = prompt_builder
        self.executor = executor
        self.evaluator = evaluator
        self.verification_runner = verification_runner
        self.review_bundle_builder = review_bundle_builder
        self.fsm = TaskFSM(store)

    def start(self, *, run_id: str) -> RunOutcome:
        if not self.git_manager.is_clean():
            raise OrchestratorError("Cannot start with a dirty working tree.")

        if self.store.get_run(run_id) is not None:
            raise OrchestratorError(f"Run {run_id} already exists.")

        self._ensure_run_paths(run_id)
        self.store.create_run(
            self._stored_plan_path(),
            run_id=run_id,
            status=RunStatus.RUNNING,
            task_ids=[task.id for task in self.plan.tasks],
        )
        branch_name = self.git_manager.ensure_managed_branch(
            plan_name=self.plan_path.stem,
            run_id=run_id,
        )
        self.events.log(
            "run.started",
            data={
                "run_id": run_id,
                "branch": branch_name,
                "plan_path": self._stored_plan_path(),
            },
        )
        return self._run_loop(run_id)

    def resume(self, *, run_id: str) -> RunOutcome:
        if self.store.get_run(run_id) is None:
            raise OrchestratorError(f"Unknown run {run_id}.")
        self._ensure_run_paths(run_id)
        self.store.update_run_status(run_id, RunStatus.RUNNING)
        self.events.log("run.resumed", data={"run_id": run_id})
        return self._run_loop(run_id)

    def _run_loop(self, run_id: str) -> RunOutcome:
        scheduler = Scheduler(self.plan, self.store, run_id)
        ratchet_manager = RatchetManager(
            overlay_dir=self._ensure_run_paths(run_id).overlays_dir,
            cwd=self.repo_root,
            max_checks_per_task=self.config.ralph.max_ratchet_checks_per_task,
            validation_timeout_seconds=self.config.timeouts.verification_command_seconds,
        )

        while True:
            if scheduler.is_plan_complete():
                self.store.update_run_status(run_id, RunStatus.COMPLETED)
                self.events.log(
                    "run.stopped",
                    data={"run_id": run_id, "status": RunStatus.COMPLETED, "reason": "Plan complete"},
                )
                return RunOutcome(run_id=run_id, status=RunStatus.COMPLETED, reason="Plan complete")

            task, mode = self._next_task(run_id, scheduler)
            if task is None or mode is None:
                status = self._resolve_terminal_status(run_id)
                reason = "No further actionable tasks remain."
                self.store.update_run_status(run_id, status)
                self.events.log(
                    "run.stopped",
                    data={"run_id": run_id, "status": status, "reason": reason},
                )
                return RunOutcome(run_id=run_id, status=status, reason=reason)

            if mode == TaskState.PENDING or mode == TaskState.READY:
                if not self.git_manager.is_clean():
                    message = "Workspace drift detected before starting the next task."
                    record = self.store.get_task_state(run_id, task.id)
                    attempt_count = 0 if record is None else record.attempt_count
                    self._transition(
                        run_id,
                        task.id,
                        TaskState.BLOCKED_HUMAN,
                        attempt_count=attempt_count,
                        last_error=message,
                    )
                    self.store.update_run_status(run_id, RunStatus.BLOCKED_HUMAN)
                    self.events.log(
                        "run.stopped",
                        data={"run_id": run_id, "status": RunStatus.BLOCKED_HUMAN, "reason": message},
                    )
                    return RunOutcome(run_id=run_id, status=RunStatus.BLOCKED_HUMAN, reason=message)

            if mode in {TaskState.PENDING, TaskState.READY, TaskState.REPAIR}:
                execution_outcome = self._execute_task(run_id, task, mode, ratchet_manager)
                if execution_outcome is not None:
                    return execution_outcome
                review_outcome = self._review_task(run_id, task, ratchet_manager)
                if review_outcome is not None:
                    return review_outcome
            elif mode == TaskState.REVIEW:
                review_outcome = self._review_task(run_id, task, ratchet_manager)
                if review_outcome is not None:
                    return review_outcome

    def _execute_task(
        self,
        run_id: str,
        task: Task,
        mode: TaskState,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        current_record = self.store.get_task_state(run_id, task.id)
        previous_attempt_count = 0 if current_record is None else current_record.attempt_count
        if mode == TaskState.PENDING:
            self._transition(run_id, task.id, TaskState.READY, attempt_count=previous_attempt_count)

        attempt_number = previous_attempt_count + 1
        if mode == TaskState.REPAIR:
            repair_notes = current_record.last_error if current_record is not None else None
            self._transition(
                run_id,
                task.id,
                TaskState.IN_PROGRESS,
                attempt_count=attempt_number,
                last_error=repair_notes,
            )
        else:
            self._transition(run_id, task.id, TaskState.IN_PROGRESS, attempt_count=attempt_number)
            repair_notes = None

        run_paths = self._ensure_run_paths(run_id)
        overlay_commands = ratchet_manager.load_overlay(task.id).verification_commands
        prompt = self.prompt_builder.build(
            task,
            repair_notes=repair_notes,
            ratchet_overlays=overlay_commands,
        )
        prompt_path = run_paths.prompts_dir / f"{task.id}_attempt_{attempt_number}.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        self.events.log(
            "executor.started",
            task_id=task.id,
            data={"run_id": run_id, "attempt": attempt_number},
        )
        result: ExecutorResult = self.executor.execute(
            prompt,
            timeout_seconds=self.config.timeouts.executor_call_seconds,
            cwd=self.repo_root,
        )
        self.store.record_invocation(
            run_id,
            task.id,
            role="executor",
            model=self.config.models.executor,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_seconds=result.duration_seconds,
            exit_status=result.exit_status,
        )
        self.events.log(
            "executor.completed",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "status": result.status,
                "error": result.error_message,
            },
        )

        patch_path = self.git_manager.snapshot_patch(task.id, attempt_number, run_paths.patches_dir)
        attempt = self.store.record_attempt(
            run_id,
            task.id,
            attempt_number,
            patch_path=str(patch_path),
            prompt_path=str(prompt_path),
        )

        if result.status != InvocationStatus.COMPLETED:
            return self._handle_blocked_system(
                run_id,
                task,
                attempt_number,
                result.error_message or f"Executor status: {result.status}",
            )

        self._transition(run_id, task.id, TaskState.REVIEW, attempt_count=attempt_number)
        return None

    def _review_task(
        self,
        run_id: str,
        task: Task,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        record = self.store.get_task_state(run_id, task.id)
        if record is None:
            raise OrchestratorError(f"Missing task state for {task.id}")
        attempt_number = record.attempt_count
        run_paths = self._ensure_run_paths(run_id)
        latest_attempt = self.store.get_latest_attempt(run_id, task.id)
        if latest_attempt is None:
            raise OrchestratorError(f"No attempt recorded for review task {task.id}")

        effective_commands = ratchet_manager.effective_commands(task.id, task.verification_commands)
        verification_results = self.verification_runner.run_commands(effective_commands)
        self._write_command_logs(run_paths.command_logs_dir, task.id, attempt_number, verification_results)
        for result in verification_results:
            self.events.log(
                "verification.result",
                task_id=task.id,
                data={
                    "run_id": run_id,
                    "attempt": attempt_number,
                    "command": result.command,
                    "success": result.success,
                    "timed_out": result.timed_out,
                    "exit_code": result.exit_code,
                },
            )

        git_diff = ""
        if latest_attempt.patch_path is not None:
            git_diff = Path(latest_attempt.patch_path).read_text(encoding="utf-8")
        bundle = self.review_bundle_builder.build(
            task,
            git_diff=git_diff,
            verification_results=verification_results,
            repair_notes=record.last_error,
            ratchet_overlay_history=ratchet_manager.load_overlay(task.id).verification_commands,
        )
        result: EvaluatorResult = self.evaluator.evaluate(
            bundle,
            timeout_seconds=self.config.timeouts.evaluator_call_seconds,
            cwd=self.repo_root,
        )
        self.store.record_invocation(
            run_id,
            task.id,
            role="evaluator",
            model=self.config.models.evaluator,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_seconds=result.duration_seconds,
            exit_status=result.exit_status,
        )

        review_path = run_paths.reviews_dir / f"{task.id}_attempt_{attempt_number}.json"
        review_path.write_text(
            json.dumps(
                {
                    "status": result.status,
                    "output_text": result.output_text,
                    "error_message": result.error_message,
                    "verdict": None if result.verdict is None else result.verdict.model_dump(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.store.update_attempt_artifacts(latest_attempt.id, review_path=str(review_path))

        if result.status != InvocationStatus.COMPLETED or result.verdict is None:
            return self._handle_blocked_system(
                run_id,
                task,
                attempt_number,
                result.error_message or f"Evaluator status: {result.status}",
            )

        self.events.log(
            "evaluator.verdict",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "decision": result.verdict.decision,
                "summary": result.verdict.summary,
            },
        )

        all_verification_passed = all(item.success for item in verification_results)
        if result.verdict.decision == "accept" and all_verification_passed:
            commit_sha = self.git_manager.commit_task(task.id, task.title)
            self._transition(run_id, task.id, TaskState.DONE, attempt_count=attempt_number, last_error=None)
            self.events.log(
                "task.accepted",
                task_id=task.id,
                data={"run_id": run_id, "commit": commit_sha},
            )
            if self.config.ralph.auto_push:
                self.git_manager.push_if_enabled(auto_push=True)
            return None

        repair_notes = result.verdict.repair_notes
        if result.verdict.decision == "accept" and not all_verification_passed:
            failed_commands = [item.command for item in verification_results if not item.success]
            repair_notes = "Verification failed for: " + ", ".join(failed_commands)
            new_commands: list[str] = []
        else:
            new_commands = result.verdict.new_verification_commands

        ratchet_result = ratchet_manager.add_checks(task.id, new_commands)
        for command in ratchet_result.accepted:
            self.events.log(
                "ratchet.added",
                task_id=task.id,
                data={"run_id": run_id, "command": command},
            )

        if attempt_number >= self.config.ralph.max_retries_per_task:
            message = repair_notes or "Retry budget exhausted."
            self._transition(
                run_id,
                task.id,
                TaskState.FAILED_EXHAUSTED,
                attempt_count=attempt_number,
                last_error=message,
            )
            return None

        self._transition(
            run_id,
            task.id,
            TaskState.REPAIR,
            attempt_count=attempt_number,
            last_error=repair_notes or result.verdict.summary,
        )
        return None

    def _handle_blocked_system(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        message: str,
    ) -> RunOutcome:
        self._transition(
            run_id,
            task.id,
            TaskState.BLOCKED_SYSTEM,
            attempt_count=attempt_number,
            last_error=message,
        )
        self.store.update_run_status(run_id, RunStatus.BLOCKED_SYSTEM)
        self.events.log(
            "run.stopped",
            data={"run_id": run_id, "status": RunStatus.BLOCKED_SYSTEM, "reason": message},
        )
        return RunOutcome(run_id=run_id, status=RunStatus.BLOCKED_SYSTEM, reason=message)

    def _next_task(self, run_id: str, scheduler: Scheduler) -> tuple[Task | None, TaskState | None]:
        for task in self.plan.tasks:
            state = self.fsm.get_state(run_id, task.id)
            if state == TaskState.REVIEW:
                return task, state

        for task in self.plan.tasks:
            state = self.fsm.get_state(run_id, task.id)
            if state == TaskState.REPAIR:
                return task, state

        task = scheduler.next_ready_task()
        if task is None:
            return None, None
        return task, self.fsm.get_state(run_id, task.id)

    def _resolve_terminal_status(self, run_id: str) -> RunStatus:
        task_states = self.store.list_task_states(run_id)
        if any(record.state == TaskState.BLOCKED_SYSTEM for record in task_states.values()):
            return RunStatus.BLOCKED_SYSTEM
        if any(record.state == TaskState.BLOCKED_HUMAN for record in task_states.values()):
            return RunStatus.BLOCKED_HUMAN
        if any(record.state == TaskState.FAILED_EXHAUSTED for record in task_states.values()):
            return RunStatus.FAILED_EXHAUSTED
        return RunStatus.BLOCKED_HUMAN

    def _transition(
        self,
        run_id: str,
        task_id: str,
        new_state: TaskState,
        *,
        attempt_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        previous_state = self.fsm.get_state(run_id, task_id)
        record = self.fsm.transition(
            run_id,
            task_id,
            new_state,
            attempt_count=attempt_count,
            last_error=last_error,
        )
        self.events.log(
            "task.transition",
            task_id=task_id,
            data={
                "run_id": run_id,
                "from_state": previous_state,
                "to_state": new_state,
                "attempt_count": record.attempt_count,
                "last_error": record.last_error,
            },
        )

    def _ensure_run_paths(self, run_id: str) -> RunPaths:
        run_root = self.state_root / "runs" / run_id
        overlays_dir = run_root / "overlays"
        patches_dir = run_root / "patches"
        prompts_dir = run_root / "prompts"
        reviews_dir = run_root / "reviews"
        command_logs_dir = run_root / "command_logs"
        for path in [run_root, overlays_dir, patches_dir, prompts_dir, reviews_dir, command_logs_dir]:
            path.mkdir(parents=True, exist_ok=True)
        return RunPaths(
            run_root=run_root,
            overlays_dir=overlays_dir,
            patches_dir=patches_dir,
            prompts_dir=prompts_dir,
            reviews_dir=reviews_dir,
            command_logs_dir=command_logs_dir,
        )

    def _stored_plan_path(self) -> str:
        try:
            return str(self.plan_path.relative_to(self.repo_root))
        except ValueError:
            return str(self.plan_path)

    def _write_command_logs(
        self,
        command_logs_root: Path,
        task_id: str,
        attempt_number: int,
        results: list[VerificationResult],
    ) -> None:
        attempt_dir = command_logs_root / f"{task_id}_attempt_{attempt_number}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        for index, result in enumerate(results, start=1):
            base_name = attempt_dir / f"{index:02d}"
            (base_name.with_suffix(".stdout.log")).write_text(result.stdout, encoding="utf-8")
            (base_name.with_suffix(".stderr.log")).write_text(result.stderr, encoding="utf-8")
            (base_name.with_suffix(".json")).write_text(
                json.dumps(result.model_dump(), indent=2),
                encoding="utf-8",
            )
