from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

from ralph_ng.adapters.base import ExecutorResult, InvocationStatus
from ralph_ng.config import RalphConfig, RolesConfig, RoleConfig
from ralph_ng.events import EventLogger
from ralph_ng.git_manager import GitManager
from ralph_ng.orchestrator import Orchestrator, RunStatus
from ralph_ng.plan import Plan
from ralph_ng.prompt_builder import PromptBuilder
from ralph_ng.review_bundle import ReviewBundleBuilder
from ralph_ng.review_findings import ReviewResult, ReviewFindings, ReviewFinding, FindingSeverity, FindingCategory
from ralph_ng.state import StateStore
from ralph_ng.verification import VerificationRunner
from ralph_ng.utils import parse_human_intervention_payload


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
    git(repo, "add", "work.txt")
    git(repo, "commit", "-m", "Initial commit")
    return repo


def make_plan(task_count: int = 3) -> Plan:
    tasks: list[dict[str, object]] = []
    previous_task_id: str | None = None
    for index in range(1, task_count + 1):
        task_id = f"TASK-{index:03d}"
        dependencies = [] if previous_task_id is None else [previous_task_id]
        tasks.append(
            {
                "id": task_id,
                "title": f"Task {index}",
                "description": f"Append marker for {task_id}",
                "depends_on": dependencies,
                "acceptance_criteria": [f"work.txt includes {task_id}"],
                "verification_commands": [
                    f"{sys.executable} -c \"from pathlib import Path; "
                    f"text=Path('work.txt').read_text(); assert '{task_id}' in text\""
                ],
                "expected_files": ["work.txt"],
            }
        )
        previous_task_id = task_id
    return Plan.model_validate({"tasks": tasks})


class FakeExecutor:
    def __init__(self, repo_root: Path, behaviors: dict[str, list[callable]]) -> None:
        self.repo_root = repo_root
        self.behaviors = behaviors
        self.call_counts: dict[str, int] = {}

    def execute(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | Path | None = None,
    ) -> ExecutorResult:
        task_id = re.search(r"- ID: (TASK-\d+)", prompt)
        assert task_id is not None
        resolved_task_id = task_id.group(1)
        attempt_number = self.call_counts.get(resolved_task_id, 0) + 1
        self.call_counts[resolved_task_id] = attempt_number
        behavior = self.behaviors[resolved_task_id][attempt_number - 1]
        return behavior(self.repo_root, attempt_number)


class FakeReviewer:
    def __init__(self, verdicts: dict[str, list[callable]]) -> None:
        self.verdicts = verdicts
        self.call_counts: dict[str, int] = {}

    def review(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | None = None,
    ) -> ReviewResult:
        # Extract task ID from prompt
        match = re.search(r"TASK-\d+", prompt)
        task_id = match.group(0) if match else "TASK-001"
        attempt_number = self.call_counts.get(task_id, 0) + 1
        self.call_counts[task_id] = attempt_number
        behavior = self.verdicts[task_id][attempt_number - 1]
        return behavior(attempt_number)


class DriftAfterFirstCommitGitManager(GitManager):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.commit_count = 0

    def commit_task(self, task_id: str, title: str) -> str:
        sha = super().commit_task(task_id, title)
        self.commit_count += 1
        if self.commit_count == 1:
            (self.repo_root / "drift.txt").write_text("unexpected drift\n", encoding="utf-8")
        return sha


def make_orchestrator(
    tmp_path: Path,
    plan: Plan,
    executor: FakeExecutor,
    reviewer: FakeReviewer,
    *,
    git_manager: GitManager | None = None,
    max_retries: int = 3,
) -> tuple[Path, StateStore, EventLogger, Orchestrator]:
    repo = executor.repo_root
    state_root = tmp_path / "state-root"
    config = RalphConfig(
        roles=RolesConfig(
            executor=RoleConfig(provider="claude", model="sonnet"),
            reviewer=RoleConfig(provider="codex", model="gpt-5.3-codex", cli_path="codex"),
        ),
    )
    config.ralph.max_retries_per_task = max_retries
    store = StateStore(state_root / "state.db")
    events = EventLogger(state_root / "events.jsonl")
    orchestrator = Orchestrator(
        repo_root=repo,
        plan=plan,
        plan_path=repo / "ralph-plan.yaml",
        config=config,
        state_root=state_root,
        store=store,
        events=events,
        git_manager=git_manager
        or GitManager(
            repo_root=repo,
            branch_prefix="ralph/",
            protected_branches=["main", "master"],
            force_push=False,
        ),
        prompt_builder=PromptBuilder(repo_root=repo, spec_paths=[]),
        executor=executor,
        reviewer=reviewer,
        verification_runner=VerificationRunner(timeout_seconds=5, cwd=repo),
        review_bundle_builder=ReviewBundleBuilder(repo_root=repo),
    )
    return repo, store, events, orchestrator


