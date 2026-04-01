from pathlib import Path
from datetime import UTC, datetime, timedelta

from ralph_ng.adapters.streaming import ActivityKind, LiveActivityTracker, make_activity
from ralph_ng.events import EventLogger
from ralph_ng.plan import Plan
from ralph_ng.progress import ProgressRenderer, ProviderActivity
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


def test_renders_task_state_and_run_id(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001", "TASK-002"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=2)
    events = EventLogger(tmp_path / "events.jsonl")
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "run-001" in output
    assert "001" in output  # task ID shown (possibly truncated)
    assert "First task" in output
    assert "review" in output


def test_renders_cost_panel(tmp_path: Path) -> None:
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
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "$" in output  # cost estimate
    assert "120" in output or "in" in output  # token count


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

    assert "BLOCKED" in output
    assert "Workspace drift detected." in output
    assert "INTERVENTION" in output  # banner title


def test_renders_structured_human_intervention_details(tmp_path: Path) -> None:
    from ralph_ng.utils import make_human_intervention_payload

    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="blocked_human", task_ids=["TASK-001"])
    store.transition_task(
        "run-001",
        "TASK-001",
        "blocked_human",
        attempt_count=1,
        last_error=make_human_intervention_payload(
            summary="Adversarial review escalation for TASK-001.",
            what_failed="Reviewer rejected the task with one unresolved finding.",
            expected_human_actions=[
                "Inspect F1 and apply the suggested fix.",
                "Run `npm run build`.",
                "Run `ralph resume --run-id run-001`.",
            ],
            findings=[
                {
                    "id": "F1",
                    "severity": "critical",
                    "category": "correctness",
                    "description": "Build is broken.",
                },
            ],
        ),
    )
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Reviewer rejected the task with one unresolved finding." in output
    assert "F1 [critical] (correctness): Build is broken." in output
    assert "Do this:" in output
    assert "Inspect F1 and apply the suggested fix." in output


def test_renders_completion_status(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="completed", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "done", attempt_count=1)
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "COMPLETED" in output
    assert "done" in output


def test_renders_without_events_or_invocations(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "run-001" in output
    assert "RUNNING" in output
    assert "idle" in output  # executor/reviewer idle


def test_event_log_shows_recent_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    events = EventLogger(tmp_path / "events.jsonl")
    events.log("executor.started", task_id="TASK-001", data={"run_id": "run-001", "attempt": 1})
    events.log("executor.completed", task_id="TASK-001", data={"run_id": "run-001", "status": "completed"})
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "executor.started" in output
    assert "executor.completed" in output


def test_problem_banner_on_failed_exhausted(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="failed_exhausted", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "failed_exhausted", attempt_count=3, last_error="Retry budget exhausted.")
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "FAILED" in output
    assert "Retry budget exhausted" in output


def test_problem_banner_on_stale_running_review(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    store.acquire_controller("run-001", 999999)
    store.connection.execute(
        "UPDATE runs SET last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "run-001"),
    )
    store.connection.commit()
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Run heartbeat stale" in output
    assert "orchestrator process may have died" in output


def test_problem_banner_uses_controller_heartbeat_not_updated_at(tmp_path: Path) -> None:
    import os

    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    store.acquire_controller("run-001", os.getpid())
    store.update_heartbeat("run-001")
    store.connection.execute(
        "UPDATE runs SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "run-001"),
    )
    store.connection.commit()
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Run heartbeat stale" not in output


def test_provider_activity_defaults(tmp_path: Path) -> None:
    pa = ProviderActivity()
    assert pa.provider == ""
    assert pa.active is False
    assert pa.review_round == 0


def test_verification_events_in_log(tmp_path: Path) -> None:
    """Verification results appear in the event log section."""
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")
    events.log(
        "verification.result",
        task_id="TASK-001",
        data={"run_id": "run-001", "command": "pytest tests/test_first.py -x", "success": True},
    )
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "verification.result" in output
    assert "pass" in output


def test_activity_panel_with_tracker(tmp_path: Path) -> None:
    """Activity panel shows tracker events when tracker is present."""
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")
    events.log("executor.started", task_id="TASK-001", data={"run_id": "run-001", "attempt": 1})

    tracker = LiveActivityTracker()
    tracker.push(make_activity(ActivityKind.COMMENTARY, "Analyzing the auth module"))
    tracker.push(make_activity(ActivityKind.TOOL_EDIT, "[Edit] src/auth/oauth.ts"))  # not significant
    tracker.push(make_activity(ActivityKind.COMMENTARY, "Running tests to verify changes"))
    tracker.push(make_activity(ActivityKind.COMMENTARY, "All tests passing, moving to User model"))

    from ralph_ng.providers.event_store import ProviderEventStore
    from ralph_ng.providers.events import ProviderEventType, make_event

    pe_store = ProviderEventStore(tmp_path)
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="claude", role="executor", review_round=0,
        event_type=ProviderEventType.AGENT_STARTED,
    ))

    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=pe_store,
        executor_activity_tracker=tracker,
    )

    output = renderer.render_text("run-001", plan=make_plan())

    # Recent activity should show concrete operator-visible progress.
    assert "Analyzing the auth module" in output or "Running tests" in output
    assert "All tests passing" in output
    assert "[Edit] src/auth/oauth.ts" not in output


