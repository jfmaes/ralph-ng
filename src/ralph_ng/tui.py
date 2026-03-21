"""Textual-based live dashboard for ralph-ng."""

from __future__ import annotations

import threading
import time
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from .adapters.streaming import LiveActivityTracker
from .config import RalphConfig
from .events import EventLogger
from .plan import Plan
from .progress import ProgressRenderer
from .providers.event_store import ProviderEventStore
from .providers.events import ProviderEventType
from .state import StateStore


TERMINAL_STATES = {"completed", "blocked_human", "blocked_system", "failed_exhausted", "paused"}
RUN_HEARTBEAT_SECONDS = 5.0


class RalphDashboardApp(App[None]):
    """Textual app for live run monitoring."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #summary {
        height: auto;
        padding: 0 1;
    }

    #overview {
        height: auto;
        max-height: 50%;
    }

    #left-column {
        width: 52;
        min-width: 40;
        padding: 0 1;
    }

    #right-column {
        width: 1fr;
        padding: 0 1;
    }

    #banner {
        height: auto;
        margin-bottom: 1;
    }

    #tasks,
    #current-task,
    #cost,
    #events-title {
        height: auto;
    }

    .reasoning-title {
        height: auto;
        padding: 0 1;
    }

    RichLog {
        background: $surface;
        scrollbar-size-vertical: 1;
    }

    #executor-log,
    #reviewer-log {
        height: 1fr;
        min-height: 6;
        border: round $panel;
        margin: 0 1;
    }

    #reasoning-panes {
        height: 1fr;
    }

    #event-log {
        height: 8;
        border: round $panel;
        margin: 0 1 1 1;
    }
    """

    BINDINGS = [
        ("p", "pause_run", "Pause"),
        ("s", "stop_run", "Stop"),
        ("q", "quit_dashboard", "Quit"),
        ("ctrl+c", "quit_dashboard", "Quit"),
    ]

    def __init__(
        self,
        *,
        run_id: str,
        plan: Plan,
        config: RalphConfig,
        store: StateStore,
        events: EventLogger,
        provider_event_store: ProviderEventStore,
        executor_activity_tracker: LiveActivityTracker | None = None,
        reviewer_activity_tracker: LiveActivityTracker | None = None,
        orchestrator_call: Callable[[], object] | None = None,
        orchestrator: object | None = None,
        state_root: str | None = None,
        auto_exit_on_terminal: bool = True,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.plan = plan
        self.config = config
        self.store = store
        self.events = events
        self.provider_event_store = provider_event_store
        self.executor_activity_tracker = executor_activity_tracker
        self.reviewer_activity_tracker = reviewer_activity_tracker
        self.orchestrator_call = orchestrator_call
        self._orchestrator = orchestrator
        self._state_root = state_root
        self.auto_exit_on_terminal = auto_exit_on_terminal

        self.renderer = ProgressRenderer(
            store=store,
            events=events,
            provider_event_store=provider_event_store,
            config=config,
            executor_activity_tracker=executor_activity_tracker,
            reviewer_activity_tracker=reviewer_activity_tracker,
        )

        self.outcome_holder: dict[str, object] = {}
        self.error_holder: dict[str, BaseException] = {}
        self.worker: threading.Thread | None = None
        self.last_heartbeat = time.monotonic()
        self._activity_context: tuple[str, int] | None = None
        self._executor_seen = 0
        self._reviewer_seen = 0
        self._event_seen = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="summary")
        with Horizontal(id="overview"):
            with Vertical(id="left-column"):
                yield Static(id="banner")
                yield Static(id="tasks")
                yield Static(id="cost")
            with Vertical(id="right-column"):
                yield Static(id="current-task")
        with Vertical(id="reasoning-panes"):
            yield Static(Text("Executor Reasoning", style="bold cyan"), classes="reasoning-title")
            yield RichLog(id="executor-log", wrap=True, markup=True, auto_scroll=True)
            yield Static(Text("Reviewer Reasoning", style="bold magenta"), classes="reasoning-title")
            yield RichLog(id="reviewer-log", wrap=True, markup=True, auto_scroll=True)
        yield Static(Text("Events", style="bold dim"), id="events-title")
        yield RichLog(id="event-log", wrap=True, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        if self.orchestrator_call is not None:
            self.worker = threading.Thread(target=self._run_worker)
            self.worker.start()
        self.set_interval(0.25, self.refresh_dashboard)
        self.call_after_refresh(self.refresh_dashboard)

    def _run_worker(self) -> None:
        assert self.orchestrator_call is not None
        try:
            self.outcome_holder["outcome"] = self.orchestrator_call()
        except BaseException as exc:  # pragma: no cover - exercised in app runtime
            self.error_holder["error"] = exc

    def refresh_dashboard(self) -> None:
        run = self.store.get_run(self.run_id)
        if run is None:
            return

        width = max(self.size.width, 120)
        task_states = self.store.list_task_states(self.run_id)
        run_events = self.renderer._run_events(self.run_id)
        task_titles = {task.id: task.title for task in self.plan.tasks}
        task_order = [task.id for task in self.plan.tasks]
        current_task_id = self.renderer._current_task_id(task_states)
        current_task = None if current_task_id is None else task_states.get(current_task_id)

        self.renderer._update_provider_activity(self.run_id, current_task)

        self.query_one("#summary", Static).update(self.renderer._build_header(run))
        banner = self.renderer._build_problem_banner(self.run_id, run, task_states, current_task)
        self.query_one("#banner", Static).update(banner if banner is not None else Text(""))
        self.query_one("#tasks", Static).update(
            self.renderer._build_task_table(task_states, task_titles, task_order, current_task_id, width < 80)
        )
        self.query_one("#current-task", Static).update(
            self.renderer._build_focus_panel(self.run_id, current_task, task_titles, run_events)
        )
        self.query_one("#cost", Static).update(
            self.renderer._build_cost_panel(self.run_id, self.store.get_run_summary(self.run_id))
        )

        # Controller status and stale banner
        controller_text_parts = []
        try:
            ctrl_row = self.store.connection.execute(
                "SELECT controller_pid, last_heartbeat_at, phase_checkpoint FROM runs WHERE id = ?",
                (self.run_id,),
            ).fetchone()
            if ctrl_row:
                pid = ctrl_row["controller_pid"]
                heartbeat = ctrl_row["last_heartbeat_at"]
                checkpoint = ctrl_row["phase_checkpoint"]
                if pid:
                    controller_text_parts.append(f"Controller: PID {pid}")
                if heartbeat:
                    from datetime import datetime, UTC
                    try:
                        last = datetime.fromisoformat(heartbeat)
                        age = (datetime.now(UTC) - last).total_seconds()
                        controller_text_parts.append(f"Heartbeat: {age:.0f}s ago")
                        # Stale banner
                        if age > 30.0 and run.status == "running":
                            stale_banner = Text(
                                f"⚠ Controller (PID {pid}) has not heartbeated for {age:.0f}s — run may be orphaned.\n"
                                f"  Run: ralph resume --run-id {self.run_id}",
                                style="bold red",
                            )
                            self.query_one("#banner", Static).update(stale_banner)
                    except (ValueError, TypeError):
                        pass
                if checkpoint:
                    controller_text_parts.append(f"Phase: {checkpoint}")
        except Exception:
            pass

        # Failure/recovery panel for current task
        if current_task_id:
            try:
                fail_row = self.store.connection.execute(
                    "SELECT failure_class, failure_detail_json FROM task_states WHERE run_id = ? AND task_id = ?",
                    (self.run_id, current_task_id),
                ).fetchone()
                if fail_row and fail_row["failure_class"]:
                    import json as _json
                    detail = ""
                    try:
                        detail_data = _json.loads(fail_row["failure_detail_json"]) if fail_row["failure_detail_json"] else {}
                        detail = detail_data.get("summary", "")
                    except (ValueError, TypeError):
                        pass
                    recovery_text = Text()
                    recovery_text.append("Failure: ", style="bold red")
                    recovery_text.append(f"{fail_row['failure_class']}\n")
                    if detail:
                        recovery_text.append(f"  {detail}\n")
            except Exception:
                pass

        self._sync_activity_logs(current_task)
        self._sync_event_log(run_events)

        # Heartbeat — keep the controller alive in the database
        if self.worker is not None and self.worker.is_alive():
            now = time.monotonic()
            if now - self.last_heartbeat >= RUN_HEARTBEAT_SECONDS:
                try:
                    self.store.touch_run(self.run_id)
                except KeyError:
                    pass
                self.last_heartbeat = now

        if self.error_holder and self.worker is not None and not self.worker.is_alive():
            self.exit()
            return

        if self.auto_exit_on_terminal and run.status in TERMINAL_STATES:
            if self.worker is None or not self.worker.is_alive():
                self.exit()

    def _sync_activity_logs(self, current_task) -> None:
        executor_log = self.query_one("#executor-log", RichLog)
        reviewer_log = self.query_one("#reviewer-log", RichLog)

        if current_task is None:
            if self._activity_context is not None:
                executor_log.clear()
                reviewer_log.clear()
                self._activity_context = None
                self._executor_seen = 0
                self._reviewer_seen = 0
            return

        context = (current_task.task_id, current_task.attempt_count)
        provider_events = self.provider_event_store.read_events(
            self.run_id, current_task.task_id, current_task.attempt_count,
        )
        if context != self._activity_context:
            executor_log.clear()
            reviewer_log.clear()
            self._activity_context = context
            self._executor_seen = 0
            self._reviewer_seen = 0

        executor_events = self._collect_role_events(provider_events, "executor")
        reviewer_events = self._collect_role_events(provider_events, "reviewer")

        if len(executor_events) != self._executor_seen:
            self._rewrite_role_log(executor_log, executor_events)
            self._executor_seen = len(executor_events)
        if len(reviewer_events) != self._reviewer_seen:
            self._rewrite_role_log(reviewer_log, reviewer_events)
            self._reviewer_seen = len(reviewer_events)

        if self._executor_seen == 0 and self.renderer.executor_activity.active:
            executor_log.clear()
            executor_log.write(Text("waiting for provider output...", style="dim italic"))
            self._executor_seen = -1
        if self._reviewer_seen == 0 and self.renderer.reviewer_activity.active:
            reviewer_log.clear()
            reviewer_log.write(Text("waiting for provider output...", style="dim italic"))
            self._reviewer_seen = -1

    def _collect_role_events(
        self,
        provider_events: list,
        role: str,
    ) -> list:
        textual = [
            event
            for event in provider_events
            if event.role == role
            and event.event_type in {
                ProviderEventType.AGENT_THOUGHT_STARTED,
                ProviderEventType.AGENT_THOUGHT_DELTA,
                ProviderEventType.AGENT_THOUGHT_FINISHED,
                ProviderEventType.AGENT_COMMENTARY,
                ProviderEventType.AGENT_STRUCTURED_OUTPUT,
                ProviderEventType.AGENT_ERROR,
            }
            and str(event.payload.get("text", "")).strip()
        ]
        if not textual:
            textual = [
                event
                for event in provider_events
                if event.role == role
                and event.event_type == ProviderEventType.AGENT_PROGRESS
                and str(event.payload.get("text", "")).strip()
            ]
        return textual

    def _rewrite_role_log(self, log: RichLog, textual_events: list) -> None:
        log.clear()
        rendered_items = self._render_role_events(textual_events)
        for item in rendered_items[-8:]:
            log.write(item)

    def _render_role_events(self, textual_events: list) -> list[Text]:
        items: list[tuple[str, object]] = []
        thought_positions: dict[str, tuple[int, int]] = {}

        for event in textual_events:
            text = str(event.payload.get("text", "")).strip()
            if not text:
                continue
            if event.event_type in {
                ProviderEventType.AGENT_THOUGHT_STARTED,
                ProviderEventType.AGENT_THOUGHT_DELTA,
                ProviderEventType.AGENT_THOUGHT_FINISHED,
            }:
                block_id = str(event.payload.get("block_id", "")).strip() or f"thought-{len(thought_positions) + 1}"
                existing = thought_positions.get(block_id)
                if existing is None:
                    thought_number = len(thought_positions) + 1
                    thought_positions[block_id] = (len(items), thought_number)
                    items.append(("thought", (thought_number, text)))
                else:
                    position, thought_number = existing
                    items[position] = ("thought", (thought_number, text))
            else:
                items.append(("event", event))

        rendered: list[Text] = []
        for kind, payload in items:
            if kind == "thought":
                thought_number, text = payload
                body = "\n".join(f"  {line}" for line in str(text).splitlines()) or f"  {text}"
                rendered.append(Text.assemble(
                    (f"Thought {thought_number}", "bold cyan"),
                    "\n",
                    (body, "white"),
                ))
                continue

            event = payload
            text = str(event.payload.get("text", "")).strip()
            event_type = event.event_type
            if event_type == ProviderEventType.AGENT_ERROR:
                rendered.append(Text(f"ERROR: {text}", style="bold red"))
            elif event_type == ProviderEventType.AGENT_STRUCTURED_OUTPUT:
                rendered.append(Text(text, style="green"))
            elif event_type == ProviderEventType.AGENT_PROGRESS:
                rendered.append(Text(f"  {text}", style="dim"))
            else:
                rendered.append(Text(text))

        return rendered

    def _sync_event_log(self, run_events) -> None:
        event_log = self.query_one("#event-log", RichLog)
        if self._event_seen == 0:
            event_log.clear()
        for event in run_events[self._event_seen:]:
            task = event.task_id or "-"
            detail = self.renderer._event_detail(event)
            line = f"{self.renderer._format_time(event.timestamp)}  {event.event_type:<22} {task:<8} {detail}"
            event_log.write(line.rstrip())
        self._event_seen = len(run_events)

    def _request_orchestrator_shutdown(self) -> None:
        """Signal the orchestrator to pause, via object method or control file."""
        if self._orchestrator is not None:
            shutdown_fn = getattr(self._orchestrator, "request_shutdown", None)
            if callable(shutdown_fn):
                shutdown_fn()
                return
        # Fallback: write control file
        if self._state_root is not None:
            from pathlib import Path
            control_path = Path(self._state_root) / f"{self.run_id}.control"
            try:
                control_path.write_text("pause", encoding="utf-8")
            except OSError:
                pass

    def action_pause_run(self) -> None:
        """Pause the run at the next safe checkpoint, keep dashboard open."""
        self._request_orchestrator_shutdown()
        self.notify("Pause requested — run will pause at next checkpoint. Dashboard stays open.")

    def action_stop_run(self) -> None:
        """Stop the run and close the dashboard."""
        self._request_orchestrator_shutdown()
        self.notify("Stop requested — waiting for checkpoint, then exiting...")
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=15.0)
        self.exit()

    def action_quit_dashboard(self) -> None:
        """Quit the dashboard, cleanly stopping the orchestrator if controlling."""
        if self.worker is not None and self.worker.is_alive():
            self._request_orchestrator_shutdown()
            self.worker.join(timeout=10.0)
        self.exit()