def executor_write_marker(task_id: str) -> callable:
    def _behavior(repo_root: Path, attempt_number: int) -> ExecutorResult:
        with (repo_root / "work.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{task_id}\n")
        return ExecutorResult(
            status=InvocationStatus.COMPLETED,
            output_text=f"applied {task_id}",
            input_tokens=10,
            output_tokens=5,
            duration_seconds=0.01,
            exit_status=0,
        )

    return _behavior


def executor_noop(repo_root: Path, attempt_number: int) -> ExecutorResult:
    return ExecutorResult(
        status=InvocationStatus.COMPLETED,
        output_text="noop",
        input_tokens=1,
        output_tokens=1,
        duration_seconds=0.01,
        exit_status=0,
    )


def executor_retry_marker(repo_root: Path, attempt_number: int) -> ExecutorResult:
    with (repo_root / "work.txt").open("a", encoding="utf-8") as handle:
        handle.write("TASK-001\n")
        if attempt_number >= 2:
            handle.write("retry-marker\n")
    return ExecutorResult(
        status=InvocationStatus.COMPLETED,
        output_text="retry applied",
        input_tokens=10,
        output_tokens=5,
        duration_seconds=0.01,
        exit_status=0,
    )


def accept_verdict(summary: str = "ok") -> callable:
    def _behavior(attempt_number: int) -> ReviewResult:
        return ReviewResult(
            status=InvocationStatus.COMPLETED,
            findings=ReviewFindings(
                decision="accept",
                confidence=0.95,
                spec_compliance="pass",
                summary=summary,
                findings=[],
            ),
            input_tokens=7,
            output_tokens=4,
            duration_seconds=0.01,
        )
    return _behavior


def reject_with_overlay(summary: str = "needs more work") -> callable:
    def _behavior(attempt_number: int) -> ReviewResult:
        return ReviewResult(
            status=InvocationStatus.COMPLETED,
            findings=ReviewFindings(
                decision="reject",
                confidence=0.92,
                spec_compliance="fail",
                summary=summary,
                findings=[
                    ReviewFinding(
                        id="F1",
                        severity=FindingSeverity.IMPORTANT,
                        category=FindingCategory.CORRECTNESS,
                        description="Add the retry marker.",
                        evidence="work.txt missing retry-marker",
                    ),
                ],
                repair_notes="Add the retry marker.",
                new_verification_commands=[
                    f"{sys.executable} -c \"from pathlib import Path; "
                    "text=Path('work.txt').read_text(); assert 'retry-marker' in text\""
                ],
            ),
            input_tokens=7,
            output_tokens=4,
            duration_seconds=0.01,
        )
    return _behavior


