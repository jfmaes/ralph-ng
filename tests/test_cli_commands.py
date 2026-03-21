from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

from ralph_ng.adapters.base import ExecutorResult, InvocationStatus
from ralph_ng.cli import app
from ralph_ng.preflight import PreflightCheck, PreflightResult
from ralph_ng.review_findings import ReviewResult, ReviewFindings


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
                "cli = \"claude\"",
                "",
                "[roles.executor]",
                "provider = \"claude\"",
                "transport = \"cli\"",
                "model = \"sonnet\"",
                "cli_path = \"claude\"",
                "",
                "[roles.reviewer]",
                "provider = \"codex\"",
                "transport = \"cli\"",
                "model = \"gpt-5.3-codex\"",
                "cli_path = \"codex\"",
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


class FakeReviewer:
    def review(self, prompt: str, *, timeout_seconds: int | float, cwd: str | None = None) -> ReviewResult:
        return ReviewResult(
            status=InvocationStatus.COMPLETED,
            findings=ReviewFindings(
                decision="accept",
                confidence=0.95,
                spec_compliance="pass",
                summary="ok",
                findings=[],
            ),
            input_tokens=5,
            output_tokens=5,
            duration_seconds=0.01,
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
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0
    assert "run-001" in result.stdout
    assert "completed" in result.stdout
    assert (repo / ".git" / "ralph" / "state.db").exists()


def test_start_runs_preflight_and_continues_on_warning(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())
    monkeypatch.setattr(
        "ralph_ng.preflight.run_preflight",
        lambda **kwargs: PreflightResult(checks=[
            PreflightCheck(name="Executor CLI", passed=False, message="missing"),
        ]),
    )

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0
    assert "Preflight Checks" in result.stdout
    assert "Continuing despite preflight warnings" in result.stdout
    assert "completed" in result.stdout


def test_resume_loads_existing_run(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr(
        "ralph_ng.cli.build_executor",
        lambda config, **kw: FakeExecutor(repo, rate_limited=True),
    )
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())
    start = runner.invoke(app, ["start"])
    assert start.exit_code == 0
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))

    result = runner.invoke(app, ["resume", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "completed" in result.stdout


def test_start_refuses_when_resumable_run_exists(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr(
        "ralph_ng.cli.build_executor",
        lambda config, **kw: FakeExecutor(repo, rate_limited=True),
    )
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())

    first = runner.invoke(app, ["start"])
    assert first.exit_code == 0

    second = runner.invoke(app, ["start"])

    assert second.exit_code == 1
    assert "ralph resume --run-id" in second.stdout
    assert "run-001" in second.stdout


def test_status_displays_run_info(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())
    runner.invoke(app, ["start"])

    result = runner.invoke(app, ["status", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "run-001" in result.stdout
    assert "COMPLETED" in result.stdout


def test_status_json_displays_controller_and_failure_info(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)

    state_root = repo / ".git" / "ralph"
    state_root.mkdir(parents=True, exist_ok=True)
    from ralph_ng.state import StateStore

    with StateStore(state_root / "state.db") as store:
        store.create_run("ralph-plan.yaml", run_id="run-001", status="blocked_system", task_ids=["TASK-001"])
        store.acquire_controller("run-001", os.getpid())
        store.update_phase_checkpoint("run-001", "review:TASK-001:attempt:1")
        store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1, last_error="boom")
        store.update_task_failure("run-001", "TASK-001", "provider_crash")
        store.connection.execute(
            """
            INSERT INTO recovery_attempts
            (run_id, task_id, attempt_number, failure_class, recovery_kind, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-001", "TASK-001", 1, "provider_crash", "diagnosis", "failed", "diagnosis failed", "2026-03-21T00:00:00+00:00"),
        )
        store.connection.commit()

    result = runner.invoke(app, ["status", "--run-id", "run-001", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "run-001"
    assert payload["status"] == "blocked_system"
    assert payload["controller_alive"] is True
    assert payload["phase_checkpoint"] == "review:TASK-001:attempt:1"
    assert payload["current_task"] == "TASK-001"
    assert payload["failure_class"] == "provider_crash"
    assert payload["tasks"]["TASK-001"]["failure_class"] == "provider_crash"
    assert payload["recovery_attempts"][0]["task_id"] == "TASK-001"
    assert payload["recovery_attempts"][0]["recovery_kind"] == "diagnosis"


def test_stop_command_writes_control_file_for_running_run(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)

    state_root = repo / ".git" / "ralph"
    state_root.mkdir(parents=True, exist_ok=True)
    from ralph_ng.state import StateStore

    with StateStore(state_root / "state.db") as store:
        store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])

    result = runner.invoke(app, ["stop", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "Stop requested for run run-001" in result.stdout
    assert (state_root / "run-001.control").read_text(encoding="utf-8") == "pause"


def test_doctor_reports_failure_and_recommendation(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)

    state_root = repo / ".git" / "ralph"
    state_root.mkdir(parents=True, exist_ok=True)
    from ralph_ng.state import StateStore

    with StateStore(state_root / "state.db") as store:
        store.create_run("ralph-plan.yaml", run_id="run-001", status="blocked_system", task_ids=["TASK-001"])
        store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=2, last_error="provider crashed")
        store.update_task_failure("run-001", "TASK-001", "provider_crash")
        store.update_phase_checkpoint("run-001", "review:TASK-001:attempt:2")
        store.connection.execute(
            """
            INSERT INTO recovery_attempts
            (run_id, task_id, attempt_number, failure_class, recovery_kind, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-001", "TASK-001", 2, "provider_crash", "diagnosis", "failed", "diagnosis failed", "2026-03-21T00:00:00+00:00"),
        )
        store.connection.commit()

    result = runner.invoke(app, ["doctor", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "Run:" in result.stdout
    assert "blocked_system" in result.stdout
    assert "failure: provider_crash" in result.stdout
    assert "review:TASK-001:attempt:2" in result.stdout
    assert "Recovery attempts:" in result.stdout
    assert "diagnosis" in result.stdout
    assert "ralph resume --run-id run-001" in result.stdout


def test_incidents_command_reports_failures_and_recovery_attempts(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    monkeypatch.chdir(repo)

    state_root = repo / ".git" / "ralph"
    state_root.mkdir(parents=True, exist_ok=True)
    from ralph_ng.state import StateStore

    with StateStore(state_root / "state.db") as store:
        store.create_run("ralph-plan.yaml", run_id="run-001", status="blocked_system", task_ids=["TASK-001"])
        store.transition_task("run-001", "TASK-001", "blocked_system", attempt_count=1, last_error="provider crashed")
        store.update_task_failure("run-001", "TASK-001", "provider_crash", '{"summary":"provider crashed hard"}')
        store.connection.execute(
            """
            INSERT INTO recovery_attempts
            (run_id, task_id, attempt_number, failure_class, recovery_kind, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-001", "TASK-001", 1, "provider_crash", "diagnosis", "failed", "diagnosis failed", "2026-03-21T00:00:00+00:00"),
        )
        store.connection.commit()

    result = runner.invoke(app, ["incidents", "--run-id", "run-001"])

    assert result.exit_code == 0
    assert "Incidents for run-001" in result.stdout
    assert "failure_class: provider_crash" in result.stdout
    assert "provider crashed hard" in result.stdout
    assert "recovery: diagnosis failed" in result.stdout


def test_start_refuses_dirty_tree(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    write_plan(repo)
    (repo / "work.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("ralph_ng.cli.generate_run_id", lambda: "run-001")
    monkeypatch.setattr("ralph_ng.cli.build_executor", lambda config, **kw: FakeExecutor(repo))
    monkeypatch.setattr("ralph_ng.cli.build_reviewer", lambda config, **kw: FakeReviewer())

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