def run_textual_progress(
    orchestrator_call,
    *,
    run_id: str,
    plan: Plan,
    config: RalphConfig,
    store: StateStore,
    events: EventLogger,
    provider_event_store: ProviderEventStore,
    executor_activity_tracker: LiveActivityTracker | None = None,
    reviewer_activity_tracker: LiveActivityTracker | None = None,
    orchestrator: object | None = None,
    state_root: str | None = None,
):
    app = RalphDashboardApp(
        run_id=run_id,
        plan=plan,
        config=config,
        store=store,
        events=events,
        provider_event_store=provider_event_store,
        executor_activity_tracker=executor_activity_tracker,
        reviewer_activity_tracker=reviewer_activity_tracker,
        orchestrator_call=orchestrator_call,
        orchestrator=orchestrator,
        state_root=state_root,
        auto_exit_on_terminal=True,
    )
    app.run()
    if app.error_holder:
        raise next(iter(app.error_holder.values()))
    if orchestrator_call is not None and "outcome" not in app.outcome_holder:
        # Dashboard was quit by operator — run was paused, not an error
        run = store.get_run(run_id)
        if run is not None and run.status == "paused":
            return app.outcome_holder.get("outcome")
        raise RuntimeError("Textual dashboard exited before the run produced an outcome.")
    return app.outcome_holder.get("outcome")


def watch_textual_progress(
    *,
    run_id: str,
    plan: Plan,
    config: RalphConfig,
    store: StateStore,
    events: EventLogger,
    provider_event_store: ProviderEventStore,
) -> None:
    app = RalphDashboardApp(
        run_id=run_id,
        plan=plan,
        config=config,
        store=store,
        events=events,
        provider_event_store=provider_event_store,
        orchestrator_call=None,
        auto_exit_on_terminal=False,
    )
    app.run()
