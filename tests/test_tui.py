import sqlite3
from pathlib import Path

from ralph_ng.config import RalphConfig
from ralph_ng.events import EventLogger
from ralph_ng.plan import Plan
from ralph_ng.providers.events import ProviderEventType, make_event
from ralph_ng.providers.event_store import ProviderEventStore
from ralph_ng.state import StateStore
from ralph_ng.tui import RalphDashboardApp, run_textual_progress


def make_plan() -> Plan:
    return Plan.model_validate(
        {
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "First task",
                    "description": "Do the thing",
                    "depends_on": [],
                    "acceptance_criteria": ["done"],
                    "verification_commands": [],
                    "expected_files": ["work.txt"],
                }
            ]
        }
    )


def test_run_textual_progress_uses_dedicated_dashboard_store(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    events = EventLogger(tmp_path / "events.jsonl")
    provider_event_store = ProviderEventStore(tmp_path)
    captured: dict[str, object] = {}

    class FakeApp:
        def __init__(self, **kwargs):
            captured["store"] = kwargs["store"]
            self.error_holder = {}
            self.outcome_holder = {"outcome": "ok"}

        def run(self) -> None:
            return None

    monkeypatch.setattr("ralph_ng.tui.RalphDashboardApp", FakeApp)

    outcome = run_textual_progress(
        lambda: "worker-outcome",
        run_id="run-001",
        plan=make_plan(),
        config=RalphConfig(),
        store=store,
        events=events,
        provider_event_store=provider_event_store,
    )

    dashboard_store = captured["store"]
    assert isinstance(dashboard_store, StateStore)
    assert dashboard_store is not store
    assert dashboard_store.db_path == store.db_path
    assert outcome == "ok"

    try:
        dashboard_store.connection.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("Expected dashboard store connection to be closed")


def test_run_textual_progress_marks_run_paused_on_crash(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    store.acquire_controller("run-001", 12345)
    events = EventLogger(tmp_path / "events.jsonl")
    provider_event_store = ProviderEventStore(tmp_path)

    class FakeApp:
        def __init__(self, **kwargs):
            self.error_holder = {"error": RuntimeError("boom")}
            self.outcome_holder = {}

        def run(self) -> None:
            return None

    monkeypatch.setattr("ralph_ng.tui.RalphDashboardApp", FakeApp)

    try:
        run_textual_progress(
            lambda: "worker-outcome",
            run_id="run-001",
            plan=make_plan(),
            config=RalphConfig(),
            store=store,
            events=events,
            provider_event_store=provider_event_store,
        )
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("Expected run_textual_progress to re-raise the dashboard error")

    run = store.get_run("run-001")
    assert run is not None
    assert run.status == "paused"
    row = store.connection.execute(
        "SELECT controller_pid, stop_reason FROM runs WHERE id = ?",
        ("run-001",),
    ).fetchone()
    assert row is not None
    assert row["controller_pid"] is None
    assert "unhandled_exception" in row["stop_reason"]


def test_collect_role_events_keeps_progress_and_lifecycle_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    app = RalphDashboardApp(
        run_id="run-001",
        plan=make_plan(),
        config=RalphConfig(),
        store=store,
        events=EventLogger(tmp_path / "events.jsonl"),
        provider_event_store=ProviderEventStore(tmp_path),
        orchestrator_call=None,
        auto_exit_on_terminal=False,
    )
    provider_events = [
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_STARTED),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tokens 1 in / 8 out"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tokens 1 in / 21 out"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_COMMENTARY, payload={"text": "Checking acceptance criteria"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tool result received"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="codex", role="reviewer", review_round=1, event_type=ProviderEventType.AGENT_FINISHED, payload={"status": "completed"}),
    ]

    role_events = app._collect_role_events(provider_events, "reviewer")
    texts = [app._event_display_text(event) for event in role_events]

    assert texts == [
        "session started",
        "tokens 1 in / 21 out",
        "Checking acceptance criteria",
        "tool result received",
        "session completed",
    ]


def test_render_role_events_formats_started_finished_and_progress(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    app = RalphDashboardApp(
        run_id="run-001",
        plan=make_plan(),
        config=RalphConfig(),
        store=store,
        events=EventLogger(tmp_path / "events.jsonl"),
        provider_event_store=ProviderEventStore(tmp_path),
        orchestrator_call=None,
        auto_exit_on_terminal=False,
    )
    provider_events = [
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_STARTED),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "session initialized"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_FINISHED, payload={"status": "completed"}),
    ]

    rendered = [item.plain for item in app._render_role_events(provider_events)]

    assert rendered == [
        "  session started",
        "  session initialized",
        "  session completed",
    ]


def test_select_events_for_log_display_preserves_recent_thoughts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    app = RalphDashboardApp(
        run_id="run-001",
        plan=make_plan(),
        config=RalphConfig(),
        store=store,
        events=EventLogger(tmp_path / "events.jsonl"),
        provider_event_store=ProviderEventStore(tmp_path),
        orchestrator_call=None,
        auto_exit_on_terminal=False,
    )
    provider_events = [
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_THOUGHT_STARTED, payload={"text": "First thought", "block_id": "t1", "block_state": "start"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tokens 1 in / 8 out"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tool result received"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_PROGRESS, payload={"text": "tokens 1 in / 21 out"}),
        make_event(run_id="run-001", task_id="TASK-001", attempt_number=1, provider="claude", role="executor", review_round=0, event_type=ProviderEventType.AGENT_THOUGHT_STARTED, payload={"text": "Second thought", "block_id": "t2", "block_state": "start"}),
    ]

    selected = app._select_events_for_log_display(provider_events, limit=3)
    texts = [app._event_display_text(event) for event in selected]

    assert "First thought" in texts
    assert "Second thought" in texts
    assert "tokens 1 in / 21 out" in texts or "tool result received" in texts



def test_rewrite_reviewer_log_labels_first_pass_verdict_when_challenge_pending(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create_run("ralph-plan.yaml", run_id="run-001", status="running", task_ids=["TASK-001"])
    app = RalphDashboardApp(
        run_id="run-001",
        plan=make_plan(),
        config=RalphConfig(),
        store=store,
        events=EventLogger(tmp_path / "events.jsonl"),
        provider_event_store=ProviderEventStore(tmp_path),
        orchestrator_call=None,
        auto_exit_on_terminal=False,
    )

    class DummyLog:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def clear(self) -> None:
            self.lines.clear()

        def write(self, item) -> None:
            self.lines.append(item.plain if hasattr(item, "plain") else str(item))

    log = DummyLog()
    app._rewrite_reviewer_log(
        log,
        [],
        "accept (0.77)",
        ["F1 [minor] (testing): Behavioral verification is insufficient."],
        True,
    )

    assert any("First-Pass Verdict: accept (0.77)" in line for line in log.lines)
    assert any("Challenge pending" in line for line in log.lines)
