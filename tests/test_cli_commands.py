from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

from ralph_ng.adapters.base import (
    EvaluatorResult,
    EvaluatorVerdict,
    ExecutorResult,
    InvocationStatus,
)
from ralph_ng.cli import app


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


def init_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "ralph-ng")
    git(repo, "config", "user.email", "ralph@example.com")
    (repo / "work.txt").write_text("base\n", encoding="utf-8")
    (repo / "ralph.toml").write_text(
        "\n".join(
            [
                "[ralph]",
                "branch_prefix = \"ralph/\"",
                "auto_push = false",
                "",
                "[models]",
                "executor = \"sonnet\"",
                "evaluator = \"opus\"",
                "cli = \"claude\"",
            ]
        ),
        encoding="utf-8",
    )
    git(repo, "add", "work.txt", "ralph.toml")
    git(repo, "commit", "-m", "Initial commit")
    return repo


def write_plan(repo: Path, *, content: str | None = None) -> None:
    plan_content = content or "\n".join(
        [
            "tasks:",
            "  - id: \"TASK-001\"",
            "    title: \"Update work file\"",
            "    description: \"Append a marker.\"",
            "    depends_on: []",
            "    acceptance_criteria:",
            "      - \"Marker exists.\"",
            "    verification_commands:",
            f"      - \"{sys.executable} -c \\\"from pathlib import Path; "
            "text=Path('work.txt').read_text(); assert 'TASK-001' in text\\\"\"",
            "    expected_files:",
            "      - \"work.txt\"",
        ]
    )
    (repo / "ralph-plan.yaml").write_text(plan_content, encoding="utf-8")
    git(repo, "add", "ralph-plan.yaml")
    git(repo, "commit", "-m", "Add plan")


class FakeExecutor:
    def __init__(self, repo_root: Path, *, rate_limited: bool = False) -> None:
        self.repo_root = repo_root
        self.rate_limited = rate_limited

    def execute(self, prompt: str, *, timeout_seconds: int | float, cwd: str | Path | None = None) -> ExecutorResult:
        if self.rate_limited:
            return ExecutorResult(
                status=InvocationStatus.RATE_LIMITED,
                output_text="",
                input_tokens=0,
                output_tokens=0,
                duration_seconds=0.01,
                exit_status=1,
                error_message="Rate limit exceeded",
            )
        with (self.repo_root / "work.txt").open("a", encoding="utf-8") as handle:
            handle.write("TASK-001\n")
        return ExecutorResult(
            status=InvocationStatus.COMPLETED,
            output_text="ok",
            input_tokens=10,
            output_tokens=5,
            duration_seconds=0.01,
            exit_status=0,
        )


class FakeEvaluator:
    def evaluate(self, review_bundle, *, timeout_seconds: int | float, cwd: str | Path | None = None) -> EvaluatorResult:
        return EvaluatorResult(
            status=InvocationStatus.COMPLETED,
            output_text="ok",
            input_tokens=5,
            output_tokens=5,
            duration_seconds=0.01,
            exit_status=0,
            verdict=EvaluatorVerdict(decision="accept", summary="ok"),
        )


def test_validate_plan_with_valid_and_invalid_plans(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)

    valid = runner.invoke(app, ["validate-plan", "--plan", str(repo / "ralph-plan.yaml"), "--base-dir", str(repo)])
    invalid = runner.invoke(
        app,
        ["validate-plan", "--plan", str(repo / "missing.yaml"), "--base-dir", str(repo)],
    )

    assert valid.exit_code == 0
    assert "Plan is valid" in valid.stdout
    assert invalid.exit_code == 1


def test_start_creates_run_and_begins_execution(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor_adapter", lambda config: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_evaluator_adapter", lambda config: FakeEvaluator())

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0
    assert "run-001" in result.stdout
    assert "completed" in result.stdout
    assert (repo / ".git" / "ralph" / "state.db").exists()


def test_resume_loads_existing_run(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr(
        "ralph_ng.cli.build_executor_adapter",
        lambda config: FakeExecutor(repo, rate_limited=True),
    )
    monkeypatch.setattr("ralph_ng.cli.build_evaluator_adapter", lambda config: FakeEvaluator())
    start = runner.invoke(app, ["start"])
    assert start.exit_code == 0
    monkeypatch.setattr("ralph_ng.cli.build_executor_adapter", lambda config: FakeExecutor(repo))

    result = runner.invoke(app, ["resume", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "completed" in result.stdout


def test_status_displays_run_info(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor_adapter", lambda config: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_evaluator_adapter", lambda config: FakeEvaluator())
    runner.invoke(app, ["start"])

    result = runner.invoke(app, ["status", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "run-001" in result.stdout
    assert "completed" in result.stdout


def test_start_refuses_dirty_tree(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    (repo / "work.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor_adapter", lambda config: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_evaluator_adapter", lambda config: FakeEvaluator())

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "dirty" in result.stdout.lower()


def test_start_refuses_if_plan_validation_fails(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(
        repo,
        content="\n".join(
            [
                "tasks:",
                "  - id: \"TASK-001\"",
                "    title: \"Invalid task\"",
                "    description: \"Missing verification commands.\"",
                "    depends_on: []",
                "    acceptance_criteria:",
                "      - \"Never valid.\"",
                "    verification_commands: []",
                "    expected_files:",
                "      - \"work.txt\"",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "start failed" in result.stdout.lower()
