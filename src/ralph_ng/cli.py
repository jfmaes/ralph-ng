"""CLI entrypoints for ralph-ng."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import os
import secrets
import signal
import subprocess
import threading
import time

import typer
from rich.console import Console
from rich.live import Live

from .adapters.factory import build_executor, build_reviewer
from .adapters.streaming import LiveActivityTracker
from .config import RalphConfig, load_config
from .events import EventLogger
from .git_manager import GitError, GitManager
from .orchestrator import Orchestrator, OrchestratorError, RunStatus
from .providers.event_store import ProviderEventStore
from .plan import load_plan
from .progress import ProgressRenderer
from .prompt_builder import PromptBuilder
from .resume import ResumeAction, ResumeManager
from .review_bundle import ReviewBundleBuilder
from .state import StateStore
from .utils import parse_human_intervention_payload
try:
    from .tui import run_textual_progress, watch_textual_progress
except ModuleNotFoundError:  # pragma: no cover - fallback when textual isn't installed
    run_textual_progress = None
    watch_textual_progress = None
from .validation import PlanValidationError, validate_plan, validate_plan_file
from .verification import VerificationRunner


# Global reference for signal handler access
_active_orchestrator: Orchestrator | None = None
_active_run_id: str | None = None
_sigint_count = 0
_sigint_lock = threading.Lock()


def _cleanup_dirty_workspace(repo_root: Path, console: Console) -> None:
    """Reset uncommitted changes left by a dead/paused run."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode == 0 and status.stdout.strip():
        console.print("[yellow]Cleaning up uncommitted changes from stale run...[/yellow]")
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_root, capture_output=True, check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=repo_root, capture_output=True, check=False)


