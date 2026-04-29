from __future__ import annotations

from pathlib import Path
import subprocess

from ralph_ng.events import EventLogger
from ralph_ng.git_manager import GitManager
from ralph_ng.plan import Plan
from ralph_ng.resume import ResumeAction, ResumeManager
from ralph_ng.state import StateStore
from ralph_ng.utils import make_human_intervention_payload


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "ralph-ng")
    git(repo, "config", "user.email", "ralph@example.com")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "Initial commit")
    return repo


def make_plan() -> Plan:
    return Plan.model_validate(
        {
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "First task",
                    "description": "Modify app.py",
                    "depends_on": [],
                    "acceptance_criteria": ["app.py is updated"],
                    "verification_commands": ["pytest -q"],
                    "expected_files": ["app.py"],
                },
                {
                    "id": "TASK-002",
                    "title": "Independent follow-up",
                    "description": "Can run after exhaustion of task one if independent.",
                    "depends_on": [],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest -q"],
                    "expected_files": ["app.py"],
                },
            ]
        }
    )


def make_serial_plan(task_count: int = 4) -> Plan:
    tasks = []
    previous: str | None = None
    for index in range(1, task_count + 1):
        task_id = f"TASK-{index:03d}"
        tasks.append(
            {
                "id": task_id,
                "title": f"Task {index}",
                "description": f"Task {index}",
                "depends_on": [] if previous is None else [previous],
                "acceptance_criteria": ["done"],
                "verification_commands": ["pytest -q"],
                "expected_files": ["app.py"],
            }
        )
        previous = task_id
    return Plan.model_validate({"tasks": tasks})


def make_managers(tmp_path: Path) -> tuple[Path, StateStore, GitManager, EventLogger, ResumeManager]:
    repo = init_repo(tmp_path)
    store = StateStore(tmp_path / "state.db")
    events = EventLogger(tmp_path / "events.jsonl")
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    resume = ResumeManager(store=store, git_manager=manager, events=events)
    return repo, store, manager, events, resume


def test_resume_from_blocked_system_continues_from_review_when_workspace_matches_patch(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    repo, store, manager, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch_path = manager.snapshot_patch("TASK-001", 1, tmp_path / "patches")
    store.record_attempt("run-001", "TASK-001", 1, patch_path=str(patch_path))
    store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "review"


def test_resume_with_clean_state_continues_from_ready(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"


def test_resume_with_diverged_dirty_state_blocks(tmp_path: Path) -> None:
    plan = make_plan()
    repo, store, manager, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch_path = manager.snapshot_patch("TASK-001", 1, tmp_path / "patches")
    store.record_attempt("run-001", "TASK-001", 1, patch_path=str(patch_path))
    store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1)
    (repo / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.BLOCKED_HUMAN
    assert decision.task_id == "TASK-001"
    assert "diverged" in decision.reason


def test_resume_with_clean_state_after_manual_fix_continues(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "blocked_human", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"


def test_resume_blocked_human_interrupted_executor_auto_resets_to_head(tmp_path: Path) -> None:
    plan = make_plan()
    repo, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    message = make_human_intervention_payload(
        summary="Interrupted executor left uncommitted changes for TASK-001.",
        what_failed="Executor was interrupted.",
        expected_human_actions=["Reset to HEAD and retry."],
        metadata={"task_id": "TASK-001", "reason": "interrupted_executor_dirty_workspace"},
    )
    store.transition_task("run-001", "TASK-001", "blocked_human", attempt_count=1, last_error=message)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"
    assert store.get_task_state("run-001", "TASK-001").attempt_count == 0
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_resume_restarts_interrupted_executor_attempt(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"
    assert store.get_task_state("run-001", "TASK-001").attempt_count == 0


def test_resume_dirty_in_progress_auto_resets(tmp_path: Path) -> None:
    """Interrupted executor with dirty workspace auto-resets to HEAD, no human needed."""
    plan = make_plan()
    repo, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=2)
    # Dirty the workspace
    (repo / "app.py").write_text("PARTIAL WORK\n", encoding="utf-8")

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"
    # Attempt count decremented (was 2, now 1 — the interrupted attempt doesn't count)
    assert store.get_task_state("run-001", "TASK-001").attempt_count == 1
    # Workspace is clean (reset to HEAD)
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_resume_blocks_on_multiple_active_tasks(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    store.transition_task("run-001", "TASK-002", "in_progress", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.BLOCKED_HUMAN
    assert "Multiple active tasks" in decision.reason


def test_resume_recovers_when_branch_rewound_to_accepted_commit(tmp_path: Path) -> None:
    plan = make_serial_plan()
    repo, store, manager, events, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])

    for task_id in ("TASK-001", "TASK-002"):
        with (repo / "app.py").open("a", encoding="utf-8") as handle:
            handle.write(f"{task_id}\n")
        git(repo, "add", "app.py")
        git(repo, "commit", "-m", f"{task_id}: committed")
        commit = git(repo, "rev-parse", "HEAD")
        store.transition_task("run-001", task_id, "done", attempt_count=1)
        events.log("task.accepted", task_id=task_id, data={"run_id": "run-001", "commit": commit})

    store.transition_task("run-001", "TASK-003", "review", attempt_count=1)
    store.transition_task("run-001", "TASK-004", "in_progress", attempt_count=1)

    accepted_commit = git(repo, "rev-list", "--max-count=1", "--grep=TASK-001: committed", "HEAD")
    git(repo, "reset", "--hard", accepted_commit)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-002"
    assert decision.resume_state == "ready"
    assert store.get_task_state("run-001", "TASK-001").state == "done"
    assert store.get_task_state("run-001", "TASK-002").state == "ready"
    assert store.get_task_state("run-001", "TASK-003").state == "pending"
    assert store.get_task_state("run-001", "TASK-004").state == "pending"


def test_resume_complete_when_all_tasks_done(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "done", attempt_count=1)
    store.transition_task("run-001", "TASK-002", "done", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.COMPLETE
    assert decision.task_id is None
    assert store.get_task_state("run-001", "TASK-001").state == "done"
    assert store.get_task_state("run-001", "TASK-002").state == "done"


def test_resume_after_failed_exhausted_continues_exhausted_task(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "failed_exhausted", attempt_count=3)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "failed_exhausted"
    record = store.get_task_state("run-001", "TASK-001")
    assert record.state == "failed_exhausted"
    assert record.attempt_count == 3
