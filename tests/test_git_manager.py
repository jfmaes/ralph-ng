from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ralph_ng.git_manager import GitError, GitManager


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


def test_creates_managed_branch_with_correct_prefix(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )

    branch = manager.ensure_managed_branch(plan_name="example-plan", run_id="run-001")

    assert branch == "ralph/example-plan/run-001"
    assert manager.current_branch() == branch


def test_detects_dirty_working_tree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )

    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert manager.is_clean() is False


def test_snapshots_patch_after_changes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    patch_path = manager.snapshot_patch("TASK-001", 1, tmp_path / "patches")

    assert patch_path.exists()
    patch = patch_path.read_text(encoding="utf-8")
    assert "diff --git" in patch
    assert "+VALUE = 2" in patch


def test_commits_with_structured_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    manager.ensure_managed_branch(plan_name="example-plan", run_id="run-001")
    (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    commit_sha = manager.commit_task("TASK-001", "Update sample app")

    assert commit_sha
    assert git(repo, "log", "-1", "--pretty=%s") == "TASK-001: Update sample app"


def test_refuses_push_from_protected_branch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )

    with pytest.raises(GitError) as exc_info:
        manager.push_if_enabled(auto_push=True)

    assert "protected branch" in str(exc_info.value)


def test_refuses_force_push(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=True,
    )

    with pytest.raises(GitError) as exc_info:
        manager.push_if_enabled(auto_push=True)

    assert "force-push" in str(exc_info.value)


def test_detects_workspace_drift_against_snapshot(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch_path = manager.snapshot_patch("TASK-001", 1, tmp_path / "patches")

    assert manager.detect_drift(patch_path) is False

    (repo / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

    assert manager.detect_drift(patch_path) is True


def test_reset_to_head_clean_discards_dirty_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("temp\n", encoding="utf-8")

    manager.reset_to_head_clean()

    assert manager.is_clean() is True
    assert (repo / "scratch.txt").exists() is False
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_pushes_to_remote_when_enabled(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    manager = GitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    branch = manager.ensure_managed_branch(plan_name="example-plan", run_id="run-001")
    (repo / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    manager.commit_task("TASK-001", "Update sample app")

    pushed = manager.push_if_enabled(auto_push=True)

    assert pushed is True
    assert git(repo, "ls-remote", "--heads", "origin", branch)