app = typer.Typer(
    help="ralph-ng autonomous coding orchestrator",
    invoke_without_command=True,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()
_SIGINT_FORCE_WINDOW_SECONDS = 3.0


def _sigint_handler(signum, frame):
    """Handle SIGINT: first press pauses, second press within 3s forces exit."""
    global _sigint_count
    with _sigint_lock:
        _sigint_count += 1
        count = _sigint_count
    if _active_orchestrator is not None:
        _active_orchestrator.request_shutdown()
    if count >= 2:
        # Second SIGINT — force kill child processes then exit
        console.print("\n[red]Force quit — killing provider subprocesses...[/red]")
        _force_kill_children()
        _emergency_release_controller()
        os._exit(130)
    else:
        console.print("\n[yellow]Pausing at next checkpoint... (press Ctrl+C again to force quit)[/yellow]")


def _force_kill_children() -> None:
    """SIGKILL all child process groups spawned by stream_subprocess.

    Provider subprocesses are started with start_new_session=True, so they
    have their own process groups.  We find them via /proc on Linux.
    """
    if os.name == "nt":
        return
    my_pid = os.getpid()
    try:
        # Read /proc to find children — no third-party dependency needed
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                stat = open(f"/proc/{entry}/stat").read()
                # Field 4 is PPID
                fields = stat.split()
                ppid = int(fields[3])
                child_pid = int(fields[0])
                if ppid == my_pid:
                    # Kill the child's entire process group
                    try:
                        pgid = os.getpgid(child_pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass


def _emergency_release_controller() -> None:
    """Best-effort controller release on force quit — no exceptions allowed."""
    try:
        if _active_orchestrator is None or _active_run_id is None:
            return
        store = _active_orchestrator.store
        run = store.get_run(_active_run_id)
        if run is not None and run.status == "running":
            store.release_controller(
                _active_run_id,
                stop_reason='{"cause": "force_killed", "detail": "Double SIGINT"}',
            )
            store.update_run_status(_active_run_id, "paused")
    except Exception:
        pass  # best-effort — don't crash the emergency handler


def _install_signal_handlers(orchestrator: Orchestrator) -> tuple:
    """Install graceful signal handlers. Returns previous handlers for restoration."""
    global _active_orchestrator, _sigint_count
    _active_orchestrator = orchestrator
    _sigint_count = 0
    prev_sigint = signal.signal(signal.SIGINT, _sigint_handler)
    prev_sigterm = signal.SIG_DFL
    if os.name != "nt":
        prev_sigterm = signal.signal(signal.SIGTERM, _sigint_handler)
    return prev_sigint, prev_sigterm


def _restore_signal_handlers(prev_handlers: tuple) -> None:
    """Restore previous signal handlers."""
    global _active_orchestrator, _active_run_id
    _active_orchestrator = None
    _active_run_id = None
    prev_sigint, prev_sigterm = prev_handlers
    signal.signal(signal.SIGINT, prev_sigint)
    if os.name != "nt":
        signal.signal(signal.SIGTERM, prev_sigterm)


def generate_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S-%f")
    return f"{timestamp}-{secrets.token_hex(2)}"


def resolve_repo_root() -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("Current directory is not inside a git repository.")
    return Path(completed.stdout.strip())


def resolve_state_root(repo_root: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("Could not resolve git directory for repository.")
    git_dir = repo_root / completed.stdout.strip()
    return git_dir / "ralph"


def build_orchestrator(
    *,
    repo_root: Path,
    plan,
    plan_path: Path,
    config: RalphConfig,
    state_root: Path,
    store: StateStore,
    events: EventLogger,
    executor_activity_tracker: LiveActivityTracker | None = None,
    reviewer_activity_tracker: LiveActivityTracker | None = None,
) -> Orchestrator:
    specs_dir = repo_root / "specs"
    spec_paths = sorted(specs_dir.glob("*.md")) if specs_dir.exists() else []

    reviewer = build_reviewer(config, tracker=reviewer_activity_tracker)
    provider_event_store = ProviderEventStore(state_root)

    return Orchestrator(
        repo_root=repo_root,
        plan=plan,
        plan_path=plan_path,
        config=config,
        state_root=state_root,
        store=store,
        events=events,
        git_manager=GitManager(
            repo_root=repo_root,
            branch_prefix=config.ralph.branch_prefix,
            protected_branches=config.git.protected_branches,
            force_push=config.git.force_push,
        ),
        prompt_builder=PromptBuilder(repo_root=repo_root, spec_paths=spec_paths),
        executor=build_executor(config, tracker=executor_activity_tracker),
        reviewer=reviewer,
        verification_runner=VerificationRunner(
            timeout_seconds=config.timeouts.verification_command_seconds,
            cwd=repo_root,
        ),
        review_bundle_builder=ReviewBundleBuilder(repo_root=repo_root),
        provider_event_store=provider_event_store,
        executor_activity_tracker=executor_activity_tracker,
        reviewer_activity_tracker=reviewer_activity_tracker,
    )


def run_with_progress(
    orchestrator_call,
    *,
    run_id: str,
    plan,
    config: RalphConfig,
    store: StateStore,
    events: EventLogger,
    provider_event_store: ProviderEventStore | None = None,
    executor_activity_tracker: LiveActivityTracker | None = None,
    reviewer_activity_tracker: LiveActivityTracker | None = None,
    orchestrator: Orchestrator | None = None,
    state_root: str | None = None,
):
    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=provider_event_store,
        config=config,
        executor_activity_tracker=executor_activity_tracker,
        reviewer_activity_tracker=reviewer_activity_tracker,
    )
    if not console.is_terminal:
        return orchestrator_call()

    if run_textual_progress is not None:
        return run_textual_progress(
            orchestrator_call,
            run_id=run_id,
            plan=plan,
            config=config,
            store=store,
            events=events,
            provider_event_store=provider_event_store or ProviderEventStore(store.db_path.parent),
            executor_activity_tracker=executor_activity_tracker,
            reviewer_activity_tracker=reviewer_activity_tracker,
            orchestrator=orchestrator,
            state_root=state_root,
        )

    outcome_holder: dict[str, object] = {}
    error_holder: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            outcome_holder["outcome"] = orchestrator_call()
        except BaseException as exc:  # pragma: no cover - exercised via foreground fallback
            error_holder["error"] = exc

    worker = threading.Thread(target=_target)
    worker.start()

    with Live(console=console, refresh_per_second=2, transient=False, vertical_overflow="visible") as live:
        while worker.is_alive():
            try:
                live.update(renderer.build_renderable(
                    run_id, plan=plan, console_width=console.width,
                ))
            except KeyError:
                pass
            worker.join(timeout=0.1)
        if "error" in error_holder:
            raise error_holder["error"]
        try:
            live.update(renderer.build_renderable(
                run_id, plan=plan, console_width=console.width,
            ))
        except KeyError:
            pass

    return outcome_holder["outcome"]


def _print_operator_guidance(
    *,
    run_id: str,
    store: StateStore,
    events: EventLogger,
    provider_event_store: ProviderEventStore,
) -> None:
    task_states = store.list_task_states(run_id)
    blocked = [
        record
        for record in task_states.values()
        if record.state in {"blocked_human", "blocked_system", "failed_exhausted"}
    ]
    if not blocked:
        return

    record = max(blocked, key=lambda item: item.updated_at)
    console.print("\n[bold]Operator Guidance[/bold]")
    console.print(f"Task: {record.task_id}  State: {record.state}  Attempt: {record.attempt_count}")

    if record.last_error:
        structured = parse_human_intervention_payload(record.last_error)
        if structured is not None:
            summary = structured.get("summary")
            what_failed = structured.get("what_failed")
            actions = structured.get("expected_human_actions", [])
            findings = structured.get("findings", [])
            if summary:
                console.print(f"Summary: {summary}")
            if what_failed:
                console.print(f"Failed: {what_failed}")
            if isinstance(findings, list):
                for finding in findings[:5]:
                    if not isinstance(finding, dict):
                        continue
                    fid = finding.get("id", "?")
                    severity = finding.get("severity", "?")
                    category = finding.get("category", "?")
                    description = finding.get("description", "")
                    console.print(f"{fid} [{severity}] ({category}): {description}")
            if isinstance(actions, list) and actions:
                console.print("Do this:")
                for action in actions[:6]:
                    if isinstance(action, str) and action.strip():
                        console.print(f"- {action}")
        else:
            console.print(f"Reason: {ProgressRenderer._humanize_error(record.last_error, max_len=400)}")

    console.print(f"Logs: {events.path}")
    pe_path = provider_event_store._event_file(run_id, record.task_id, record.attempt_count)
    if pe_path.exists():
        console.print(f"Provider events: {pe_path}")
    console.print(f"Resume: ralph resume --run-id {run_id}")


@app.command("validate-plan")
def validate_plan_command(
    plan: Path = typer.Option(
        Path("ralph-plan.yaml"),
        "--plan",
        "-p",
        help="Path to the implementation plan YAML file.",
    ),
    base_dir: Path = typer.Option(
        Path("."),
        "--base-dir",
        help="Base directory used for relative file reference validation.",
    ),
) -> None:
    """Validate a plan file against the plan contract."""

    try:
        validate_plan_file(plan, base_dir=base_dir)
    except (OSError, ValueError, PlanValidationError) as exc:
        console.print(f"[red]Plan validation failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Plan is valid:[/green] {plan}")


@app.command("start")
def start_command(
    plan: Path = typer.Option(Path("ralph-plan.yaml"), "--plan", "-p"),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
    number_of_tasks: int | None = typer.Option(
        None,
        "--number-of-tasks",
        min=1,
        help="Stop cleanly after this many tasks reach done during this invocation.",
    ),
) -> None:
    """Start a new run."""

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        plan_path = plan if plan.is_absolute() else repo_root / plan
        loaded_config = load_config(config_path)
        validated_plan = validate_plan_file(plan_path, base_dir=repo_root)
        from .preflight import run_preflight

        preflight = run_preflight(
            repo_root=repo_root,
            config=loaded_config,
            plan=validated_plan,
        )
        if preflight.checks:
            console.print("[bold]Preflight Checks[/bold]")
            console.print(preflight.summary())
            if not preflight.all_passed:
                console.print(
                    "[yellow]Continuing despite preflight warnings. "
                    "The run may still succeed, but missing tools or a dirty workspace can cause later failures.[/yellow]"
                )
        state_root = resolve_state_root(repo_root)
        state_root.mkdir(parents=True, exist_ok=True)
        # File lock to prevent concurrent starts
        lock_path = state_root / "controller.lock"
        import fcntl
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            lock_fd.close()
            raise ValueError("Another ralph process is starting. Wait for it to finish or remove the lock.")
        try:
            with StateStore(state_root / "state.db") as store:
                resumable_run = store.get_latest_resumable_run()
                if resumable_run is not None:
                    # Check if the controller is actually alive
                    if store.is_controller_alive(resumable_run.id):
                        raise ValueError(
                            "A previous run is actively controlled. "
                            f"Use `ralph watch --run-id {resumable_run.id}` to observe it."
                        )
                    if resumable_run.status == "running":
                        # Stale controller — mark as paused so new run can start
                        store.update_run_status(resumable_run.id, "paused")
                        store.release_controller(resumable_run.id, stop_reason='{"cause": "stale", "detail": "Superseded by new run"}')
                        console.print(f"[yellow]Marked stale run {resumable_run.id} as paused.[/yellow]")
                        # Clean up any uncommitted changes left by the dead run.
                        # These will be re-attempted when the stale run is resumed.
                        _cleanup_dirty_workspace(repo_root, console)
                    else:
                        raise ValueError(
                            "A previous run is still resumable. "
                            f"Use `ralph resume --run-id {resumable_run.id}` instead of starting a new run."
                        )
                events = EventLogger(state_root / "events.jsonl")
                pe_store = ProviderEventStore(state_root)
                executor_tracker = LiveActivityTracker()
                reviewer_tracker = LiveActivityTracker()
                orchestrator = build_orchestrator(
                    repo_root=repo_root,
                    plan=validated_plan,
                    plan_path=plan_path,
                    config=loaded_config,
                    state_root=state_root,
                    store=store,
                    events=events,
                    executor_activity_tracker=executor_tracker,
                    reviewer_activity_tracker=reviewer_tracker,
                )
                run_id = generate_run_id()
                global _active_run_id
                _active_run_id = run_id
                prev_handlers = _install_signal_handlers(orchestrator)
                try:
                    outcome = run_with_progress(
                        lambda: orchestrator.start(run_id=run_id, task_limit=number_of_tasks),
                        run_id=run_id,
                        plan=validated_plan,
                        config=loaded_config,
                        store=store,
                        events=events,
                        provider_event_store=pe_store,
                        executor_activity_tracker=executor_tracker,
                        reviewer_activity_tracker=reviewer_tracker,
                        orchestrator=orchestrator,
                        state_root=str(state_root),
                    )
                finally:
                    _restore_signal_handlers(prev_handlers)
                if outcome is None:
                    raise typer.Exit(code=1)
                console.print(f"Run {outcome.run_id}: {outcome.status}")
                if outcome.status == RunStatus.PAUSED:
                    console.print(outcome.reason)
                    console.print(f"Run paused. Resume with: ralph resume --run-id {outcome.run_id}")
                elif outcome.status in {"blocked_human", "blocked_system", "failed_exhausted"}:
                    _print_operator_guidance(
                        run_id=outcome.run_id,
                        store=store,
                        events=events,
                        provider_event_store=pe_store,
                    )
        finally:
            lock_fd.close()
    except (OSError, ValueError, PlanValidationError, GitError, OrchestratorError) as exc:
        console.print(f"[red]Start failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("resume")
def resume_command(
    run_id: str = typer.Option(..., "--run-id", help="Existing run identifier."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
    number_of_tasks: int | None = typer.Option(
        None,
        "--number-of-tasks",
        min=1,
        help="Stop cleanly after this many tasks reach done during this invocation.",
    ),
    max_retries: int | None = typer.Option(
        None,
        "--max-retries",
        min=1,
        help="Increase max retries per task. Exhausted tasks continue from current attempt instead of restarting.",
    ),
) -> None:
    """Resume a previous run."""

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        loaded_config = load_config(config_path)
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"Run {run_id} does not exist.")
            # Auto-adopt stale runs
            if run.status == "running" and not store.is_controller_alive(run_id):
                console.print(f"[yellow]Controller for run {run_id} is dead/stale. Adopting...[/yellow]")
                store.release_controller(run_id, stop_reason='{"cause": "stale", "detail": "Adopted by resume"}')
            plan_path = Path(run.plan_path)
            if not plan_path.is_absolute():
                plan_path = repo_root / plan_path
            plan = load_plan(plan_path)
            validate_plan(plan, base_dir=repo_root)
            git_manager = GitManager(
                repo_root=repo_root,
                branch_prefix=loaded_config.ralph.branch_prefix,
                protected_branches=loaded_config.git.protected_branches,
                force_push=loaded_config.git.force_push,
            )
            decision = ResumeManager(
                store=store,
                git_manager=git_manager,
                events=EventLogger(state_root / "events.jsonl"),
            ).resume(run_id, plan)
            if decision.action is ResumeAction.BLOCKED_HUMAN:
                console.print(f"[yellow]Resume blocked:[/yellow] {decision.reason}")
                raise typer.Exit(code=1)
            if decision.action is ResumeAction.COMPLETE:
                console.print(decision.reason)
                return
            events = EventLogger(state_root / "events.jsonl")
            pe_store = ProviderEventStore(state_root)
            executor_tracker = LiveActivityTracker()
            reviewer_tracker = LiveActivityTracker()
            orchestrator = build_orchestrator(
                repo_root=repo_root,
                plan=plan,
                plan_path=plan_path,
                config=loaded_config,
                state_root=state_root,
                store=store,
                events=events,
                executor_activity_tracker=executor_tracker,
                reviewer_activity_tracker=reviewer_tracker,
            )
            global _active_run_id
            _active_run_id = run_id
            prev_handlers = _install_signal_handlers(orchestrator)
            try:
                outcome = run_with_progress(
                    lambda: orchestrator.resume(run_id=run_id, task_limit=number_of_tasks, max_retries=max_retries),
                    run_id=run_id,
                    plan=plan,
                    config=loaded_config,
                    store=store,
                    events=events,
                    provider_event_store=pe_store,
                    executor_activity_tracker=executor_tracker,
                    reviewer_activity_tracker=reviewer_tracker,
                    orchestrator=orchestrator,
                    state_root=str(state_root),
                )
            finally:
                _restore_signal_handlers(prev_handlers)
            if outcome is None:
                raise typer.Exit(code=1)
            console.print(f"Run {outcome.run_id}: {outcome.status}")
            if outcome.status == RunStatus.PAUSED:
                console.print(outcome.reason)
                console.print(f"Run paused. Resume with: ralph resume --run-id {outcome.run_id}")
            elif outcome.status in {"blocked_human", "blocked_system", "failed_exhausted"}:
                _print_operator_guidance(
                    run_id=outcome.run_id,
                    store=store,
                    events=events,
                    provider_event_store=pe_store,
                )
    except typer.Exit:
        raise
    except (OSError, ValueError, PlanValidationError, GitError, OrchestratorError) as exc:
        console.print(f"[red]Resume failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("status")
def status_command(
    run_id: str = typer.Option(..., "--run-id", help="Existing run identifier."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
) -> None:
    """Show current run status as a dashboard snapshot."""
    import json as _json

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        loaded_config = load_config(config_path)
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"Run {run_id} does not exist.")

            if json_output:
                task_states = store.list_task_states(run_id)
                summary = store.get_run_summary(run_id)
                controller_alive = store.is_controller_alive(run_id)
                recovery_attempts = store.list_recovery_attempts(run_id)
                # Get controller details
                ctrl_row = store.connection.execute(
                    "SELECT controller_pid, last_heartbeat_at, phase_checkpoint FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                heartbeat_age = None
                if ctrl_row and ctrl_row["last_heartbeat_at"]:
                    from datetime import datetime, UTC
                    try:
                        last = datetime.fromisoformat(ctrl_row["last_heartbeat_at"])
                        heartbeat_age = round((datetime.now(UTC) - last).total_seconds(), 1)
                    except (ValueError, TypeError):
                        pass

                # Find current task
                current_task_id = None
                current_phase = None
                current_failure = None
                for tid, ts in task_states.items():
                    if ts.state in ("in_progress", "review", "repair"):
                        current_task_id = tid
                        current_phase = ts.state
                        current_failure = ts.failure_class
                        break
                    if ts.state in ("blocked_system", "blocked_human") and current_task_id is None:
                        current_task_id = tid
                        current_phase = ts.state
                        current_failure = ts.failure_class

                output = {
                    "run_id": run_id,
                    "status": run.status,
                    "controller_pid": ctrl_row["controller_pid"] if ctrl_row else None,
                    "controller_alive": controller_alive,
                    "last_heartbeat_age_seconds": heartbeat_age,
                    "phase_checkpoint": ctrl_row["phase_checkpoint"] if ctrl_row else None,
                    "tasks": {
                        tid: {
                            "state": ts.state,
                            "attempt_count": ts.attempt_count,
                            "failure_class": ts.failure_class,
                        }
                        for tid, ts in task_states.items()
                    },
                    "current_task": current_task_id,
                    "phase": current_phase,
                    "failure_class": current_failure,
                    "cost": {
                        "input_tokens": summary["invocations"]["input_tokens"],
                        "output_tokens": summary["invocations"]["output_tokens"],
                        "duration_seconds": summary["invocations"]["duration_seconds"],
                    },
                    "recovery_attempts": [
                        {
                            "task_id": attempt.task_id,
                            "attempt_number": attempt.attempt_number,
                            "failure_class": attempt.failure_class,
                            "recovery_kind": attempt.recovery_kind,
                            "status": attempt.status,
                            "summary": attempt.summary,
                            "created_at": attempt.created_at,
                        }
                        for attempt in recovery_attempts[:10]
                    ],
                }
                console.print(_json.dumps(output, indent=2))
                return

            plan_path = Path(run.plan_path)
            if not plan_path.is_absolute():
                plan_path = repo_root / plan_path
            plan = load_plan(plan_path)
            renderer = ProgressRenderer(
                store=store,
                events=EventLogger(state_root / "events.jsonl"),
                provider_event_store=ProviderEventStore(state_root),
                config=loaded_config,
            )
            console.print(renderer.render_text(run_id, plan=plan))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Status failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("watch")
def watch_command(
    run_id: str = typer.Option(..., "--run-id", help="Run identifier to watch."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
) -> None:
    """Attach to a running ralph instance and display the live dashboard."""

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        loaded_config = load_config(config_path)
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"Run {run_id} does not exist.")
            plan_path = Path(run.plan_path)
            if not plan_path.is_absolute():
                plan_path = repo_root / plan_path
            plan = load_plan(plan_path)
            renderer = ProgressRenderer(
                store=store,
                events=EventLogger(state_root / "events.jsonl"),
                provider_event_store=ProviderEventStore(state_root),
                config=loaded_config,
            )

            terminal_states = {"completed", "blocked_human", "blocked_system", "failed_exhausted", "paused"}

            if not console.is_terminal:
                # Non-interactive: single snapshot
                console.print(renderer.render_text(run_id, plan=plan))
                return

            if watch_textual_progress is not None:
                watch_textual_progress(
                    run_id=run_id,
                    plan=plan,
                    config=loaded_config,
                    store=store,
                    events=EventLogger(state_root / "events.jsonl"),
                    provider_event_store=ProviderEventStore(state_root),
                )
                return

            with Live(console=console, refresh_per_second=2, transient=False, vertical_overflow="visible") as live:
                while True:
                    try:
                        live.update(renderer.build_renderable(
                            run_id, plan=plan, console_width=console.width,
                        ))
                    except KeyError:
                        pass
                    current_run = store.get_run(run_id)
                    if current_run is None or current_run.status in terminal_states:
                        # Final render
                        try:
                            live.update(renderer.build_renderable(
                                run_id, plan=plan, console_width=console.width,
                            ))
                        except KeyError:
                            pass
                        break
                    time.sleep(0.25)

            console.print(f"\nRun {run_id} reached terminal state: {current_run.status if current_run else 'unknown'}")

    except (OSError, ValueError) as exc:
        console.print(f"[red]Watch failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("stop")
def stop_command(
    run_id: str = typer.Option("", "--run-id", help="Run identifier to stop. If omitted, stops the latest active run."),
) -> None:
    """Request a running ralph instance to pause at the next safe checkpoint."""
    try:
        repo_root = resolve_repo_root()
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            if not run_id:
                run = store.get_latest_resumable_run()
                if run is None:
                    console.print("No active run found.")
                    return
                run_id = run.id
            else:
                run = store.get_run(run_id)
                if run is None:
                    raise ValueError(f"Run {run_id} does not exist.")

            if run.status not in ("running",):
                console.print(f"Run {run_id} is not running (status: {run.status}).")
                return

            # Write control file
            control_path = state_root / f"{run_id}.control"
            control_path.write_text("pause", encoding="utf-8")
            console.print(f"Stop requested for run {run_id}. The run will pause at the next safe checkpoint.")
            console.print(f"Resume later with: ralph resume --run-id {run_id}")
    except (OSError, ValueError) as exc:
        console.print(f"[red]Stop failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("preflight")
def preflight_command(
    plan: Path = typer.Option(Path("ralph-plan.yaml"), "--plan", "-p"),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
) -> None:
    """Run environment preflight checks."""
    from .preflight import run_preflight

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        plan_path = plan if plan.is_absolute() else repo_root / plan
        loaded_config = load_config(config_path)
        loaded_plan = None
        if plan_path.exists():
            loaded_plan = load_plan(plan_path)
        result = run_preflight(
            repo_root=repo_root,
            config=loaded_config,
            plan=loaded_plan,
        )
        console.print("[bold]Preflight Checks[/bold]")
        console.print(result.summary())
        if result.all_passed:
            console.print("\n[green]All checks passed.[/green]")
        else:
            console.print("\n[yellow]Some checks failed. These are warnings — the run may still work.[/yellow]")
    except (OSError, ValueError) as exc:
        console.print(f"[red]Preflight failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("doctor")
def doctor_command(
    run_id: str = typer.Option(..., "--run-id", help="Run identifier to diagnose."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
) -> None:
    """Diagnose a run's state and recommend next steps."""
    try:
        repo_root = resolve_repo_root()
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                console.print(f"[red]Run {run_id} does not exist.[/red]")
                raise typer.Exit(code=1)

            console.print(f"[bold]Run:[/bold] {run_id}")
            console.print(f"[bold]Status:[/bold] {run.status}")
            console.print(f"[bold]Plan:[/bold] {run.plan_path}")
            console.print(f"[bold]Created:[/bold] {run.created_at}")
            console.print(f"[bold]Updated:[/bold] {run.updated_at}")

            # Controller health
            row = store.connection.execute(
                "SELECT controller_pid, controller_started_at, last_heartbeat_at, stop_reason, phase_checkpoint FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row:
                pid = row["controller_pid"]
                heartbeat = row["last_heartbeat_at"]
                stop_reason = row["stop_reason"]
                checkpoint = row["phase_checkpoint"]
                if pid:
                    alive = store.is_controller_alive(run_id)
                    status_str = "[green]alive[/green]" if alive else "[red]dead[/red]"
                    console.print(f"[bold]Controller PID:[/bold] {pid} ({status_str})")
                else:
                    console.print("[bold]Controller:[/bold] none (no active controller)")
                if heartbeat:
                    console.print(f"[bold]Last heartbeat:[/bold] {heartbeat}")
                if stop_reason:
                    console.print(f"[bold]Stop reason:[/bold] {stop_reason}")
                if checkpoint:
                    console.print(f"[bold]Phase checkpoint:[/bold] {checkpoint}")

            # Task states
            task_states = store.list_task_states(run_id)
            console.print(f"\n[bold]Tasks ({len(task_states)}):[/bold]")
            for tid, ts in task_states.items():
                console.print(f"  {tid}: {ts.state} (attempt {ts.attempt_count})")
                if ts.failure_class:
                    console.print(f"    failure: {ts.failure_class}")
                if ts.failure_detail_json:
                    try:
                        payload = json.loads(ts.failure_detail_json)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict):
                        summary = str(payload.get("summary", "")).strip()
                        if summary:
                            console.print(f"    detail: {summary}")

            recovery_attempts = store.list_recovery_attempts(run_id)
            if recovery_attempts:
                console.print("\n[bold]Recovery attempts:[/bold]")
                for attempt in recovery_attempts[:8]:
                    console.print(
                        f"  {attempt.task_id}  {attempt.recovery_kind}  {attempt.status}  "
                        f"[{attempt.failure_class}]"
                    )
                    if attempt.summary:
                        console.print(f"    {attempt.summary}")

            # Recommendations
            console.print("\n[bold]Recommended action:[/bold]")
            if run.status == "running":
                if row and row["controller_pid"] and store.is_controller_alive(run_id):
                    console.print(f"  Run is actively controlled.")
                    console.print(f"  Observe with: ralph watch --run-id {run_id}")
                else:
                    console.print("  Controller is dead/stale.")
                    console.print(f"  Resume with: ralph resume --run-id {run_id}")
            elif run.status == "paused":
                console.print("  Run was paused.")
                console.print(f"  Resume with: ralph resume --run-id {run_id}")
            elif run.status == "blocked_human":
                console.print("  Human intervention needed. Fix the issue first.")
                console.print(f"  Resume with: ralph resume --run-id {run_id}")
            elif run.status == "blocked_system":
                console.print("  System error. Check logs and fix the environment.")
                console.print(f"  Resume with: ralph resume --run-id {run_id}")
            elif run.status == "completed":
                console.print("  Run completed successfully. No action needed.")
            elif run.status == "failed_exhausted":
                console.print("  Retry budget exhausted. Review the failures and start a new run if needed.")

    except typer.Exit:
        raise
    except (OSError, ValueError) as exc:
        console.print(f"[red]Doctor failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("incidents")
def incidents_command(
    run_id: str = typer.Option(..., "--run-id", help="Run identifier."),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
) -> None:
    """Show task failures and recovery attempts for a run."""
    import json as _json

    try:
        repo_root = resolve_repo_root()
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"Run {run_id} does not exist.")

            task_states = store.list_task_states(run_id)
            recovery_attempts = store.list_recovery_attempts(run_id)

            if json_output:
                payload = {
                    "run_id": run_id,
                    "status": run.status,
                    "task_failures": {
                        task_id: {
                            "state": record.state,
                            "failure_class": record.failure_class,
                            "failure_detail_json": record.failure_detail_json,
                            "last_error": record.last_error,
                        }
                        for task_id, record in task_states.items()
                        if record.failure_class or record.last_error
                    },
                    "recovery_attempts": [
                        {
                            "task_id": attempt.task_id,
                            "attempt_number": attempt.attempt_number,
                            "failure_class": attempt.failure_class,
                            "recovery_kind": attempt.recovery_kind,
                            "status": attempt.status,
                            "summary": attempt.summary,
                            "artifact_path": attempt.artifact_path,
                            "created_at": attempt.created_at,
                        }
                        for attempt in recovery_attempts
                    ],
                }
                console.print(_json.dumps(payload, indent=2))
                return

            console.print(f"[bold]Incidents for {run_id}[/bold]")
            has_output = False
            for task_id, record in task_states.items():
                if not record.failure_class and not record.last_error:
                    continue
                has_output = True
                console.print(f"\n[bold]{task_id}[/bold]  {record.state}")
                if record.failure_class:
                    console.print(f"  failure_class: {record.failure_class}")
                if record.failure_detail_json:
                    try:
                        detail = _json.loads(record.failure_detail_json)
                    except (TypeError, ValueError, _json.JSONDecodeError):
                        detail = None
                    if isinstance(detail, dict):
                        summary = str(detail.get("summary", "")).strip()
                        if summary:
                            console.print(f"  detail: {summary}")
                elif record.last_error:
                    console.print(f"  last_error: {record.last_error}")

                task_recoveries = store.list_recovery_attempts(run_id, task_id=task_id)
                for attempt in task_recoveries[:5]:
                    console.print(
                        f"  recovery: {attempt.recovery_kind} {attempt.status} "
                        f"[{attempt.failure_class}]"
                    )
                    if attempt.summary:
                        console.print(f"    {attempt.summary}")

            if not has_output:
                console.print("No incidents recorded for this run.")
    except (OSError, ValueError) as exc:
        console.print(f"[red]Incidents failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def main() -> None:
    app()
