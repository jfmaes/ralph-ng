"""CLI entrypoints for ralph-ng."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import secrets
import subprocess
import threading

import typer
from rich.console import Console
from rich.live import Live

from .adapters.claude_evaluator import ClaudeEvaluatorAdapter
from .adapters.claude_executor import ClaudeExecutorAdapter
from .config import RalphConfig, load_config
from .events import EventLogger
from .git_manager import GitError, GitManager
from .orchestrator import Orchestrator, OrchestratorError
from .plan import load_plan
from .progress import ProgressRenderer
from .prompt_builder import PromptBuilder
from .resume import ResumeAction, ResumeManager
from .review_bundle import ReviewBundleBuilder
from .state import StateStore
from .validation import PlanValidationError, validate_plan, validate_plan_file
from .verification import VerificationRunner


app = typer.Typer(help="ralph-ng autonomous coding orchestrator")
console = Console()


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


def build_executor_adapter(config: RalphConfig) -> ClaudeExecutorAdapter:
    return ClaudeExecutorAdapter(model=config.models.executor)


def build_evaluator_adapter(config: RalphConfig) -> ClaudeEvaluatorAdapter:
    return ClaudeEvaluatorAdapter(model=config.models.evaluator)


def build_orchestrator(
    *,
    repo_root: Path,
    plan,
    plan_path: Path,
    config: RalphConfig,
    state_root: Path,
    store: StateStore,
    events: EventLogger,
) -> Orchestrator:
    specs_dir = repo_root / "specs"
    spec_paths = sorted(specs_dir.glob("*.md")) if specs_dir.exists() else []
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
        executor=build_executor_adapter(config),
        evaluator=build_evaluator_adapter(config),
        verification_runner=VerificationRunner(
            timeout_seconds=config.timeouts.verification_command_seconds,
            cwd=repo_root,
        ),
        review_bundle_builder=ReviewBundleBuilder(repo_root=repo_root),
    )


def run_with_progress(orchestrator_call, *, run_id: str, plan, store: StateStore, events: EventLogger):
    renderer = ProgressRenderer(store=store, events=events)
    if not console.is_terminal:
        return orchestrator_call()

    outcome_holder: dict[str, object] = {}
    error_holder: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            outcome_holder["outcome"] = orchestrator_call()
        except BaseException as exc:  # pragma: no cover - exercised via foreground fallback
            error_holder["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()

    with Live(console=console, refresh_per_second=4, transient=False) as live:
        while worker.is_alive():
            try:
                live.update(renderer.build_renderable(run_id, plan=plan))
            except KeyError:
                pass
            worker.join(timeout=0.1)
        if "error" in error_holder:
            raise error_holder["error"]
        try:
            live.update(renderer.build_renderable(run_id, plan=plan))
        except KeyError:
            pass
    return outcome_holder["outcome"]


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
    """Validate a plan file against the v0.1 contract."""

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
) -> None:
    """Start a new run."""

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        plan_path = plan if plan.is_absolute() else repo_root / plan
        loaded_config = load_config(config_path)
        validated_plan = validate_plan_file(plan_path, base_dir=repo_root)
        state_root = resolve_state_root(repo_root)
        state_root.mkdir(parents=True, exist_ok=True)
        with StateStore(state_root / "state.db") as store:
            events = EventLogger(state_root / "events.jsonl")
            orchestrator = build_orchestrator(
                repo_root=repo_root,
                plan=validated_plan,
                plan_path=plan_path,
                config=loaded_config,
                state_root=state_root,
                store=store,
                events=events,
            )
            run_id = generate_run_id()
            outcome = run_with_progress(
                lambda: orchestrator.start(run_id=run_id),
                run_id=run_id,
                plan=validated_plan,
                store=store,
                events=events,
            )
    except (OSError, ValueError, PlanValidationError, GitError, OrchestratorError) as exc:
        console.print(f"[red]Start failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Run {outcome.run_id}: {outcome.status}")


@app.command("resume")
def resume_command(
    run_id: str = typer.Option(..., "--run-id", help="Existing run identifier."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
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
            decision = ResumeManager(store=store, git_manager=git_manager).resume(run_id, plan)
            if decision.action is ResumeAction.BLOCKED_HUMAN:
                console.print(f"[yellow]Resume blocked:[/yellow] {decision.reason}")
                raise typer.Exit(code=1)
            if decision.action is ResumeAction.COMPLETE:
                console.print(decision.reason)
                return
            events = EventLogger(state_root / "events.jsonl")
            orchestrator = build_orchestrator(
                repo_root=repo_root,
                plan=plan,
                plan_path=plan_path,
                config=loaded_config,
                state_root=state_root,
                store=store,
                events=events,
            )
            outcome = run_with_progress(
                lambda: orchestrator.resume(run_id=run_id),
                run_id=run_id,
                plan=plan,
                store=store,
                events=events,
            )
    except typer.Exit:
        raise
    except (OSError, ValueError, PlanValidationError, GitError, OrchestratorError) as exc:
        console.print(f"[red]Resume failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Run {outcome.run_id}: {outcome.status}")


@app.command("status")
def status_command(
    run_id: str = typer.Option(..., "--run-id", help="Existing run identifier."),
    config: Path = typer.Option(Path("ralph.toml"), "--config", "-c"),
) -> None:
    """Show current run status."""

    try:
        repo_root = resolve_repo_root()
        config_path = config if config.is_absolute() else repo_root / config
        load_config(config_path)
        state_root = resolve_state_root(repo_root)
        with StateStore(state_root / "state.db") as store:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"Run {run_id} does not exist.")
            plan_path = Path(run.plan_path)
            if not plan_path.is_absolute():
                plan_path = repo_root / plan_path
            plan = load_plan(plan_path)
            renderer = ProgressRenderer(store=store, events=EventLogger(state_root / "events.jsonl"))
            console.print(renderer.render_text(run_id, plan=plan))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Status failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def main() -> None:
    app()
