from pathlib import Path

from ralph_ng.events import EventLogger
from ralph_ng.plan import Plan
from ralph_ng.progress import ProgressRenderer
from ralph_ng.state import StateStore


def make_plan() -> Plan:
    return Plan.model_validate(
        {
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "First task",
                    "description": "Do the first thing",
                    "depends_on": [],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest tests/test_first.py -x"],
                    "expected_files": ["src/first.py"],
                },
                {
                    "id": "TASK-002",
                    "title": "Second task",
                    "description": "Do the next thing",
                    "depends_on": ["TASK-001"],
                    "acceptance_criteria": ["done"],
                    "verification_commands": ["pytest tests/test_second.py -x"],
                    "expected_files": ["src/second.py"],
                },
            ]
        }
    )


def test_renders_current_task_state_and_attempt_count(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001", "TASK-002"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=2)
    events = EventLogger(tmp_path / "events.jsonl")
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "run-001" in output
    assert "TASK-001" in output
    assert "First task" in output
    assert "review" in output
    assert "Attempt 2" in output


def test_renders_verification_status_tokens_and_cost_estimate(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    store.record_invocation(
        "run-001",
        "TASK-001",
        role="executor",
        model="sonnet",
        input_tokens=120,
        output_tokens=30,
        duration_seconds=1.5,
        exit_status=0,
    )
    events = EventLogger(tmp_path / "events.jsonl")
    events.log(
        "verification.result",
        task_id="TASK-001",
        data={"run_id": "run-001", "command": "pytest tests/test_first.py -x", "success": True},
    )
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "pytest tests/test_first.py -x" in output
    assert "pass" in output
    assert "Tokens" in output
    assert "flat heuristic" in output
    assert "$" in output


def test_renders_blocker_reason_when_blocked(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="blocked_human", task_ids=["TASK-001"])
    store.transition_task(
        "run-001",
        "TASK-001",
        "blocked_human",
        attempt_count=1,
        last_error="Workspace drift detected.",
    )
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "blocked_human" in output
    assert "Workspace drift detected." in output


def test_renders_final_summary_on_completion(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="completed", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "done", attempt_count=1)
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "completed" in output
    assert "done: 1" in output


def test_renders_without_events_or_invocations(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Current Task: none" in output
    assert "Tokens: in=0 out=0" in output
