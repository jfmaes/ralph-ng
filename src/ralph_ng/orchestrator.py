"""Main orchestration loop for ralph-ng."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import subprocess
import threading
import time

from .adapters.base import (
    ExecutorAdapter,
    ExecutorResult,
    InvocationStatus,
    ReviewerAdapter,
)
from .adapters.streaming import ActivityEvent, ActivityKind, LiveActivityTracker
from .config import RalphConfig
from .events import EventLogger
from .failure import FailureClass, FailureDetail, classify_failure
from .fsm import TaskFSM, TaskState
from .git_manager import GitManager
from .plan import Plan, Task
from .prompt_builder import PromptBuilder
from .prompts.reviewer_first_pass import build_first_pass_prompt
from .prompts.challenge_executor import build_challenge_executor_prompt
from .prompts.challenge_reviewer import build_challenge_reviewer_prompt
from .ratchet import RatchetManager
from .review_bundle import ReviewBundleBuilder, collect_changed_file_snippets
from .review_findings import (
    FindingResponse,
    ReviewFindings,
    ReviewResult,
    parse_finding_responses,
)
from .providers.event_store import ProviderEventStore
from .providers.events import ProviderEventType, make_event
from .risk import RiskLevel, classify_diff_risk, count_diff_lines, extract_changed_files
from .scheduler import Scheduler
from .state import StateStore
from .utils import make_human_intervention_payload
from .verification import VerificationRunner


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED_HUMAN = "blocked_human"
    BLOCKED_SYSTEM = "blocked_system"
    FAILED_EXHAUSTED = "failed_exhausted"
    PAUSED = "paused"


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


def should_challenge(
    findings: ReviewFindings,
    diff_risk: str | RiskLevel,
    config: "ReviewConfig",
) -> bool:
    """Determine if a challenge round is needed after first-pass review."""
    has_critical_findings = any(f.severity == "critical" for f in findings.findings)
    has_security_findings = any(f.category == "security" for f in findings.findings)

    if findings.decision == "reject":
        if findings.confidence >= config.thresholds.auto_reject_min_confidence:
            return False  # high-confidence reject → go straight to repair
        return True  # low-confidence reject → challenge

    # Accept path
    if has_critical_findings:
        return True
    if has_security_findings:
        return True
    if diff_risk in ("high", "critical", RiskLevel.HIGH, RiskLevel.CRITICAL):
        return True
    if findings.confidence < config.thresholds.challenge_below_confidence:
        return True
    return False


def should_escalate(
    findings: ReviewFindings,
    diff_risk: str | RiskLevel,
    config: "ReviewConfig",
    *,
    rejection_count: int = 0,
) -> bool:
    """Determine if review should escalate to human intervention."""
    # Rejection loop exhaustion
    if findings.decision == "reject" and rejection_count >= config.escalation.max_rejection_loops:
        return True

    # All rounds below confidence threshold
    if findings.confidence < config.escalation.on_all_rounds_below_confidence:
        return True

    # Critical risk below confidence
    if diff_risk in ("critical", RiskLevel.CRITICAL):
        if findings.confidence < config.escalation.on_critical_below_confidence:
            return True

    # Security paths below confidence
    has_security_findings = any(f.category == "security" for f in findings.findings)
    if has_security_findings:
        if findings.confidence < config.escalation.on_security_path_below_confidence:
            return True

    # Persistent critical findings after at least one repair cycle
    has_critical_findings = any(f.severity == "critical" for f in findings.findings)
    if has_critical_findings and findings.decision == "reject" and rejection_count > 1:
        return True

    return False


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
        reviewer: ReviewerAdapter,
        verification_runner: VerificationRunner,
        review_bundle_builder: ReviewBundleBuilder,
        provider_event_store: ProviderEventStore | None = None,
        executor_activity_tracker: LiveActivityTracker | None = None,
        reviewer_activity_tracker: LiveActivityTracker | None = None,
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
        self.reviewer = reviewer
        self.verification_runner = verification_runner
        self.review_bundle_builder = review_bundle_builder
        self.provider_event_store = provider_event_store or ProviderEventStore(Path(state_root))
        self.executor_activity_tracker = executor_activity_tracker
        self.reviewer_activity_tracker = reviewer_activity_tracker
        self.fsm = TaskFSM(store)
        self._rejection_counts: dict[str, int] = {}  # task_id → consecutive rejection count
        self._current_task_id: str | None = None  # for session management
        self._shutdown_requested = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        # Propagate cancel event to transports for mid-stream interruption
        for adapter in (self.executor, self.reviewer):
            transport = getattr(adapter, "transport", None)
            if transport is not None and hasattr(transport, "cancel_event"):
                transport.cancel_event = self._shutdown_requested

    def start(self, *, run_id: str, task_limit: int | None = None) -> RunOutcome:
        import os as _os
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
        self.store.acquire_controller(run_id, _os.getpid())
        self._start_heartbeat(run_id)
        try:
            branch_name = self.git_manager.ensure_managed_branch(
                plan_name=self.plan_path.stem,
                run_id=run_id,
            )
            self.store.update_phase_checkpoint(run_id, "run:started")
            self.events.log(
                "run.started",
                data={
                    "run_id": run_id,
                    "branch": branch_name,
                    "plan_path": self._stored_plan_path(),
                },
            )
            return self._run_loop(run_id, task_limit=task_limit)
        finally:
            self._stop_heartbeat()
            stop_reason = None
            if self._shutdown_requested.is_set():
                stop_reason = json.dumps({"cause": "operator_interrupt"})
                # Clean up uncommitted changes from the interrupted run.
                # Review-ready work is committed; partial work is discarded.
                self._cleanup_on_pause(run_id)
            self.store.release_controller(run_id, stop_reason=stop_reason)

    def resume(
        self,
        *,
        run_id: str,
        task_limit: int | None = None,
        max_retries: int | None = None,
    ) -> RunOutcome:
        import os as _os
        if self.store.get_run(run_id) is None:
            raise OrchestratorError(f"Unknown run {run_id}.")
        self._ensure_run_paths(run_id)

        # Reset blocked/failed tasks so they can be retried.
        # With --max-retries: increase budget and continue from current attempt.
        # Without (default): discard changes and restart from scratch.
        self._reset_blocked_tasks(run_id, max_retries=max_retries)

        if max_retries is not None:
            self.config.ralph.max_retries_per_task = max_retries

        self.store.acquire_controller(run_id, _os.getpid())
        self._start_heartbeat(run_id)
        self.store.update_run_status(run_id, RunStatus.RUNNING)
        self.store.update_phase_checkpoint(run_id, "run:resumed")
        self.events.log("run.resumed", data={"run_id": run_id})
        try:
            return self._run_loop(run_id, task_limit=task_limit)
        finally:
            self._stop_heartbeat()
            stop_reason = None
            if self._shutdown_requested.is_set():
                stop_reason = json.dumps({"cause": "operator_interrupt"})
                self._cleanup_on_pause(run_id)
            self.store.release_controller(run_id, stop_reason=stop_reason)

    def _reset_blocked_tasks(
        self,
        run_id: str,
        *,
        max_retries: int | None = None,
    ) -> None:
        """Reset blocked/failed tasks so they can be retried.

        With max_retries set: keep current attempt count (continue where left off).
        Without (default): reset to pending with attempt_count=0 (restart from scratch).
        """
        task_states = self.store.list_task_states(run_id)
        for task_id, record in task_states.items():
            if record.state == TaskState.BLOCKED_HUMAN:
                # Blocked tasks always reset to pending — the human has
                # resolved the issue.
                self._rejection_counts.pop(task_id, None)
                self._transition(
                    run_id, task_id, TaskState.PENDING,
                    attempt_count=0, last_error=None,
                )
                self.events.log(
                    "task.transition",
                    task_id=task_id,
                    data={
                        "run_id": run_id,
                        "from_state": record.state,
                        "to_state": TaskState.PENDING,
                        "attempt_count": 0,
                        "reason": "reset_on_resume",
                    },
                )
            elif record.state == TaskState.FAILED_EXHAUSTED:
                self._rejection_counts.pop(task_id, None)
                if max_retries is not None:
                    # Continue from current state with a higher budget.
                    # Reset to REPAIR so the executor retries with existing code.
                    self._transition(
                        run_id, task_id, TaskState.REPAIR,
                        attempt_count=record.attempt_count,
                        last_error=record.last_error,
                    )
                    self.events.log(
                        "task.transition",
                        task_id=task_id,
                        data={
                            "run_id": run_id,
                            "from_state": record.state,
                            "to_state": TaskState.REPAIR,
                            "attempt_count": record.attempt_count,
                            "reason": "retry_budget_increased",
                            "max_retries": max_retries,
                        },
                    )
                else:
                    # Default: discard and restart from scratch.
                    self._transition(
                        run_id, task_id, TaskState.PENDING,
                        attempt_count=0, last_error=None,
                    )
                    self.events.log(
                        "task.transition",
                        task_id=task_id,
                        data={
                            "run_id": run_id,
                            "from_state": record.state,
                            "to_state": TaskState.PENDING,
                            "attempt_count": 0,
                            "reason": "reset_on_resume",
                        },
                    )

    def request_shutdown(self) -> None:
        """Signal the orchestrator to pause at the next safe checkpoint."""
        self._shutdown_requested.set()

    def _start_heartbeat(self, run_id: str, interval_seconds: float = 5.0) -> None:
        """Start a controller-owned heartbeat loop on a dedicated DB connection."""
        self._heartbeat_stop.clear()
        self.store.update_heartbeat(run_id)

        def _heartbeat_loop() -> None:
            heartbeat_store = StateStore(self.store.db_path)
            try:
                while not self._heartbeat_stop.wait(interval_seconds):
                    try:
                        heartbeat_store.update_heartbeat(run_id)
                    except Exception:
                        break
            finally:
                heartbeat_store.close()

        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"ralph-heartbeat-{run_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        """Stop the background heartbeat loop."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    def _cleanup_on_pause(self, run_id: str) -> None:
        """Discard uncommitted changes left by an interrupted run.

        Only committed (DONE) work survives a pause.  Everything else is
        rolled back so that resume starts the incomplete task from scratch.
        """
        try:
            if not self.git_manager.is_clean():
                self.git_manager.reset_to_head_clean()
        except Exception:
            pass  # best-effort; don't mask the real pause outcome

    def _check_shutdown(self, run_id: str) -> RunOutcome | None:
        """Return a paused outcome if shutdown was requested, else None."""
        if not self._shutdown_requested.is_set():
            # Also check file-based control channel
            control_path = self.state_root / f"{run_id}.control"
            if control_path.exists():
                try:
                    action = control_path.read_text(encoding="utf-8").strip().lower()
                    control_path.unlink(missing_ok=True)
                    if action in ("pause", "stop"):
                        self._shutdown_requested.set()
                except OSError:
                    pass
        if not self._shutdown_requested.is_set():
            return None
        self.store.update_run_status(run_id, RunStatus.PAUSED)
        self.events.log(
            "run.stopped",
            data={"run_id": run_id, "status": RunStatus.PAUSED, "reason": "Operator requested pause"},
        )
        return RunOutcome(run_id=run_id, status=RunStatus.PAUSED, reason="Operator requested pause")

    # ── Preflight checks ────────────────────────────────────────────

    _DEPENDENCY_CHECKS: list[tuple[str, str, str, str]] = [
        # (manifest_file, check_command, install_command, ecosystem_name)
        ("package.json", "npm ls --depth=0", "npm install", "Node.js"),
        ("yarn.lock", "yarn check --verify-tree", "yarn install", "Yarn"),
        ("pnpm-lock.yaml", "pnpm ls --depth=0", "pnpm install", "pnpm"),
        ("requirements.txt", "pip check", "pip install -r requirements.txt", "Python (pip)"),
        ("pyproject.toml", "pip check", "pip install -e '.[dev]'  # or: pip install -e .", "Python"),
        ("Cargo.toml", "cargo check --message-format=short 2>&1 | head -20", "cargo build", "Rust"),
        ("Gemfile", "bundle check", "bundle install", "Ruby"),
        ("go.mod", "go build ./... 2>&1 | head -20", "go mod download", "Go"),
        ("composer.json", "composer check-platform-reqs", "composer install", "PHP"),
    ]

    def _preflight_dependency_check(self, run_id: str) -> RunOutcome | None:
        """Check project dependencies are installed before starting tasks.

        Detects the project type from manifest files and runs a lightweight
        dependency check. If dependencies are missing, blocks with actionable
        guidance rather than letting the executor fail repeatedly.
        """
        failures: list[tuple[str, str, str, str]] = []  # (ecosystem, check_cmd, install_cmd, stderr)

        # Also check plan verification commands for basic runnability
        seen_checks: set[str] = set()

        for manifest, check_cmd, install_cmd, ecosystem in self._DEPENDENCY_CHECKS:
            if not (self.repo_root / manifest).exists():
                continue
            if check_cmd in seen_checks:
                continue
            seen_checks.add(check_cmd)
            try:
                result = subprocess.run(
                    ["bash", "-lc", check_cmd],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode != 0:
                    error_output = (result.stderr or result.stdout or "").strip()
                    # Truncate very long output
                    if len(error_output) > 500:
                        error_output = error_output[:500] + "\n... (truncated)"
                    failures.append((ecosystem, check_cmd, install_cmd, error_output))
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                # Don't block on check failures — the check tool itself might
                # not be installed, which is fine (e.g. cargo not installed for
                # a non-Rust project that happens to have a Cargo.toml).
                continue

        if not failures:
            self.events.log(
                "preflight.passed",
                data={"run_id": run_id, "checks_run": len(seen_checks)},
            )
            return None

        # Build actionable human intervention payload
        actions: list[str] = [
            "Project dependency check failed. Install missing dependencies before resuming:",
            "",
        ]
        for ecosystem, _check_cmd, install_cmd, error_output in failures:
            actions.append(f"  {ecosystem}: run `{install_cmd}`")
            if error_output:
                # Show first 3 lines of error for context
                error_lines = error_output.splitlines()[:3]
                for line in error_lines:
                    actions.append(f"    {line}")
        actions.append("")
        actions.append(f"Run `ralph resume --run-id {run_id}` once dependencies are installed.")

        first_task_id = self.plan.tasks[0].id if self.plan.tasks else "TASK-001"
        reason = make_human_intervention_payload(
            summary="Missing project dependencies detected during preflight check.",
            what_failed=(
                f"Preflight dependency check failed for "
                f"{', '.join(eco for eco, _, _, _ in failures)}. "
                f"The executor will not be able to build or test the project until "
                f"dependencies are installed."
            ),
            expected_human_actions=actions,
            metadata={
                "reason": "preflight_dependency_check",
                "ecosystems_failed": [eco for eco, _, _, _ in failures],
            },
        )

        record = self.store.get_task_state(run_id, first_task_id)
        attempt_count = 0 if record is None else record.attempt_count
        self._transition(
            run_id,
            first_task_id,
            TaskState.BLOCKED_HUMAN,
            attempt_count=attempt_count,
            last_error=reason,
        )
        self.store.update_run_status(run_id, RunStatus.BLOCKED_HUMAN)
        self.events.log(
            "preflight.failed",
            data={
                "run_id": run_id,
                "status": RunStatus.BLOCKED_HUMAN,
                "reason": "Missing dependencies",
                "ecosystems": [eco for eco, _, _, _ in failures],
            },
        )
        return RunOutcome(
            run_id=run_id,
            status=RunStatus.BLOCKED_HUMAN,
            reason="Missing project dependencies — install before resuming.",
        )

    def _run_loop(self, run_id: str, *, task_limit: int | None = None) -> RunOutcome:
        # Preflight: check project dependencies before starting any work
        preflight_outcome = self._preflight_dependency_check(run_id)
        if preflight_outcome is not None:
            return preflight_outcome

        scheduler = Scheduler(self.plan, self.store, run_id)
        ratchet_manager = RatchetManager(
            overlay_dir=self._ensure_run_paths(run_id).overlays_dir,
            cwd=self.repo_root,
            max_checks_per_task=self.config.ralph.max_ratchet_checks_per_task,
            validation_timeout_seconds=self.config.timeouts.verification_command_seconds,
        )
        completed_task_ids: set[str] = set()

        while True:
            # Check for shutdown/pause request at the top of each loop iteration
            pause_outcome = self._check_shutdown(run_id)
            if pause_outcome is not None:
                return pause_outcome

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
                    message = make_human_intervention_payload(
                        summary=f"Workspace drift detected before starting {task.id}.",
                        what_failed=(
                            "The working tree is not clean, so Ralph cannot safely separate "
                            "run-owned changes from unrelated edits."
                        ),
                        expected_human_actions=[
                            "Inspect the current git diff and identify unrelated changes.",
                            "Commit, stash, or discard those changes until the workspace is clean.",
                            f"Run `ralph resume --run-id {run_id}` once the workspace is clean.",
                        ],
                        metadata={"task_id": task.id, "reason": "workspace_drift"},
                    )
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
                        data={
                            "run_id": run_id,
                            "status": RunStatus.BLOCKED_HUMAN,
                            "reason": f"Workspace drift detected before starting {task.id}.",
                        },
                    )
                    return RunOutcome(
                        run_id=run_id,
                        status=RunStatus.BLOCKED_HUMAN,
                        reason=f"Workspace drift detected before starting {task.id}.",
                    )

            if mode in {TaskState.PENDING, TaskState.READY, TaskState.REPAIR}:
                execution_outcome = self._execute_task(run_id, task, mode, ratchet_manager)
                if execution_outcome is not None:
                    return execution_outcome
                # Check if self-healing reset the task to READY (skip review, re-enter loop)
                post_exec_state = self.fsm.get_state(run_id, task.id)
                if post_exec_state == TaskState.READY:
                    continue  # self-healing succeeded, retry from top of loop
                review_outcome = self._review_task(run_id, task, ratchet_manager)
                if review_outcome is not None:
                    return review_outcome
            elif mode == TaskState.REVIEW:
                review_outcome = self._review_task(run_id, task, ratchet_manager)
                if review_outcome is not None:
                    return review_outcome

            limit_outcome = self._maybe_stop_after_task_limit(
                run_id=run_id,
                task_id=task.id,
                scheduler=scheduler,
                completed_task_ids=completed_task_ids,
                task_limit=task_limit,
            )
            if limit_outcome is not None:
                return limit_outcome

    def _execute_task(
        self,
        run_id: str,
        task: Task,
        mode: TaskState,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        # Check for shutdown before starting expensive provider call
        pause_outcome = self._check_shutdown(run_id)
        if pause_outcome is not None:
            return pause_outcome
        if self.executor_activity_tracker is not None:
            self.executor_activity_tracker.clear()
        # Clear sessions when switching to a new task; preserve for same-task repairs
        if task.id != self._current_task_id:
            self._clear_transport_sessions()
            self._current_task_id = task.id
        current_record = self.store.get_task_state(run_id, task.id)
        previous_attempt_count = 0 if current_record is None else current_record.attempt_count
        if mode == TaskState.PENDING:
            self._transition(run_id, task.id, TaskState.READY, attempt_count=previous_attempt_count)

        attempt_number = previous_attempt_count + 1
        self.store.update_phase_checkpoint(run_id, f"execute:{task.id}:attempt:{attempt_number}")
        if mode == TaskState.REPAIR:
            raw_error = current_record.last_error if current_record is not None else None
            # Restore persisted rejection count from structured payload
            repair_notes = raw_error
            if raw_error:
                try:
                    payload = json.loads(raw_error)
                    if isinstance(payload, dict) and "repair_notes" in payload:
                        repair_notes = payload["repair_notes"]
                        persisted_count = payload.get("rejection_count", 0)
                        if persisted_count and task.id not in self._rejection_counts:
                            self._rejection_counts[task.id] = persisted_count
                except (json.JSONDecodeError, TypeError):
                    pass  # legacy plain-text repair notes
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

        executor_model = self._executor_model(attempt_number)
        if self._is_auto_escalated_executor_model(attempt_number):
            self.events.log(
                "executor.auto_escalated",
                task_id=task.id,
                data={
                    "run_id": run_id,
                    "attempt": attempt_number,
                    "from_model": self._base_executor_model(),
                    "to_model": executor_model,
                },
            )
        self.events.log(
            "executor.started",
            task_id=task.id,
            data={"run_id": run_id, "attempt": attempt_number, "model": executor_model},
        )
        self._set_activity_sink(
            tracker=self.executor_activity_tracker,
            run_id=run_id,
            task_id=task.id,
            attempt_number=attempt_number,
            provider=self._executor_provider(),
            role="executor",
            review_round=0,
        )
        self._emit_provider_event(
            run_id=run_id, task_id=task.id, attempt_number=attempt_number,
            provider=self._executor_provider(), role="executor", review_round=0,
            event_type=ProviderEventType.AGENT_STARTED,
            payload={"model": executor_model},
        )
        executor_finished_emitted = False
        try:
            result = self._execute_with_model(
                prompt,
                timeout_seconds=self.config.timeouts.executor_call_seconds,
                cwd=self.repo_root,
                model=executor_model,
            )
            self.store.record_invocation(
                run_id,
                task.id,
                role="executor",
                model=executor_model,
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
            self._emit_provider_event(
                run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                provider=self._executor_provider(), role="executor", review_round=0,
                event_type=ProviderEventType.AGENT_FINISHED,
                payload={"status": str(result.status), "duration": result.duration_seconds},
            )
            executor_finished_emitted = True
        finally:
            if not executor_finished_emitted:
                self._emit_provider_event(
                    run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                    provider=self._executor_provider(), role="executor", review_round=0,
                    event_type=ProviderEventType.AGENT_FINISHED,
                    payload={"status": "interrupted"},
                )
        self._clear_activity_sink(self.executor_activity_tracker)

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
                exit_code=result.exit_status,
                duration_seconds=result.duration_seconds,
                stderr=result.stderr_text,
                timed_out=(result.status == InvocationStatus.TIMEOUT),
            )

        self._transition(run_id, task.id, TaskState.REVIEW, attempt_count=attempt_number)
        return None

    def _review_task(
        self,
        run_id: str,
        task: Task,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        if self.reviewer_activity_tracker is not None:
            self.reviewer_activity_tracker.clear()
        record = self.store.get_task_state(run_id, task.id)
        if record is None:
            raise OrchestratorError(f"Missing task state for {task.id}")
        attempt_number = record.attempt_count
        self.store.update_phase_checkpoint(run_id, f"review:{task.id}:attempt:{attempt_number}")
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

        if not self.config.review.enabled:
            all_verification_passed = all(result.success for result in verification_results)
            findings = ReviewFindings(
                decision="accept",
                confidence=1.0,
                spec_compliance="pass" if all_verification_passed else "partial",
                summary=(
                    "Review disabled; verification passed."
                    if all_verification_passed
                    else "Review disabled; verification failed."
                ),
                findings=[],
            )
            return self._process_review_verdict(
                run_id,
                task,
                attempt_number,
                findings,
                RiskLevel.LOW,
                verification_results,
                ratchet_manager,
            )

        return self._adversarial_review(
            run_id, task, attempt_number, run_paths, latest_attempt,
            git_diff, verification_results, ratchet_manager,
        )

    def _adversarial_review(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        run_paths: RunPaths,
        latest_attempt,
        git_diff: str,
        verification_results: list,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        """Adversarial review path (v0.2) — independent first-pass + optional challenge."""

        # Check for shutdown before starting review
        pause_outcome = self._check_shutdown(run_id)
        if pause_outcome is not None:
            return pause_outcome

        # Risk classification before review
        diff_lines = count_diff_lines(git_diff)
        changed_files = extract_changed_files(git_diff)
        review_config = self.config.review
        diff_risk = classify_diff_risk(
            diff_lines,
            changed_files,
            security_paths=review_config.risk.security_paths or None,
            high_risk_paths=review_config.risk.high_risk_paths or None,
        )
        self.events.log(
            "review.risk_classified",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "diff_lines": diff_lines,
                "file_count": len(changed_files),
                "risk": diff_risk,
            },
        )

        # Build independent first-pass review prompt (no executor rationale)
        vr_dicts = [
            {
                "command": vr.command,
                "success": vr.success,
                "exit_code": vr.exit_code,
                "stdout": vr.stdout,
                "stderr": vr.stderr,
            }
            for vr in verification_results
        ]
        changed_snippets = collect_changed_file_snippets(self.repo_root, task.expected_files)

        first_pass_prompt = build_first_pass_prompt(
            task_id=task.id,
            task_title=task.title,
            task_description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            git_diff=git_diff,
            changed_file_snippets=changed_snippets,
            verification_results=vr_dicts,
            ratchet_overlay_history=ratchet_manager.load_overlay(task.id).verification_commands or None,
        )

        self.events.log(
            "reviewer.started",
            task_id=task.id,
            data={"run_id": run_id, "attempt": attempt_number, "round": 1},
        )
        self._set_activity_sink(
            tracker=self.reviewer_activity_tracker,
            run_id=run_id,
            task_id=task.id,
            attempt_number=attempt_number,
            provider=self._reviewer_provider(),
            role="reviewer",
            review_round=1,
        )
        self._emit_provider_event(
            run_id=run_id, task_id=task.id, attempt_number=attempt_number,
            provider=self._reviewer_provider(), role="reviewer", review_round=1,
            event_type=ProviderEventType.AGENT_STARTED,
            payload={"model": self._reviewer_model()},
        )
        reviewer_finished_emitted = False
        try:
            review_result: ReviewResult = self.reviewer.review(
                first_pass_prompt,
                timeout_seconds=self.config.timeouts.reviewer_call_seconds,
                cwd=self.repo_root,
            )
            self.store.record_invocation(
                run_id,
                task.id,
                role="reviewer",
                model=self._reviewer_model(),
                input_tokens=review_result.input_tokens,
                output_tokens=review_result.output_tokens,
                duration_seconds=review_result.duration_seconds,
                exit_status=0,
            )
            self._emit_provider_event(
                run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                provider=self._reviewer_provider(), role="reviewer", review_round=1,
                event_type=ProviderEventType.AGENT_FINISHED,
                payload={"status": str(review_result.status), "duration": review_result.duration_seconds},
            )
            reviewer_finished_emitted = True
        finally:
            if not reviewer_finished_emitted:
                self._emit_provider_event(
                    run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                    provider=self._reviewer_provider(), role="reviewer", review_round=1,
                    event_type=ProviderEventType.AGENT_FINISHED,
                    payload={"status": "interrupted"},
                )
        self._clear_activity_sink(self.reviewer_activity_tracker)

        # Persist review artifact
        review_path = run_paths.reviews_dir / f"{task.id}_attempt_{attempt_number}_round1.json"
        review_path.write_text(
            json.dumps(
                {
                    "status": review_result.status,
                    "findings": review_result.findings.model_dump() if review_result.findings else None,
                    "error_message": review_result.error_message,
                    "raw_output_text": review_result.raw_output_text or None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.store.update_attempt_artifacts(latest_attempt.id, review_path=str(review_path))

        if review_result.status != InvocationStatus.COMPLETED or review_result.findings is None:
            return self._handle_blocked_system(
                run_id,
                task,
                attempt_number,
                review_result.error_message or f"Reviewer status: {review_result.status}",
                duration_seconds=review_result.duration_seconds,
                timed_out=(review_result.status == InvocationStatus.TIMEOUT),
            )

        findings = review_result.findings
        self.events.log(
            "reviewer.verdict",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "round": 1,
                "decision": findings.decision,
                "confidence": findings.confidence,
                "finding_count": len(findings.findings),
                "risk": diff_risk,
            },
        )

        # Check if challenge round is needed
        if should_challenge(findings, diff_risk, review_config):
            challenge_outcome = self._challenge_round(
                run_id, task, attempt_number, run_paths, latest_attempt,
                git_diff, findings, diff_risk, verification_results,
                ratchet_manager, changed_snippets,
            )
            if challenge_outcome is not None:
                return challenge_outcome
            # After challenge, re-read the final findings from the challenge result
            # The challenge method handles accept/reject/escalation internally
            return None

        # No challenge — process the first-pass verdict directly
        return self._process_review_verdict(
            run_id, task, attempt_number, findings, diff_risk,
            verification_results, ratchet_manager,
        )

    def _challenge_round(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        run_paths: RunPaths,
        latest_attempt,
        git_diff: str,
        findings: ReviewFindings,
        diff_risk: RiskLevel,
        verification_results: list,
        ratchet_manager: RatchetManager,
        changed_snippets: dict[str, str],
    ) -> RunOutcome | None:
        """Run a bounded challenge round: executor repairs → reviewer re-reviews."""


        # Step 1: Send findings to executor with repair-first prompt
        if self.executor_activity_tracker is not None:
            self.executor_activity_tracker.clear()
        challenge_exec_prompt = build_challenge_executor_prompt(
            task_id=task.id,
            task_title=task.title,
            findings=[f.model_dump() for f in findings.findings],
            reviewer_summary=findings.summary,
        )

        self.events.log(
            "challenge.executor_started",
            task_id=task.id,
            data={"run_id": run_id, "attempt": attempt_number, "round": 2},
        )
        self.store.update_phase_checkpoint(run_id, f"challenge_executor:{task.id}:attempt:{attempt_number}")
        self._set_activity_sink(
            tracker=self.executor_activity_tracker,
            run_id=run_id,
            task_id=task.id,
            attempt_number=attempt_number,
            provider=self._executor_provider(),
            role="executor",
            review_round=2,
        )
        self._emit_provider_event(
            run_id=run_id, task_id=task.id, attempt_number=attempt_number,
            provider=self._executor_provider(), role="executor", review_round=2,
            event_type=ProviderEventType.AGENT_STARTED,
            payload={"model": self._executor_model(attempt_number)},
        )
        challenge_exec_finished = False
        try:
            exec_result = self._execute_with_model(
                challenge_exec_prompt,
                timeout_seconds=self.config.timeouts.executor_call_seconds,
                cwd=self.repo_root,
                model=self._executor_model(attempt_number),
            )
            self.store.record_invocation(
                run_id,
                task.id,
                role="executor",
                model=self._executor_model(),
                input_tokens=exec_result.input_tokens,
                output_tokens=exec_result.output_tokens,
                duration_seconds=exec_result.duration_seconds,
                exit_status=exec_result.exit_status,
            )
            self._emit_provider_event(
                run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                provider=self._executor_provider(), role="executor", review_round=2,
                event_type=ProviderEventType.AGENT_FINISHED,
                payload={"status": str(exec_result.status), "duration": exec_result.duration_seconds},
            )
            challenge_exec_finished = True
        finally:
            if not challenge_exec_finished:
                self._emit_provider_event(
                    run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                    provider=self._executor_provider(), role="executor", review_round=2,
                    event_type=ProviderEventType.AGENT_FINISHED,
                    payload={"status": "interrupted"},
                )
        self._clear_activity_sink(self.executor_activity_tracker)

        if exec_result.status != InvocationStatus.COMPLETED:
            return self._handle_blocked_system(
                run_id, task, attempt_number,
                exec_result.error_message or f"Challenge executor: {exec_result.status}",
                exit_code=exec_result.exit_status,
                duration_seconds=exec_result.duration_seconds,
                timed_out=(exec_result.status == InvocationStatus.TIMEOUT),
            )

        # Parse finding responses from executor output
        finding_responses: list[FindingResponse] = []
        try:
            finding_responses = parse_finding_responses(exec_result.output_text)
        except (ValueError, Exception):
            pass  # executor may not produce structured responses — that's ok

        # Step 2: Snapshot the post-repair diff under a distinct name so we
        # do not overwrite the first-pass patch for this attempt.
        challenge_patch_path = run_paths.patches_dir / f"{task.id}_attempt_{attempt_number}_challenge.patch"
        challenge_patch_path.parent.mkdir(parents=True, exist_ok=True)
        challenge_patch_path.write_text(
            self.git_manager._build_patch_snapshot(), encoding="utf-8",
        )
        # Update the attempt record so drift detection uses the post-challenge patch
        self.store.update_attempt_artifacts(
            latest_attempt.id,
            patch_path=str(challenge_patch_path),
        )
        new_diff_text = challenge_patch_path.read_text(encoding="utf-8")

        effective_commands = ratchet_manager.effective_commands(task.id, task.verification_commands)
        new_verification_results = self.verification_runner.run_commands(effective_commands)

        # Step 3: Run machine-checkable finding verification commands
        machine_check_commands = [
            f.verification_command
            for f in findings.findings
            if f.machine_checkable and f.verification_command
        ]
        if machine_check_commands:
            ratchet_manager.add_checks(task.id, machine_check_commands)

        # Step 4: Send new diff + responses to reviewer for final verdict
        if self.reviewer_activity_tracker is not None:
            self.reviewer_activity_tracker.clear()
        # Update changed snippets
        changed_snippets = collect_changed_file_snippets(self.repo_root, task.expected_files)

        vr_dicts = [
            {
                "command": vr.command,
                "success": vr.success,
                "exit_code": vr.exit_code,
            }
            for vr in new_verification_results
        ]
        challenge_review_prompt = build_challenge_reviewer_prompt(
            task_id=task.id,
            task_title=task.title,
            task_description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            original_findings=[f.model_dump() for f in findings.findings],
            executor_responses=[fr.model_dump() for fr in finding_responses],
            new_git_diff=new_diff_text,
            changed_file_snippets=changed_snippets,
            verification_results=vr_dicts,
        )

        self.events.log(
            "challenge.reviewer_started",
            task_id=task.id,
            data={"run_id": run_id, "attempt": attempt_number, "round": 2},
        )
        self.store.update_phase_checkpoint(run_id, f"challenge_reviewer:{task.id}:attempt:{attempt_number}")
        self._set_activity_sink(
            tracker=self.reviewer_activity_tracker,
            run_id=run_id,
            task_id=task.id,
            attempt_number=attempt_number,
            provider=self._reviewer_provider(),
            role="reviewer",
            review_round=2,
        )
        self._emit_provider_event(
            run_id=run_id, task_id=task.id, attempt_number=attempt_number,
            provider=self._reviewer_provider(), role="reviewer", review_round=2,
            event_type=ProviderEventType.AGENT_STARTED,
            payload={"model": self._reviewer_model()},
        )
        challenge_reviewer_finished = False
        try:
            final_result: ReviewResult = self.reviewer.review(
                challenge_review_prompt,
                timeout_seconds=self.config.timeouts.reviewer_call_seconds,
                cwd=self.repo_root,
            )
            self.store.record_invocation(
                run_id,
                task.id,
                role="reviewer",
                model=self._reviewer_model(),
                input_tokens=final_result.input_tokens,
                output_tokens=final_result.output_tokens,
                duration_seconds=final_result.duration_seconds,
                exit_status=0,
            )
            self._emit_provider_event(
                run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                provider=self._reviewer_provider(), role="reviewer", review_round=2,
                event_type=ProviderEventType.AGENT_FINISHED,
                payload={"status": str(final_result.status), "duration": final_result.duration_seconds},
            )
            challenge_reviewer_finished = True
        finally:
            if not challenge_reviewer_finished:
                self._emit_provider_event(
                    run_id=run_id, task_id=task.id, attempt_number=attempt_number,
                    provider=self._reviewer_provider(), role="reviewer", review_round=2,
                    event_type=ProviderEventType.AGENT_FINISHED,
                    payload={"status": "interrupted"},
                )
        self._clear_activity_sink(self.reviewer_activity_tracker)

        # Persist challenge review
        review_path = run_paths.reviews_dir / f"{task.id}_attempt_{attempt_number}_round2.json"
        review_path.write_text(
            json.dumps(
                {
                    "status": final_result.status,
                    "findings": final_result.findings.model_dump() if final_result.findings else None,
                    "error_message": final_result.error_message,
                    "raw_output_text": final_result.raw_output_text or None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.store.update_attempt_artifacts(latest_attempt.id, review_path=str(review_path))

        if final_result.status != InvocationStatus.COMPLETED or final_result.findings is None:
            return self._handle_blocked_system(
                run_id, task, attempt_number,
                final_result.error_message or f"Challenge reviewer: {final_result.status}",
                duration_seconds=final_result.duration_seconds,
                timed_out=(final_result.status == InvocationStatus.TIMEOUT),
            )

        final_findings = final_result.findings
        self.events.log(
            "reviewer.verdict",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "round": 2,
                "decision": final_findings.decision,
                "confidence": final_findings.confidence,
                "finding_count": len(final_findings.findings),
            },
        )

        # Check for escalation after challenge
        if should_escalate(
            final_findings, diff_risk, self.config.review,
            rejection_count=self._rejection_counts.get(task.id, 0),
        ):
            return self._escalate_to_human(
                run_id, task, attempt_number,
                final_findings, finding_responses, diff_risk,
            )

        return self._process_review_verdict(
            run_id, task, attempt_number, final_findings, diff_risk,
            new_verification_results, ratchet_manager,
        )

    def _process_review_verdict(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        findings: ReviewFindings,
        diff_risk: RiskLevel,
        verification_results: list,
        ratchet_manager: RatchetManager,
    ) -> RunOutcome | None:
        """Process a review verdict — accept, reject, or escalate."""
        all_verification_passed = all(
            getattr(item, "success", item.get("success", False) if isinstance(item, dict) else False)
            for item in verification_results
        )

        if findings.decision == "accept" and all_verification_passed:
            self._rejection_counts.pop(task.id, None)
            if self.git_manager.is_clean():
                commit_sha = self.git_manager.current_commit()
            else:
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

        rejection_count = self._rejection_counts.get(task.id, 0)
        if findings.decision == "reject":
            rejection_count += 1
            self._rejection_counts[task.id] = rejection_count

            # Check escalation on rejection
            if should_escalate(
                findings, diff_risk, self.config.review,
                rejection_count=rejection_count,
            ):
                return self._escalate_to_human(
                    run_id, task, attempt_number, findings, [], diff_risk,
                )
        else:
            # Reviewer accepted the code; any remaining loop is about verification,
            # not reviewer disagreement, so reset the rejection counter.
            self._rejection_counts.pop(task.id, None)

        repair_notes = findings.repair_notes
        if findings.decision == "accept" and not all_verification_passed:
            failed_commands = [
                getattr(item, "command", item.get("command", "?") if isinstance(item, dict) else "?")
                for item in verification_results
                if not getattr(item, "success", item.get("success", False) if isinstance(item, dict) else False)
            ]
            repair_notes = "Verification failed for: " + ", ".join(failed_commands)
            new_commands: list[str] = []
        else:
            new_commands = findings.new_verification_commands

        # Detect recurring findings across attempts — if the same issues keep
        # appearing, the executor likely can't fix them (e.g. missing system
        # package) and the human should be notified early.
        if attempt_number >= 2:
            recurring = self._detect_recurring_findings(run_id, task, attempt_number, findings)
            if recurring:
                return self._escalate_to_human_environment(
                    run_id, task, attempt_number, findings, recurring, diff_risk,
                )

            # Also detect recurring verification failures (same commands keep failing)
            if findings.decision == "accept" and not all_verification_passed:
                recurring_cmds = self._detect_recurring_verification_failures(
                    run_id, task, attempt_number, verification_results,
                )
                if recurring_cmds:
                    return self._escalate_to_human_environment(
                        run_id, task, attempt_number, findings,
                        [f"Verification keeps failing: {cmd}" for cmd in recurring_cmds],
                        diff_risk,
                    )

        ratchet_result = ratchet_manager.add_checks(task.id, new_commands)
        for command in ratchet_result.accepted:
            self.events.log(
                "ratchet.added",
                task_id=task.id,
                data={"run_id": run_id, "command": command},
            )

        if attempt_number >= self.config.ralph.max_retries_per_task:
            return self._handle_exhausted(
                run_id, task, attempt_number, findings,
                repair_notes, diff_risk,
            )

        # Persist rejection count so it survives resume
        repair_payload = json.dumps({
            "repair_notes": repair_notes or findings.summary,
            "rejection_count": self._rejection_counts.get(task.id, 0),
        })
        self._transition(
            run_id,
            task.id,
            TaskState.REPAIR,
            attempt_count=attempt_number,
            last_error=repair_payload,
        )
        return None

    def _handle_exhausted(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        findings: ReviewFindings,
        repair_notes: str | None,
        diff_risk: RiskLevel,
    ) -> RunOutcome:
        """Stop the run when a task exhausts its retry budget.

        Reverts uncommitted changes from the failed task to prevent workspace
        drift, and provides the operator with two clear resume options.
        """
        budget = self.config.ralph.max_retries_per_task

        # Build a summary of what went wrong across attempts
        summary_parts: list[str] = []
        if repair_notes:
            summary_parts.append(repair_notes)
        if findings.findings:
            summary_parts.append(f"Last review had {len(findings.findings)} finding(s):")
            for f in findings.findings[:5]:
                summary_parts.append(f"  {f.id} [{f.severity}] ({f.category}): {f.description}")

        actions = [
            f"Task {task.id} failed after {attempt_number}/{budget} attempts.",
            "",
            "Option 1 — Discard changes and restart from scratch (default):",
            f"  ralph resume --run-id {run_id}",
            "  This reverts all uncommitted changes and re-implements the task from attempt 0.",
            "",
            "Option 2 — Increase retry budget and continue from current state:",
            f"  ralph resume --run-id {run_id} --max-retries {budget + 3}",
            "  This keeps the current code and gives the executor more attempts.",
        ]

        reason = make_human_intervention_payload(
            summary=f"Retry budget exhausted for {task.id} after {attempt_number} attempts.",
            what_failed="\n".join(summary_parts) if summary_parts else "Retry budget exhausted.",
            expected_human_actions=actions,
            findings=[
                {
                    "id": f.id,
                    "severity": str(f.severity),
                    "category": str(f.category),
                    "description": f.description,
                    "suggested_fix": f.suggested_fix,
                    "verification_command": f.verification_command,
                }
                for f in findings.findings
            ],
            metadata={
                "task_id": task.id,
                "risk": str(diff_risk),
                "attempt_count": attempt_number,
                "max_retries": budget,
                "escalation_reason": "retry_budget_exhausted",
            },
        )

        self._transition(
            run_id,
            task.id,
            TaskState.FAILED_EXHAUSTED,
            attempt_count=attempt_number,
            last_error=reason,
        )

        # Revert uncommitted changes from the failed task to prevent
        # workspace drift errors on subsequent tasks.
        if not self.git_manager.is_clean():
            self.git_manager.reset_to_head_clean()

        self.store.update_run_status(run_id, RunStatus.BLOCKED_HUMAN)
        self.events.log(
            "run.stopped",
            task_id=task.id,
            data={
                "run_id": run_id,
                "status": RunStatus.BLOCKED_HUMAN,
                "reason": f"Retry budget exhausted for {task.id}.",
                "attempt_count": attempt_number,
                "max_retries": budget,
            },
        )
        return RunOutcome(
            run_id=run_id,
            status=RunStatus.BLOCKED_HUMAN,
            reason=f"Retry budget exhausted for {task.id} after {attempt_number} attempts.",
        )

    def _escalate_to_human(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        findings: ReviewFindings,
        finding_responses: list[FindingResponse],
        diff_risk: RiskLevel,
    ) -> RunOutcome:
        """Escalate to human with structured context."""
        actions = [
            "Read the unresolved reviewer findings below and decide whether to repair or accept the risk.",
        ]
        for finding in findings.findings[:3]:
            if finding.suggested_fix:
                actions.append(f"{finding.id}: {finding.suggested_fix}")
            else:
                actions.append(f"{finding.id}: inspect and address the reported issue.")
            if finding.verification_command:
                actions.append(f"{finding.id}: rerun `{finding.verification_command}` after making changes.")
        actions.append(f"Run `ralph resume --run-id {run_id}` after applying the chosen fix.")

        reason = make_human_intervention_payload(
            summary=f"Adversarial review escalation for {task.id}.",
            what_failed=(
                f"Reviewer rejected the task with {len(findings.findings)} unresolved finding(s) "
                f"at {findings.confidence:.2f} confidence under {diff_risk} scrutiny."
            ),
            expected_human_actions=actions,
            findings=[
                {
                    "id": f.id,
                    "severity": str(f.severity),
                    "category": str(f.category),
                    "description": f.description,
                    "suggested_fix": f.suggested_fix,
                    "verification_command": f.verification_command,
                }
                for f in findings.findings
            ],
            metadata={
                "task_id": task.id,
                "risk": str(diff_risk),
                "decision": findings.decision,
                "confidence": findings.confidence,
                "executor_responses": [
                    {
                        "finding_id": fr.finding_id,
                        "verdict": str(fr.verdict),
                        "explanation": fr.explanation,
                    }
                    for fr in finding_responses
                ],
            },
        )
        self._transition(
            run_id,
            task.id,
            TaskState.BLOCKED_HUMAN,
            attempt_count=attempt_number,
            last_error=reason,
        )
        self.store.update_run_status(run_id, RunStatus.BLOCKED_HUMAN)
        self.events.log(
            "review.escalated",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "risk": diff_risk,
                "reason": "human_escalation",
            },
        )
        return RunOutcome(
            run_id=run_id,
            status=RunStatus.BLOCKED_HUMAN,
            reason=f"Adversarial review escalation for {task.id}.",
        )

    def _detect_recurring_findings(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        current_findings: ReviewFindings,
    ) -> list[str] | None:
        """Detect findings that recur across attempts.

        Returns a list of recurring finding descriptions if the same issues
        keep appearing (indicating the executor cannot fix them, e.g. a missing
        system package or environmental dependency). Returns None if no
        recurring pattern is detected.
        """
        run_paths = self._ensure_run_paths(run_id)

        # Collect finding signatures from previous attempts
        past_signatures: dict[str, int] = {}  # normalized description → count
        for prev_attempt in range(1, attempt_number):
            for round_num in (1, 2):
                review_file = run_paths.reviews_dir / f"{task.id}_attempt_{prev_attempt}_round{round_num}.json"
                if not review_file.exists():
                    continue
                try:
                    payload = json.loads(review_file.read_text(encoding="utf-8"))
                    findings_data = payload.get("findings")
                    if not isinstance(findings_data, dict):
                        continue
                    for f in findings_data.get("findings", []):
                        if not isinstance(f, dict):
                            continue
                        desc = str(f.get("description", "")).strip().lower()
                        cat = str(f.get("category", ""))
                        sig = f"{cat}:{desc[:80]}"
                        past_signatures[sig] = past_signatures.get(sig, 0) + 1
                except (OSError, ValueError, json.JSONDecodeError):
                    continue

        if not past_signatures:
            return None

        # Check current findings against past signatures
        recurring: list[str] = []
        for finding in current_findings.findings:
            desc = finding.description.strip().lower()
            cat = str(finding.category)
            sig = f"{cat}:{desc[:80]}"
            if past_signatures.get(sig, 0) >= 1:
                recurring.append(finding.description)

        # Only flag if a meaningful proportion of findings are recurring
        if len(recurring) >= 1 and len(recurring) >= len(current_findings.findings) * 0.5:
            return recurring
        return None

    def _detect_recurring_verification_failures(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        current_results: list,
    ) -> list[str] | None:
        """Detect verification commands that keep failing across attempts."""
        current_failed = set()
        for item in current_results:
            success = getattr(item, "success", item.get("success", False) if isinstance(item, dict) else False)
            if not success:
                cmd = getattr(item, "command", item.get("command", "?") if isinstance(item, dict) else "?")
                current_failed.add(cmd)

        if not current_failed:
            return None

        # Check past verification events
        past_failed: dict[str, int] = {}
        for event in self.events.read_events():
            if event.data.get("run_id") != run_id:
                continue
            if event.task_id != task.id:
                continue
            if event.event_type != "verification.result":
                continue
            past_attempt = event.data.get("attempt", 0)
            if past_attempt >= attempt_number:
                continue
            if not event.data.get("success"):
                cmd = event.data.get("command", "?")
                past_failed[cmd] = past_failed.get(cmd, 0) + 1

        recurring = [cmd for cmd in current_failed if past_failed.get(cmd, 0) >= 1]
        return recurring if recurring else None

    def _escalate_to_human_environment(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        findings: ReviewFindings,
        recurring_descriptions: list[str],
        diff_risk: RiskLevel,
    ) -> RunOutcome:
        """Escalate to human when findings recur across attempts.

        This typically indicates an environmental issue the executor cannot fix
        (missing package, system dependency, configuration issue).
        """
        actions: list[str] = [
            "The following issues keep appearing across multiple attempts, suggesting "
            "the executor cannot resolve them autonomously:",
        ]
        for desc in recurring_descriptions[:5]:
            actions.append(f"  - {desc}")
        actions.append("")
        actions.append("Likely actions needed:")

        # Detect common patterns and suggest specific actions
        combined = " ".join(recurring_descriptions).lower()
        if any(kw in combined for kw in ("package", "module", "import", "install", "not found", "no module")):
            actions.append("- Install the missing package (e.g. `pip install <pkg>`, `npm install <pkg>`, `apt install <pkg>`)")
        if any(kw in combined for kw in ("permission", "access denied", "forbidden")):
            actions.append("- Fix file or service permissions")
        if any(kw in combined for kw in ("config", "environment", "env var", "variable")):
            actions.append("- Set the required environment variable or configuration")
        if any(kw in combined for kw in ("test", "verification", "assertion")):
            actions.append("- Review and fix the failing test or verification command")

        actions.append(f"- Run `ralph resume --run-id {run_id}` once the environment is ready.")

        reason = make_human_intervention_payload(
            summary=f"Recurring issues detected for {task.id} — likely environmental.",
            what_failed=(
                f"The same finding(s) appeared across {attempt_number} attempts, "
                f"indicating the executor cannot resolve them. "
                f"This usually means a missing dependency or environmental issue."
            ),
            expected_human_actions=actions,
            findings=[
                {
                    "id": f.id,
                    "severity": str(f.severity),
                    "category": str(f.category),
                    "description": f.description,
                    "suggested_fix": f.suggested_fix,
                    "verification_command": f.verification_command,
                }
                for f in findings.findings
            ],
            metadata={
                "task_id": task.id,
                "risk": str(diff_risk),
                "recurring_findings": recurring_descriptions,
                "escalation_reason": "recurring_findings",
            },
        )
        self._transition(
            run_id,
            task.id,
            TaskState.BLOCKED_HUMAN,
            attempt_count=attempt_number,
            last_error=reason,
        )
        self.store.update_run_status(run_id, RunStatus.BLOCKED_HUMAN)
        self.events.log(
            "review.escalated",
            task_id=task.id,
            data={
                "run_id": run_id,
                "attempt": attempt_number,
                "risk": diff_risk,
                "reason": "recurring_findings",
                "recurring_count": len(recurring_descriptions),
            },
        )
        return RunOutcome(
            run_id=run_id,
            status=RunStatus.BLOCKED_HUMAN,
            reason=f"Recurring unresolvable findings for {task.id} — human intervention needed.",
        )

    def _base_executor_model(self) -> str:
        if self.config.roles is not None:
            return self.config.roles.executor.model
        return self.config.models.executor

    def _executor_model(self, attempt_number: int | None = None) -> str:
        base_model = self._base_executor_model()
        if attempt_number is None or not self._is_auto_escalated_executor_model(attempt_number):
            return base_model
        return self.config.ralph.auto_escalate_model

    def _is_auto_escalated_executor_model(self, attempt_number: int) -> bool:
        threshold = self.config.ralph.auto_escalate
        if threshold is None:
            return False
        if attempt_number < threshold:
            return False
        return self.config.ralph.auto_escalate_model != self._base_executor_model()

    def _execute_with_model(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | Path | None,
        model: str,
    ) -> ExecutorResult:
        transport = getattr(self.executor, "transport", None)
        original_model = getattr(transport, "model", None)
        if isinstance(original_model, str):
            transport.model = model
            try:
                return self.executor.execute(
                    prompt,
                    timeout_seconds=timeout_seconds,
                    cwd=cwd,
                )
            finally:
                transport.model = original_model
        return self.executor.execute(
            prompt,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )

    def _reviewer_model(self) -> str:
        if self.config.roles is not None:
            return self.config.roles.reviewer.model
        return "unknown"

    def _executor_provider(self) -> str:
        if self.config.roles is not None:
            return self.config.roles.executor.provider
        return "claude"

    def _reviewer_provider(self) -> str:
        if self.config.roles is not None:
            return self.config.roles.reviewer.provider
        return "claude"

    def _clear_transport_sessions(self) -> None:
        """Clear session state on transports that support it (e.g. Claude --resume)."""
        for adapter in (self.executor, self.reviewer):
            transport = getattr(adapter, "transport", None)
            clear_fn = getattr(transport, "clear_session", None)
            if callable(clear_fn):
                clear_fn()

    def _set_activity_sink(
        self,
        *,
        tracker: LiveActivityTracker | None,
        run_id: str,
        task_id: str,
        attempt_number: int,
        provider: str,
        role: str,
        review_round: int,
    ) -> None:
        if tracker is None:
            return

        def _sink(event: ActivityEvent) -> None:
            if event.kind == ActivityKind.COMMENTARY and event.block_id is not None:
                block_state = event.block_state or "update"
                event_type = {
                    "start": ProviderEventType.AGENT_THOUGHT_STARTED,
                    "update": ProviderEventType.AGENT_THOUGHT_DELTA,
                    "finish": ProviderEventType.AGENT_THOUGHT_FINISHED,
                }.get(block_state, ProviderEventType.AGENT_THOUGHT_DELTA)
            else:
                event_type = {
                    ActivityKind.COMMENTARY: ProviderEventType.AGENT_COMMENTARY,
                    ActivityKind.PROGRESS: ProviderEventType.AGENT_PROGRESS,
                    ActivityKind.ERROR: ProviderEventType.AGENT_ERROR,
                    ActivityKind.RESULT: ProviderEventType.AGENT_STRUCTURED_OUTPUT,
                }.get(event.kind)
            if event_type is None:
                return
            self._emit_provider_event(
                run_id=run_id,
                task_id=task_id,
                attempt_number=attempt_number,
                provider=provider,
                role=role,
                review_round=review_round,
                event_type=event_type,
                payload={
                    "text": event.text if event.text is not None else event.summary,
                    "summary": event.summary,
                    "kind": event.kind.value,
                    "block_id": event.block_id,
                    "block_state": event.block_state,
                },
            )

        tracker.set_sink(_sink)

    @staticmethod
    def _clear_activity_sink(tracker: LiveActivityTracker | None) -> None:
        if tracker is not None:
            tracker.set_sink(None)

    def _emit_provider_event(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_number: int,
        provider: str,
        role: str,
        review_round: int,
        event_type: ProviderEventType,
        payload: dict | None = None,
    ) -> None:
        """Emit and persist a provider event."""
        event = make_event(
            run_id=run_id,
            task_id=task_id,
            attempt_number=attempt_number,
            provider=provider,
            role=role,
            review_round=review_round,
            event_type=event_type,
            payload=payload,
        )
        self.provider_event_store.log(event)

    def _handle_blocked_system(
        self,
        run_id: str,
        task: Task,
        attempt_number: int,
        message: str,
        *,
        exit_code: int | None = None,
        duration_seconds: float = 0.0,
        stderr: str = "",
        timed_out: bool = False,
    ) -> RunOutcome:
        # If the operator requested shutdown, don't classify as a system failure —
        # transition to paused so resume can pick up cleanly.
        if self._shutdown_requested.is_set():
            self._transition(
                run_id, task.id, TaskState.READY,
                attempt_count=max(attempt_number - 1, 0),
                last_error=None,
            )
            self.store.update_run_status(run_id, RunStatus.PAUSED)
            self.events.log(
                "run.stopped",
                data={"run_id": run_id, "status": RunStatus.PAUSED, "reason": "Operator interrupt during provider call"},
            )
            return RunOutcome(run_id=run_id, status=RunStatus.PAUSED, reason="Operator interrupt")

        # Classify the failure
        failure_class = classify_failure(
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            stderr=stderr,
            timed_out=timed_out,
            error_message=message,
        )
        detail = FailureDetail(
            failure_class=failure_class,
            summary=message,
        )
        self.store.update_task_failure(
            run_id, task.id,
            failure_class=failure_class.value,
            failure_detail_json=detail.model_dump_json(),
        )

        # Attempt self-healing if enabled
        healing_config = self.config.self_healing
        if healing_config.enabled:
            from .self_healing import attempt_self_healing, is_failure_eligible_for_diagnosis
            if is_failure_eligible_for_diagnosis(failure_class):
                recovery = attempt_self_healing(
                    config=healing_config,
                    task_id=task.id,
                    task_title=task.title,
                    task_description=task.description,
                    failure_class=failure_class,
                    error_message=message,
                    stderr_summary=stderr,
                    cwd=self.repo_root,
                    store=self.store,
                    run_id=run_id,
                    events=self.events,
                )
                if recovery.succeeded:
                    # Auto-fix worked — retry the task instead of blocking
                    self.events.log(
                        "self_healing.recovery_succeeded",
                        task_id=task.id,
                        data={
                            "run_id": run_id,
                            "failure_class": failure_class.value,
                            "actions": recovery.actions_taken,
                            "summary": recovery.summary,
                        },
                    )
                    # Transition back to READY for retry
                    self._transition(
                        run_id,
                        task.id,
                        TaskState.READY,
                        attempt_count=attempt_number,
                        last_error=f"Self-healed: {recovery.summary}",
                    )
                    return None  # type: ignore[return-value]  # None means "continue loop"
                else:
                    # Diagnosis ran but couldn't auto-fix — enrich the error message
                    if recovery.diagnosis:
                        message = (
                            f"{message}\n\n"
                            f"Self-healing diagnosis: {recovery.diagnosis.root_cause_summary}\n"
                            f"Human actions: {'; '.join(recovery.diagnosis.human_actions)}"
                        )

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
            data={
                "run_id": run_id,
                "status": RunStatus.BLOCKED_SYSTEM,
                "reason": message,
                "failure_class": failure_class.value,
            },
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

    def _maybe_stop_after_task_limit(
        self,
        *,
        run_id: str,
        task_id: str,
        scheduler: Scheduler,
        completed_task_ids: set[str],
        task_limit: int | None,
    ) -> RunOutcome | None:
        if task_limit is None or task_id in completed_task_ids:
            return None
        if self.fsm.get_state(run_id, task_id) is not TaskState.DONE:
            return None

        completed_task_ids.add(task_id)
        if len(completed_task_ids) < task_limit:
            return None
        if scheduler.is_plan_complete():
            self.store.update_run_status(run_id, RunStatus.COMPLETED)
            self.events.log(
                "run.stopped",
                data={"run_id": run_id, "status": RunStatus.COMPLETED, "reason": "Plan complete"},
            )
            return RunOutcome(run_id=run_id, status=RunStatus.COMPLETED, reason="Plan complete")

        reason = f"Task limit reached after completing {len(completed_task_ids)} task(s)."
        self.store.update_run_status(run_id, RunStatus.PAUSED)
        self.events.log(
            "run.stopped",
            data={
                "run_id": run_id,
                "status": RunStatus.PAUSED,
                "reason": reason,
                "task_limit": task_limit,
            },
        )
        return RunOutcome(run_id=run_id, status=RunStatus.PAUSED, reason=reason)

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