def test_happy_path_executes_three_task_plan_to_completion(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    plan = make_plan(3)
    repo.joinpath("ralph-plan.yaml").write_text(plan.model_dump_json(), encoding="utf-8")
    git(repo, "add", "ralph-plan.yaml")
    git(repo, "commit", "-m", "Add plan")
    executor = FakeExecutor(
        repo,
        {
            "TASK-001": [executor_write_marker("TASK-001")],
            "TASK-002": [executor_write_marker("TASK-002")],
            "TASK-003": [executor_write_marker("TASK-003")],
        },
    )
    reviewer = FakeReviewer(
        {
            "TASK-001": [accept_verdict()],
            "TASK-002": [accept_verdict()],
            "TASK-003": [accept_verdict()],
        }
    )
    _, store, events, orchestrator = make_orchestrator(tmp_path, plan, executor, reviewer)

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.COMPLETED
    assert store.get_run("run-001").status == "completed"
    assert store.get_task_state("run-001", "TASK-003").state == "done"
    assert "TASK-003" in (repo / "work.txt").read_text(encoding="utf-8")
    assert any(event.event_type == "run.stopped" for event in events.read_events())


def test_retry_flow_rejects_once_then_accepts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    plan = make_plan(1)
    executor = FakeExecutor(repo, {"TASK-001": [executor_retry_marker, executor_retry_marker]})
    reviewer = FakeReviewer({"TASK-001": [reject_with_overlay(), accept_verdict("fixed")]})
    _, store, _, orchestrator = make_orchestrator(tmp_path, plan, executor, reviewer)

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.COMPLETED
    assert store.get_task_state("run-001", "TASK-001").attempt_count == 2
    overlay_path = tmp_path / "state-root" / "runs" / "run-001" / "overlays" / "TASK-001.yaml"
    assert overlay_path.exists()
    assert "retry-marker" in (repo / "work.txt").read_text(encoding="utf-8")


def test_accepts_clean_tree_without_creating_new_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    # Make base repo already satisfy the task verification.
    (repo / "work.txt").write_text("base\nTASK-001\n", encoding="utf-8")
    git(repo, "add", "work.txt")
    git(repo, "commit", "-m", "Pre-existing task result")

    plan = make_plan(1)
    executor = FakeExecutor(repo, {"TASK-001": [executor_noop]})
    reviewer = FakeReviewer({"TASK-001": [accept_verdict("already satisfied")]})
    _, store, events, orchestrator = make_orchestrator(tmp_path, plan, executor, reviewer)

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.COMPLETED
    assert store.get_task_state("run-001", "TASK-001").state == "done"
    accepted = [event for event in events.read_events() if event.event_type == "task.accepted"]
    assert accepted


def test_failed_exhausted_after_retry_budget(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    plan = make_plan(1)
    executor = FakeExecutor(
        repo,
        {"TASK-001": [executor_write_marker("TASK-001")] * 3},
    )
    reviewer = FakeReviewer(
        {"TASK-001": [reject_with_overlay(), reject_with_overlay(), reject_with_overlay()]}
    )
    _, store, _, orchestrator = make_orchestrator(
        tmp_path,
        plan,
        executor,
        reviewer,
        max_retries=3,
    )

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.BLOCKED_HUMAN
    # With adversarial review, 3 consecutive high-confidence rejections → escalation


def test_blocked_system_on_rate_limit_pauses_cleanly(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    plan = make_plan(1)

    def rate_limited(repo_root: Path, attempt_number: int) -> ExecutorResult:
        return ExecutorResult(
            status=InvocationStatus.RATE_LIMITED,
            output_text="",
            input_tokens=0,
            output_tokens=0,
            duration_seconds=0.01,
            exit_status=1,
            error_message="Rate limit exceeded",
        )

    executor = FakeExecutor(repo, {"TASK-001": [rate_limited]})
    reviewer = FakeReviewer({"TASK-001": [accept_verdict()]})
    _, store, _, orchestrator = make_orchestrator(tmp_path, plan, executor, reviewer)

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.BLOCKED_SYSTEM
    assert store.get_task_state("run-001", "TASK-001").state == "blocked_system"


def test_blocked_human_on_workspace_drift_pauses_cleanly(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    plan = make_plan(2)
    executor = FakeExecutor(
        repo,
        {
            "TASK-001": [executor_write_marker("TASK-001")],
            "TASK-002": [executor_write_marker("TASK-002")],
        },
    )
    reviewer = FakeReviewer({"TASK-001": [accept_verdict()], "TASK-002": [accept_verdict()]})
    drift_git_manager = DriftAfterFirstCommitGitManager(
        repo_root=repo,
        branch_prefix="ralph/",
        protected_branches=["main", "master"],
        force_push=False,
    )
    _, store, _, orchestrator = make_orchestrator(
        tmp_path,
        plan,
        executor,
        reviewer,
        git_manager=drift_git_manager,
    )

    outcome = orchestrator.start(run_id="run-001")

    assert outcome.status is RunStatus.BLOCKED_HUMAN
    assert store.get_task_state("run-001", "TASK-002").state == "blocked_human"
    payload = parse_human_intervention_payload(store.get_task_state("run-001", "TASK-002").last_error or "")
    assert payload is not None
    assert "working tree is not clean" in payload["what_failed"].lower()
