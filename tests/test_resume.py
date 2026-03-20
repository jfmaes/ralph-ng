from __future__ import annotations

from pathlib import Path
import subprocess

from ralph_ng.git_manager import GitManager
from ralph_ng.plan import Plan
from ralph_ng.resume import ResumeAction, ResumeManager
from ralph_ng.state import StateStore


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


def make_managers(tmp_path: Path) -> tuple[Path, StateStore, GitManager, ResumeManager]:
    repo = init_repo(tmp_path)
    store = StateStore(tmp_path / "state.db")
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    resume = ResumeManager(store=store, git_manager=manager)
    return repo, store, manager, resume


def test_resume_from_blocked_system_continues_from_review_when_workspace_matches_patch(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    repo, store, manager, resume = make_managers(tmp_path)
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
    _, store, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"


def test_resume_with_diverged_dirty_state_blocks(tmp_path: Path) -> None:
    plan = make_plan()
    repo, store, manager, resume = make_managers(tmp_path)
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
    _, store, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "blocked_human", attempt_count=1)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-001"
    assert decision.resume_state == "ready"


def test_resume_after_failed_exhausted_skips_task_and_continues(tmp_path: Path) -> None:
    plan = make_plan()
    _, store, _, resume = make_managers(tmp_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=[task.id for task in plan.tasks])
    store.transition_task("run-001", "TASK-001", "failed_exhausted", attempt_count=3)

    decision = resume.resume("run-001", plan)

    assert decision.action is ResumeAction.CONTINUE
    assert decision.task_id == "TASK-002"
    assert decision.resume_state == "ready"
