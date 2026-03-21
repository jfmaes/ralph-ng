from pathlib import Path

from ralph_ng.state import StateStore


def test_state_store_persists_runs_tasks_attempts_and_invocations(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    store = StateStore(db_path)
    store.create_run("ralph-plan.yaml", run_id="run-001", task_ids=["TASK-001", "TASK-002"])
    store.transition_task("run-001", "TASK-001", "ready")
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=1)
    attempt = store.record_attempt(
        "run-001",
        "TASK-001",
        1,
        patch_path="patches/TASK-001_attempt_1.patch",
    )
    invocation = store.record_invocation(
        "run-001",
        "TASK-001",
        role="executor",
        model="sonnet",
        input_tokens=123,
        output_tokens=456,
        duration_seconds=3.2,
        exit_status=0,
    )
    store.close()

    reopened = StateStore(db_path)
    run = reopened.get_run("run-001")
    task_state = reopened.get_task_state("run-001", "TASK-001")
    summary = reopened.get_run_summary("run-001")

    assert run is not None
    assert run.plan_path == "ralph-plan.yaml"
    assert task_state is not None
    assert task_state.state == "in_progress"
    assert task_state.attempt_count == 1
    assert attempt.patch_path == "patches/TASK-001_attempt_1.patch"
    assert invocation.input_tokens == 123
    assert summary["attempt_count"] == 1
    assert summary["invocations"]["input_tokens"] == 123
    assert summary["invocations"]["output_tokens"] == 456


def test_transition_task_upserts_missing_task_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001")

    record = store.transition_task("run-001", "TASK-999", "blocked_system", last_error="rate limit")

    assert record.state == "blocked_system"
    assert record.last_error == "rate limit"


def test_state_store_supports_context_manager(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    with StateStore(db_path) as store:
        store.create_run("ralph-plan.yaml", run_id="run-ctx")

    reopened = StateStore(db_path)
    assert reopened.get_run("run-ctx") is not None


def test_touch_run_updates_timestamp(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    created = store.create_run("ralph-plan.yaml", run_id="run-001")

    touched = store.touch_run("run-001")

    assert touched.updated_at >= created.updated_at
