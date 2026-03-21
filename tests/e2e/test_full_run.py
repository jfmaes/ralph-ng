from __future__ import annotations

import re
from pathlib import Path
import shutil
import subprocess

from typer.testing import CliRunner

from ralph_ng.adapters.base import ExecutorResult, InvocationStatus
from ralph_ng.cli import app
from ralph_ng.review_findings import (
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewFindings,
    ReviewResult,
)


runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class FakeExecutor:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.call_counts: dict[str, int] = {}

    def execute(self, prompt: str, *, timeout_seconds: int | float, cwd: str | Path | None = None) -> ExecutorResult:
        task_id = prompt.split("- ID: ", 1)[1].splitlines()[0].strip()
        attempt_number = self.call_counts.get(task_id, 0) + 1
        self.call_counts[task_id] = attempt_number

        with (self.repo_root / "work.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{task_id}\n")
            if task_id == "TASK-002" and attempt_number >= 2:
                handle.write("retry-marker\n")

        return ExecutorResult(
            status=InvocationStatus.COMPLETED,
            output_text=f"updated {task_id}",
            input_tokens=10,
            output_tokens=5,
            duration_seconds=0.01,
            exit_status=0,
        )


class FakeReviewer:
    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {}

    def review(self, prompt: str, *, timeout_seconds: int | float, cwd: str | None = None) -> ReviewResult:
        match = re.search(r"TASK-\d+", prompt)
        task_id = match.group(0) if match else "TASK-001"
        attempt_number = self.call_counts.get(task_id, 0) + 1
        self.call_counts[task_id] = attempt_number

        if task_id == "TASK-002" and attempt_number == 1:
            findings = ReviewFindings(
                decision="reject",
                confidence=0.92,
                spec_compliance="fail",
                summary="Missing retry marker coverage.",
                findings=[
                    ReviewFinding(
                        id="F1",
                        severity=FindingSeverity.IMPORTANT,
                        category=FindingCategory.CORRECTNESS,
                        description="Add retry-marker to work.txt for TASK-002.",
                        evidence="work.txt missing retry-marker",
                    ),
                ],
                repair_notes="Add retry-marker to work.txt for TASK-002.",
                new_verification_commands=["grep -q 'retry-marker' work.txt"],
            )
        else:
            findings = ReviewFindings(
                decision="accept",
                confidence=0.95,
                spec_compliance="pass",
                summary="Looks good",
                findings=[],
            )

        return ReviewResult(
            status=InvocationStatus.COMPLETED,
            findings=findings,
            input_tokens=5,
            output_tokens=5,
            duration_seconds=0.01,
        )


def setup_repo(tmp_path: Path) -> Path:
    fixture_root = Path(__file__).parent / "fixtures"
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(fixture_root / "ralph.toml", repo / "ralph.toml")
    shutil.copy2(fixture_root / "ralph-plan.yaml", repo / "ralph-plan.yaml")
    shutil.copy2(fixture_root / "work.txt", repo / "work.txt")
    shutil.copytree(fixture_root / "specs", repo / "specs")
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "ralph-ng")
    git(repo, "config", "user.email", "ralph@example.com")
    git(repo, "add", "ralph.toml", "ralph-plan.yaml", "work.txt", "specs/example-spec.md")
    git(repo, "commit", "-m", "Initial fixture commit")
    return repo


def test_full_lifecycle_validate_start_complete_with_one_retry(tmp_path: Path, monkeypatch) -> None:
    repo = setup_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-e2e")
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())

    validated = runner.invoke(app, ["validate-plan", "--plan", "ralph-plan.yaml", "--base-dir", "."])
    started = runner.invoke(app, ["start"])
    status = runner.invoke(app, ["status", "--run-id", "run-e2e"])

    assert validated.exit_code == 0
    assert started.exit_code == 0
    assert status.exit_code == 0
    assert "completed" in started.stdout
    assert "COMPLETED" in status.stdout
    final_text = (repo / "work.txt").read_text(encoding="utf-8")
    assert "TASK-001" in final_text
    assert "TASK-002" in final_text
    assert "TASK-003" in final_text
    assert "retry-marker" in final_text
    log_messages = git(repo, "log", "--pretty=%s")
    assert "TASK-001: Append first marker" in log_messages
    assert "TASK-002: Append second marker" in log_messages
    assert "TASK-003: Append third marker" in log_messages