def test_activity_panel_falls_back_to_tool_activity(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")

    tracker = LiveActivityTracker()
    tracker.push(make_activity(ActivityKind.TOOL_READ, "[Read] src/auth/oauth.ts"))
    tracker.push(make_activity(ActivityKind.TOOL_BASH, "[Bash] npm test"))

    from ralph_ng.providers.event_store import ProviderEventStore
    from ralph_ng.providers.events import ProviderEventType, make_event

    pe_store = ProviderEventStore(tmp_path)
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="claude", role="executor", review_round=0,
        event_type=ProviderEventType.AGENT_STARTED,
    ))

    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=pe_store,
        executor_activity_tracker=tracker,
    )

    output = renderer.render_text("run-001", plan=make_plan())

    assert "[Read] src/auth/oauth.ts" in output
    assert "[Bash] npm test" in output


def test_activity_panel_groups_thought_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")

    from ralph_ng.providers.event_store import ProviderEventStore
    from ralph_ng.providers.events import ProviderEventType, make_event

    pe_store = ProviderEventStore(tmp_path)
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="codex", role="reviewer", review_round=1,
        event_type=ProviderEventType.AGENT_STARTED,
    ))
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="codex", role="reviewer", review_round=1,
        event_type=ProviderEventType.AGENT_THOUGHT_STARTED,
        payload={"text": "Inspecting spec coverage first", "block_id": "thought-1"},
    ))
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="codex", role="reviewer", review_round=1,
        event_type=ProviderEventType.AGENT_THOUGHT_DELTA,
        payload={"text": "Inspecting spec coverage first and then checking edge cases", "block_id": "thought-1"},
    ))
    pe_store.log(make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="codex", role="reviewer", review_round=1,
        event_type=ProviderEventType.AGENT_THOUGHT_FINISHED,
        payload={"text": "Now checking edge cases around missing dependencies", "block_id": "thought-2"},
    ))

    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=pe_store,
    )

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Thought 1:" in output
    assert "Inspecting spec coverage first" in output
    assert "checking edge cases" in output
    assert "Thought 2:" in output


def test_activity_panel_shows_running_timer_and_model_from_provider_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "in_progress", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")

    from ralph_ng.providers.event_store import ProviderEventStore
    from ralph_ng.providers.events import ProviderEventType, make_event

    pe_store = ProviderEventStore(tmp_path)
    started = make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="claude", role="executor", review_round=0,
        event_type=ProviderEventType.AGENT_STARTED,
        payload={"model": "opus"},
    )
    started.timestamp = (datetime.now(UTC) - timedelta(seconds=65)).isoformat()
    pe_store.log(started)

    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=pe_store,
    )

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Executor" in output
    assert "claude/opus" in output
    assert "running for" in output
    assert "01:05 / 10:00" in output


