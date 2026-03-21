"""Git workspace management for ralph-ng."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


class GitError(RuntimeError):
    """Raised when a git operation cannot be completed safely."""


class GitManager:
    """Manage the repository state used by a run."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        branch_prefix: str,
        protected_branches: list[str],
        force_push: bool,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.branch_prefix = branch_prefix
        self.protected_branches = set(protected_branches)
        self.force_push = force_push

    def ensure_managed_branch(self, *, plan_name: str, run_id: str) -> str:
        branch_name = self._managed_branch_name(plan_name, run_id)
        if branch_name in self.protected_branches:
            raise GitError(f"Refusing to create managed branch on protected branch {branch_name}")

        if self._branch_exists(branch_name):
            self._run_git("checkout", branch_name)
        else:
            self._run_git("checkout", "-b", branch_name)
        return branch_name

    def current_branch(self) -> str:
        return self._run_git("rev-parse", "--abbrev-ref", "HEAD")

    def current_commit(self) -> str:
        return self._run_git("rev-parse", "HEAD")

    def git_dir(self) -> Path:
        return self.repo_root / self._run_git("rev-parse", "--git-dir")

    def is_clean(self) -> bool:
        return self._git_status_porcelain() == ""

    def snapshot_patch(self, task_id: str, attempt_number: int, output_dir: str | Path) -> Path:
        patch_dir = Path(output_dir)
        patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patch_dir / f"{task_id}_attempt_{attempt_number}.patch"
        patch_path.write_text(self._build_patch_snapshot(), encoding="utf-8")
        return patch_path

    def detect_drift(self, expected_patch_path: str | Path | None) -> bool:
        if self.is_clean():
            return False
        if expected_patch_path is None:
            return True

        path = Path(expected_patch_path)
        if not path.exists():
            return True

        return self._build_patch_snapshot() != path.read_text(encoding="utf-8")

    def reset_to_head_clean(self) -> None:
        """Discard uncommitted changes and untracked files, returning to HEAD."""
        self._run_git("reset", "--hard", "HEAD")
        completed = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(
                f"git clean -fd failed with exit code {completed.returncode}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )

    def commit_task(self, task_id: str, title: str) -> str:
        self._run_git("add", "-A")
        self._run_git("commit", "-m", f"{task_id}: {title}")
        return self._run_git("rev-parse", "HEAD")

    def push_if_enabled(self, *, auto_push: bool, remote_name: str = "origin") -> bool:
        if not auto_push:
            return False
        if self.force_push:
            raise GitError("Refusing to force-push; force-push is disabled in v0.1")

        branch_name = self.current_branch()
        if branch_name in self.protected_branches:
            raise GitError(f"Refusing to push protected branch {branch_name}")

        self._run_git("push", "--set-upstream", remote_name, branch_name)
        return True

    def _managed_branch_name(self, plan_name: str, run_id: str) -> str:
        normalized_plan_name = re.sub(r"[^A-Za-z0-9._-]+", "-", plan_name.strip()).strip("-").lower()
        if not normalized_plan_name:
            normalized_plan_name = "plan"
        return f"{self.branch_prefix}{normalized_plan_name}/{run_id}"

    def _branch_exists(self, branch_name: str) -> bool:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    def _git_status_porcelain(self) -> str:
        return self._run_git("status", "--porcelain")

    def _build_patch_snapshot(self) -> str:
        tracked = self._run_git("diff", "--binary", "--no-ext-diff", "HEAD")
        untracked_patches: list[str] = []
        for relative_path in self._list_untracked_files():
            completed = subprocess.run(
                ["git", "diff", "--binary", "--no-ext-diff", "--no-index", "--", "/dev/null", relative_path],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode not in {0, 1}:
                raise GitError(
                    f"git diff for untracked file {relative_path} failed: {completed.stderr.strip()}"
                )
            untracked_patches.append(completed.stdout)

        return tracked + "".join(untracked_patches)

    def _list_untracked_files(self) -> list[str]:
        output = self._run_git("ls-files", "--others", "--exclude-standard")
        return [line for line in output.splitlines() if line]

    def _run_git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed with exit code {completed.returncode}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout.strip()