def test_focus_panel_shows_active_reviewer_timer(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")

    from ralph_ng.providers.event_store import ProviderEventStore
    from ralph_ng.providers.events import ProviderEventType, make_event

    pe_store = ProviderEventStore(tmp_path)
    started = make_event(
        run_id="run-001", task_id="TASK-001", attempt_number=1,
        provider="codex", role="reviewer", review_round=1,
        event_type=ProviderEventType.AGENT_STARTED,
        payload={"model": "gpt-5.3-codex"},
    )
    started.timestamp = (datetime.now(UTC) - timedelta(seconds=42)).isoformat()
    pe_store.log(started)

    renderer = ProgressRenderer(
        store=store,
        events=events,
        provider_event_store=pe_store,
    )

    output = renderer.render_text("run-001", plan=make_plan())

    assert "reviewer running for 00:42" in output
    assert "(codex/gpt-5.3-codex)" in output


def test_focus_panel_shows_verification_and_risk(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    events = EventLogger(tmp_path / "events.jsonl")
    events.log(
        "verification.result",
        task_id="TASK-001",
        data={"run_id": "run-001", "attempt": 1, "command": "pytest tests/test_first.py -x", "success": True},
    )
    events.log(
        "review.risk_classified",
        task_id="TASK-001",
        data={"run_id": "run-001", "attempt": 1, "risk": "low", "diff_lines": 12, "file_count": 2},
    )
    renderer = ProgressRenderer(store=store, events=events)

    output = renderer.render_text("run-001", plan=make_plan())

    assert "Current Task" in output
    assert "verification 1/1 pass" in output
    assert "reviewer-scrutiny: low (diff based)" in output
    assert "12" in output and "2 files" in output


def test_focus_panel_shows_latest_reviewer_findings(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "repair", attempt_count=1)
    attempt = store.record_attempt(
        "run-001",
        "TASK-001",
        1,
        review_path=str(tmp_path / "review.json"),
    )
    review_payload = {
        "status": "completed",
        "findings": {
            "decision": "reject",
            "confidence": 0.93,
            "spec_compliance": "fail",
            "summary": "Reviewer found real bugs",
            "findings": [
                {
                    "id": "F1",
                    "severity": "critical",
                    "category": "correctness",
                    "description": "Header callback never opens the modal.",
                },
                {
                    "id": "F2",
                    "severity": "important",
                    "category": "testing",
                    "description": "Clipboard errors are not handled.",
                },
            ],
        },
    }
    Path(attempt.review_path or "").write_text(__import__("json").dumps(review_payload), encoding="utf-8")
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "review verdict: reject (0.93)" in output
    # Detailed findings are now shown in the Reviewer Reasoning pane, not the
    # focus panel.  The focus panel shows a compact summary with finding count.
    assert "(2 findings)" in output


def test_marks_first_pass_verdict_when_challenge_is_pending(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.transition_task("run-001", "TASK-001", "review", attempt_count=1)
    store.update_phase_checkpoint("run-001", "challenge_executor:TASK-001:attempt:1")
    attempt = store.record_attempt(
        "run-001",
        "TASK-001",
        1,
        review_path=str(tmp_path / "TASK-001_attempt_1_round1.json"),
    )
    review_payload = {
        "status": "completed",
        "findings": {
            "decision": "accept",
            "confidence": 0.77,
            "spec_compliance": "pass",
            "summary": "Looks good overall",
            "findings": [
                {
                    "id": "F1",
                    "severity": "minor",
                    "category": "testing",
                    "description": "Behavioral verification is insufficient.",
                },
            ],
        },
    }
    Path(attempt.review_path or "").write_text(__import__("json").dumps(review_payload), encoding="utf-8")
    renderer = ProgressRenderer(store=store, events=EventLogger(tmp_path / "events.jsonl"))

    output = renderer.render_text("run-001", plan=make_plan())

    assert "first-pass verdict: accept (0.77)" in output
    assert "challenge pending" in output
