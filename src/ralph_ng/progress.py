"""Rich-based TUI dashboard for ralph-ng run observability."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .adapters.streaming import LiveActivityTracker
from .config import RalphConfig
from .events import EventLogger, EventRecord
from .plan import Plan
from .providers.event_store import ProviderEventStore
from .providers.events import ProviderEventType
from .state import StateStore, TaskStateRecord
from .utils import parse_human_intervention_payload

# ── Color language ─────────────────────────────────────────────────────
# Green:   success (done, pass, accept)
# Yellow:  warning (repair, nearing budget, low confidence)
# Red:     error (blocked, failed, FAIL verification, escalation)
# Cyan:    review (reviewer activity, rounds, elapsed)
# Dim:     inactive (pending, idle, timestamps)
# Blue:    provider identity
# Magenta: system (blocked_system, high/critical risk)

_STATE_STYLES: dict[str, str] = {
    "pending": "dim",
    "ready": "white",
    "in_progress": "bold white",
    "review": "cyan",
    "repair": "yellow",
    "done": "green",
    "blocked_human": "bold red",
    "blocked_system": "magenta",
    "failed_exhausted": "bold red",
}

_STATE_LABELS: dict[str, str] = {
    "pending": "pending",
    "ready": "ready",
    "in_progress": "working",
    "review": "review",
    "repair": "repair",
    "done": "done",
    "blocked_human": "BLOCKED",
    "blocked_system": "SYS_BLK",
    "failed_exhausted": "FAILED",
}

_RUN_STATUS_STYLES: dict[str, str] = {
    "running": "bold green",
    "completed": "green",
    "blocked_human": "bold red",
    "blocked_system": "magenta",
    "failed_exhausted": "bold red",
    "paused": "bold yellow",
}

_EVENT_STYLES: dict[str, str] = {
    "task.accepted": "green",
    "executor.started": "white",
    "executor.completed": "white",
    "reviewer.started": "cyan",
    "reviewer.verdict": "cyan",
    "verification.result": "white",
    "challenge.executor_started": "yellow",
    "challenge.reviewer_started": "cyan",
    "review.escalated": "bold red",
    "review.risk_classified": "magenta",
    "task.transition": "dim",
    "run.started": "dim",
    "run.stopped": "dim",
    "run.resumed": "dim",
    "ratchet.added": "dim cyan",
}

NARROW_THRESHOLD = 80
STALE_RUN_SECONDS = 15


class ProviderActivity:
    """Tracks latest activity for a provider role."""

    def __init__(self) -> None:
        self.provider: str = ""
        self.model: str = ""
        self.role: str = ""
        self.review_round: int = 0
        self.latest_commentary: str = ""
        self.current_action: str = ""
        self.started_at: str = ""
        self.elapsed_seconds: float = 0.0
        self.active: bool = False
        self.recent_lines: list[str] = []


class ProgressRenderer:
    """Build the ralph-ng TUI dashboard from persisted state."""

    def __init__(
        self,
        *,
        store: StateStore,
        events: EventLogger,
        provider_event_store: ProviderEventStore | None = None,
        config: RalphConfig | None = None,
        executor_activity_tracker: LiveActivityTracker | None = None,
        reviewer_activity_tracker: LiveActivityTracker | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.provider_event_store = provider_event_store
        self.config = config or RalphConfig()
        self.executor_activity_tracker = executor_activity_tracker
        self.reviewer_activity_tracker = reviewer_activity_tracker
        self.executor_activity = ProviderActivity()
        self.reviewer_activity = ProviderActivity()

    def render_text(self, run_id: str, *, plan: Plan | None = None) -> str:
        console = Console(width=120, record=True)
        console.print(self.build_renderable(run_id, plan=plan, console_width=120))
        return console.export_text()

    def build_renderable(
        self,
        run_id: str,
        *,
        plan: Plan | None = None,
        console_width: int | None = None,
    ):
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")

        width = console_width or 120
        narrow = width < NARROW_THRESHOLD
        summary = self.store.get_run_summary(run_id)
        task_states = self.store.list_task_states(run_id)
        run_events = self._run_events(run_id)
        task_titles = {} if plan is None else {t.id: t.title for t in plan.tasks}
        task_order = [] if plan is None else [t.id for t in plan.tasks]
        current_task_id = self._current_task_id(task_states)
        current_task = None if current_task_id is None else task_states.get(current_task_id)

        self._update_provider_activity(run_id, current_task)

        sections: list[Any] = []

        # Header
        sections.append(self._build_header(run))
        sections.append(Rule(style="dim"))

        # Problem banner (tier 1)
        banner = self._build_problem_banner(run_id, run, task_states, current_task)
        if banner is not None:
            sections.append(banner)

        # Tasks + Live Activity
        task_table = self._build_task_table(task_states, task_titles, task_order, current_task_id, narrow)
        focus = self._build_focus_panel(run_id, current_task, task_titles, run_events)
        activity = self._build_activity_panel(narrow)
        cost = self._build_cost_panel(run_id, summary)

        if narrow:
            sections.append(task_table)
            sections.append(Text(""))
            sections.append(focus)
            sections.append(Text(""))
            sections.append(activity)
        else:
            top = Table.grid(expand=True)
            top.add_column(ratio=3)
            top.add_column(ratio=2)
            top.add_row(task_table, Group(focus, Text(""), activity))
            sections.append(top)

        sections.append(Rule(style="dim"))

        # Cost (inline)
        sections.append(cost)

        sections.append(Rule(style="dim"))

        # Event log
        event_limit = 6 if narrow else 10
        sections.append(self._build_event_log(run_id, event_limit, run_events))

        return Group(*sections)

    # ── Header ────────────────────────────────────────────────────────

    def _build_header(self, run) -> Table:
        elapsed = self._elapsed_since(run.created_at)
        status_style = _RUN_STATUS_STYLES.get(run.status, "white")

        header = Table.grid(expand=True)
        header.add_column()
        header.add_column(justify="right")
        header.add_row(
            Text("ralph-ng", style="bold"),
            Text.assemble(
                (run.id, "dim"),
                "   ",
                (run.status.upper(), status_style),
                "   ",
                (elapsed, "cyan"),
            ),
        )
        return header

    # ── Problem banner ────────────────────────────────────────────────

    def _build_problem_banner(
        self,
        run_id: str,
        run,
        task_states: dict[str, TaskStateRecord],
        current_task: TaskStateRecord | None,
    ) -> Panel | None:
        lines: list[Text] = []

        # Check for blocked/failed tasks
        for tid, ts in task_states.items():
            if ts.state in ("blocked_human", "blocked_system", "failed_exhausted"):
                label = _STATE_LABELS.get(ts.state, ts.state)
                lines.append(Text.assemble(
                    (f"{tid}: {label}", "bold red"),
                    f"  attempt {ts.attempt_count}/{self.config.ralph.max_retries_per_task}",
                ))
                if ts.last_error:
                    if ts.state == "blocked_human":
                        structured = parse_human_intervention_payload(ts.last_error)
                        if structured is not None:
                            summary = structured.get("summary", "")
                            what_failed = structured.get("what_failed", "")
                            findings = structured.get("findings", [])
                            actions = structured.get("expected_human_actions", [])
                            if summary:
                                lines.append(Text(f"  {summary}", style="red"))
                            if what_failed:
                                lines.append(Text(f"  Failed: {what_failed}", style="red"))
                            if isinstance(findings, list):
                                for finding in findings[:3]:
                                    if not isinstance(finding, dict):
                                        continue
                                    fid = finding.get("id", "?")
                                    severity = finding.get("severity", "?")
                                    category = finding.get("category", "?")
                                    description = finding.get("description", "")
                                    lines.append(Text(
                                        f"  {fid} [{severity}] ({category}): {description}",
                                        style="red",
                                    ))
                            if isinstance(actions, list) and actions:
                                lines.append(Text("  Do this:", style="bold yellow"))
                                for action in actions[:4]:
                                    if isinstance(action, str) and action.strip():
                                        lines.append(Text(f"    - {action}", style="yellow"))
                        else:
                            error_preview = self._humanize_error(ts.last_error, max_len=300)
                            lines.append(Text(f"  {error_preview}", style="red"))
                    else:
                        error_preview = self._humanize_error(ts.last_error, max_len=300)
                        lines.append(Text(f"  {error_preview}", style="red"))

        # Check for tasks nearing retry budget
        if current_task and current_task.state not in ("done", "blocked_human", "blocked_system", "failed_exhausted"):
            budget = self.config.ralph.max_retries_per_task
            if current_task.attempt_count >= budget - 1 and current_task.attempt_count > 1:
                lines.append(Text.assemble(
                    (f"{current_task.task_id}: attempt {current_task.attempt_count}/{budget}", "bold yellow"),
                    " — nearing retry budget",
                ))

        if (
            run.status == "running"
            and current_task is not None
            and current_task.state in ("in_progress", "review", "repair")
        ):
            controller_problem = self._controller_problem(run_id)
            if controller_problem is not None:
                stale_seconds, controller_dead = controller_problem
                lines.append(Text.assemble(
                    ("Run heartbeat stale", "bold red"),
                    f"  no controller heartbeat for {self._format_duration_seconds(stale_seconds)}",
                ))
                if controller_dead:
                    lines.append(Text("  The recorded controller PID is no longer alive.", style="red"))
                lines.append(Text(
                    "  The orchestrator process may have died or the active provider call may be wedged.",
                    style="red",
                ))

        if not lines:
            return None

        content = Text("\n").join(lines)
        if run.status in ("blocked_human", "blocked_system"):
            title = "!! HUMAN INTERVENTION REQUIRED"
            lines.append(Text(""))
            lines.append(Text(f"  Logs: {self.events.path}", style="dim"))
            # Show provider event path for the blocked task
            for tid, ts in task_states.items():
                if ts.state in ("blocked_human", "blocked_system", "failed_exhausted"):
                    if self.provider_event_store is not None:
                        pe_path = self.provider_event_store._event_file(run_id, tid, ts.attempt_count)
                        if pe_path.exists():
                            lines.append(Text(f"  Provider events: {pe_path}", style="dim"))
                    break
            lines.append(Text(f"  Resume: ralph resume --run-id {run_id}", style="dim"))
            content = Text("\n").join(lines)
            return Panel(content, border_style="bold red", title=title, title_align="left")

        return Panel(content, border_style="yellow", title="!! WARNING", title_align="left")

    # ── Task table ────────────────────────────────────────────────────

    def _build_task_table(
        self,
        task_states: dict[str, TaskStateRecord],
        task_titles: dict[str, str],
        task_order: list[str],
        current_task_id: str | None,
        narrow: bool,
    ) -> Table:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            pad_edge=False,
            expand=True,
        )
        table.add_column("", width=1)  # active indicator
        table.add_column("State", width=8)
        table.add_column("ID", width=8)
        title_width = 25 if narrow else 35
        table.add_column("Title", width=title_width, no_wrap=True)
        table.add_column("Exec", width=6, justify="right")
        table.add_column("", width=2)  # alert

        ordered_ids = task_order or sorted(task_states.keys())
        for tid in ordered_ids:
            ts = task_states.get(tid)
            if ts is None:
                continue
            state = ts.state
            style = _STATE_STYLES.get(state, "white")
            label = _STATE_LABELS.get(state, state)
            indicator = ">" if tid == current_task_id else " "
            title = task_titles.get(tid, "")
            if len(title) > title_width:
                title = title[: title_width - 1] + "\u2026"
            att = f"{ts.attempt_count}" if ts.attempt_count > 0 else "-"

            alert = ""
            budget = self.config.ralph.max_retries_per_task
            if ts.attempt_count >= budget - 1 and ts.attempt_count > 1:
                alert = "!!"
            elif state in ("blocked_human", "blocked_system", "failed_exhausted"):
                alert = "!!"

            alert_style = "bold red" if state in ("blocked_human", "blocked_system", "failed_exhausted") else "bold yellow"

            table.add_row(
                Text(indicator, style="bold white"),
                Text(label, style=style),
                Text(tid.replace("TASK-", ""), style=style),
                Text(title, style=style),
                Text(att, style=style),
                Text(alert, style=alert_style),
            )

        return table

    def _build_focus_panel(
        self,
        run_id: str,
        current_task: TaskStateRecord | None,
        task_titles: dict[str, str],
        run_events: list[EventRecord],
    ) -> Panel:
        if current_task is None:
            return Panel(Text("No active task", style="dim"), title="Current Task", title_align="left")

        lines: list[Text] = []
        title = task_titles.get(current_task.task_id, "(untitled)")
        state_style = _STATE_STYLES.get(current_task.state, "white")
        state_label = _STATE_LABELS.get(current_task.state, current_task.state)
        task_id = current_task.task_id.replace("TASK-", "")
        budget = self.config.ralph.max_retries_per_task

        lines.append(Text.assemble(
            (task_id, "bold"),
            "  ",
            (title, "white"),
        ))
        lines.append(Text.assemble(
            (state_label, state_style),
            "  ",
            (f"execution attempt {current_task.attempt_count}/{budget}", "dim"),
        ))
        active_role_line = self._active_role_focus_line()
        if active_role_line is not None:
            lines.append(active_role_line)

        verification_events = self._verification_events(run_id, current_task, run_events)
        if verification_events:
            passes = sum(1 for event in verification_events if event.data.get("success"))
            total = len(verification_events)
            status_style = "green" if passes == total else "bold red"
            commands = ", ".join(
                self._truncate_command(str(event.data.get("command", "?")), 24)
                for event in verification_events
            )
            lines.append(Text.assemble(
                ("verification ", "bold"),
                (f"{passes}/{total} pass", status_style),
            ))
            lines.append(Text(f"  {commands}", style="dim"))
        else:
            lines.append(Text("verification pending", style="dim"))

        risk_event = self._latest_task_event(
            run_events,
            task_id=current_task.task_id,
            attempt_number=current_task.attempt_count,
            event_type="review.risk_classified",
        )
        if risk_event is not None:
            risk = str(risk_event.data.get("risk", "?"))
            risk_style = {
                "low": "green",
                "medium": "yellow",
                "high": "bold red",
                "critical": "bold red",
            }.get(risk, "white")
            lines.append(Text.assemble(
                ("reviewer-scrutiny: ", "bold"),
                (risk, risk_style),
                " (diff based)  ",
                (f"{risk_event.data.get('diff_lines', '?')} lines", "dim"),
                "  ",
                (f"{risk_event.data.get('file_count', '?')} files", "dim"),
            ))

        review_summary, review_findings = self._latest_review_summary(current_task)
        challenge_pending = self._review_challenge_pending(run_id, current_task)
        if review_summary:
            finding_count = f"  ({len(review_findings)} findings)" if review_findings else ""
            verdict = Text.assemble(
                ("first-pass verdict: " if challenge_pending else "review verdict: ", "bold"),
                (review_summary, "yellow" if (review_findings or challenge_pending) else "green"),
                (finding_count, "dim"),
            )
            if challenge_pending:
                verdict.append("  challenge pending", style="bold yellow")
            lines.append(verdict)

        last_event = self._latest_task_event(
            run_events,
            task_id=current_task.task_id,
            attempt_number=current_task.attempt_count,
        )
        if last_event is not None:
            style = _EVENT_STYLES.get(last_event.event_type, "dim")
            lines.append(Text.assemble(
                ("last ", "bold"),
                (last_event.event_type, style),
                "  ",
                (self._event_detail(last_event), style),
                "  ",
                (self._format_time(last_event.timestamp), "dim"),
            ))

        return Panel(Text("\n").join(lines), title="Current Task", title_align="left")

    def _active_role_focus_line(self) -> Text | None:
        if self.executor_activity.active:
            elapsed = self._format_clock_duration(self._activity_elapsed_seconds(self.executor_activity))
            provider_label = self.executor_activity.provider or "executor"
            model_label = f"/{self.executor_activity.model}" if self.executor_activity.model else ""
            return Text.assemble(
                ("executor running for ", "bold"),
                (elapsed, self._timeout_style(self.executor_activity, "executor")),
                "  ",
                (f"({provider_label}{model_label})", "dim"),
            )
        if self.reviewer_activity.active:
            elapsed = self._format_clock_duration(self._activity_elapsed_seconds(self.reviewer_activity))
            provider_label = self.reviewer_activity.provider or "reviewer"
            model_label = f"/{self.reviewer_activity.model}" if self.reviewer_activity.model else ""
            round_label = (
                f"  round {self.reviewer_activity.review_round}"
                if self.reviewer_activity.review_round > 0
                else ""
            )
            return Text.assemble(
                ("reviewer running for ", "bold"),
                (elapsed, self._timeout_style(self.reviewer_activity, "reviewer")),
                "  ",
                (f"({provider_label}{model_label})", "dim"),
                (round_label, "cyan"),
            )
        return None

    # ── Live activity ─────────────────────────────────────────────────

    def _build_activity_panel(self, narrow: bool) -> Table:
        grid = Table.grid(padding=(0, 1))
        grid.add_column()

        # Executor header
        if self.executor_activity.active:
            elapsed = self._elapsed_label(self.executor_activity, "executor")
            elapsed_style = self._timeout_style(self.executor_activity, "executor")
            provider_label = self.executor_activity.provider or "executor"
            model_label = f"/{self.executor_activity.model}" if self.executor_activity.model else ""
            action = self.executor_activity.current_action or "working"
            grid.add_row(Text.assemble(
                ("Executor ", "bold"),
                (f"({provider_label}{model_label})", "blue"),
                "  ",
                (action, "white"),
                "  ",
                (elapsed, elapsed_style),
            ))
            activity_lines = self.executor_activity.recent_lines
            if activity_lines:
                for line in activity_lines[-3:]:
                    grid.add_row(Text(f"  {line}", style="dim italic"))
            elif self.executor_activity.latest_commentary:
                grid.add_row(Text(f"  {self.executor_activity.latest_commentary}", style="dim italic"))
            else:
                grid.add_row(Text("  > waiting for provider output", style="dim italic"))
        else:
            grid.add_row(Text("Executor  idle", style="dim"))

        grid.add_row(Text(""))

        # Reviewer header
        if self.reviewer_activity.active:
            elapsed = self._elapsed_label(self.reviewer_activity, "reviewer")
            elapsed_style = self._timeout_style(self.reviewer_activity, "reviewer")
            provider_label = self.reviewer_activity.provider or "reviewer"
            model_label = f"/{self.reviewer_activity.model}" if self.reviewer_activity.model else ""
            action = self.reviewer_activity.current_action or "reviewing"
            round_label = f"round {self.reviewer_activity.review_round}" if self.reviewer_activity.review_round > 0 else ""
            grid.add_row(Text.assemble(
                ("Reviewer ", "bold"),
                (f"({provider_label}{model_label})", "blue"),
                "  ",
                (action, "white"),
                "  ",
                (elapsed, elapsed_style),
            ))
            if round_label:
                grid.add_row(Text(f"  {round_label}", style="cyan"))
            activity_lines = self.reviewer_activity.recent_lines
            if activity_lines:
                for line in activity_lines[-3:]:
                    grid.add_row(Text(f"  {line}", style="dim italic"))
            elif self.reviewer_activity.latest_commentary:
                grid.add_row(Text(f"  {self.reviewer_activity.latest_commentary}", style="dim italic"))
            else:
                grid.add_row(Text("  > waiting for provider output", style="dim italic"))
        else:
            grid.add_row(Text("Reviewer  idle", style="dim"))

        return grid

    # ── Verification ──────────────────────────────────────────────────

    def _build_verification_table(
        self,
        run_id: str,
        current_task: TaskStateRecord | None,
    ) -> Table:
        table = Table(show_header=False, box=None, pad_edge=False, expand=True)
        table.add_column(width=4)  # status icon
        table.add_column(no_wrap=True)  # command
        table.add_column(width=6, justify="right")  # duration

        vr_events = self._verification_events(run_id, current_task)
        if not vr_events:
            table.add_row(Text(""), Text("No verification results yet", style="dim"), Text(""))
            return table

        for event in vr_events:
            success = event.data.get("success", False)
            command = event.data.get("command", "?")
            if len(command) > 45:
                command = command[:44] + "\u2026"
            icon = Text("pass", style="green") if success else Text("FAIL", style="bold red")
            table.add_row(icon, Text(command), Text(""))

        return table

    # ── Cost panel ────────────────────────────────────────────────────

    def _build_cost_panel(self, run_id: str, summary: dict) -> Table:
        grid = Table.grid(padding=(0, 1))
        grid.add_column()

        inv = summary.get("invocations", {})
        input_tokens = inv.get("input_tokens", 0)
        output_tokens = inv.get("output_tokens", 0)
        cost = ((input_tokens + output_tokens) / 1000) * 0.01

        def fmt_tokens(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}K"
            return str(n)

        grid.add_row(Text.assemble(
            (f"{fmt_tokens(input_tokens)} in", "white"),
            "  ",
            (f"{fmt_tokens(output_tokens)} out", "white"),
            "  ",
            (f"~${cost:.2f}", "cyan"),
        ))

        counts = self.store.get_invocation_counts(run_id)
        exec_count = counts.get("executor", 0)
        review_count = counts.get("reviewer", 0)
        grid.add_row(Text(f"{exec_count} executor  {review_count} reviewer", style="dim"))

        return grid

    # ── Event log ─────────────────────────────────────────────────────

    def _build_event_log(self, run_id: str, limit: int, run_events: list[EventRecord]) -> Table:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            pad_edge=False,
            expand=True,
        )
        table.add_column("Time", width=8, style="dim", no_wrap=True)
        table.add_column("Event", width=22, no_wrap=True)
        table.add_column("Task", width=8, no_wrap=True)
        table.add_column("Detail", ratio=1)

        events = run_events[-limit:]
        for event in events:
            ts = self._format_time(event.timestamp)
            style = _EVENT_STYLES.get(event.event_type, "dim")
            detail = self._event_detail(event)

            # Override style for failures
            if event.event_type == "verification.result" and not event.data.get("success"):
                style = "bold red"
            elif event.event_type == "reviewer.verdict" and event.data.get("decision") == "reject":
                style = "yellow"

            table.add_row(
                Text(ts, style="dim"),
                Text(event.event_type, style=style),
                Text(event.task_id or "-", style="dim"),
                Text(detail, style=style),
            )

        if not events:
            table.add_row(Text(""), Text("Waiting for events...", style="dim"), Text(""), Text(""))

        return table

    # ── Helpers ────────────────────────────────────────────────────────

    def _current_task_id(self, task_states: dict[str, TaskStateRecord]) -> str | None:
        active = [
            r for r in task_states.values()
            if r.state not in ("pending", "done", "failed_exhausted")
        ]
        if not active:
            return None
        return max(active, key=lambda r: r.updated_at).task_id

    def _update_provider_activity(self, run_id: str, current_task: TaskStateRecord | None) -> None:
        self.executor_activity = ProviderActivity()
        self.reviewer_activity = ProviderActivity()

        if self.executor_activity_tracker is not None:
            self.executor_activity.recent_lines = [
                self._format_activity_event(event)
                for event in self.executor_activity_tracker.recent_display(5)
            ]
        if self.reviewer_activity_tracker is not None:
            self.reviewer_activity.recent_lines = [
                self._format_activity_event(event)
                for event in self.reviewer_activity_tracker.recent_display(5)
            ]

        if self.provider_event_store is not None and current_task is not None:
            events = self.provider_event_store.read_events(
                run_id, current_task.task_id, current_task.attempt_count,
            )
            for event in events:
                target = self.executor_activity if event.role == "executor" else self.reviewer_activity
                if event.event_type == ProviderEventType.AGENT_STARTED:
                    target.active = True
                    target.provider = event.provider
                    target.role = event.role
                    target.review_round = event.review_round
                    target.started_at = event.timestamp
                    model = str(event.payload.get("model", "")).strip()
                    if model:
                        target.model = model
                    if event.role == "executor":
                        target.current_action = "repairing (challenge)" if event.review_round > 0 else "executing"
                    else:
                        target.current_action = "final review" if event.review_round > 1 else "reviewing"
                elif event.event_type == ProviderEventType.AGENT_FINISHED:
                    target.active = False
                    target.elapsed_seconds = event.payload.get("duration", 0.0)
                elif event.event_type in (
                    ProviderEventType.AGENT_THOUGHT_STARTED,
                    ProviderEventType.AGENT_THOUGHT_DELTA,
                    ProviderEventType.AGENT_THOUGHT_FINISHED,
                    ProviderEventType.AGENT_COMMENTARY,
                    ProviderEventType.AGENT_PROGRESS,
                    ProviderEventType.AGENT_STRUCTURED_OUTPUT,
                    ProviderEventType.AGENT_ERROR,
                ):
                    target.latest_commentary = (
                        str(event.payload.get("summary", "")).strip()
                        or str(event.payload.get("text", "")).strip()
                    )
            if not self.executor_activity.recent_lines:
                self.executor_activity.recent_lines = self._activity_lines_for_role(
                    events, "executor", limit=5,
                )
            if not self.reviewer_activity.recent_lines:
                self.reviewer_activity.recent_lines = self._activity_lines_for_role(
                    events, "reviewer", limit=5,
                )
            # Fill model from config
            if self.config.roles:
                if not self.executor_activity.model:
                    self.executor_activity.model = self.config.roles.executor.model
                if not self.reviewer_activity.model:
                    self.reviewer_activity.model = self.config.roles.reviewer.model
            return

        # Fallback: business events
        for event in self.events.read_events():
            if event.data.get("run_id") != run_id:
                continue
            if event.event_type == "executor.started":
                self.executor_activity.active = True
                self.executor_activity.current_action = "executing"
            elif event.event_type == "executor.completed":
                self.executor_activity.active = False
            elif event.event_type == "reviewer.started":
                self.reviewer_activity.active = True
                self.reviewer_activity.current_action = "reviewing"
                self.reviewer_activity.review_round = event.data.get("round", 1)
            elif event.event_type == "reviewer.verdict":
                self.reviewer_activity.active = False
            elif event.event_type == "challenge.executor_started":
                self.executor_activity.active = True
                self.executor_activity.current_action = "repairing (challenge)"
            elif event.event_type == "challenge.reviewer_started":
                self.reviewer_activity.active = True
                self.reviewer_activity.current_action = "final review"
                self.reviewer_activity.review_round = event.data.get("round", 2)

    @staticmethod
    def _activity_lines_for_role(
        events: list,
        role: str,
        *,
        limit: int,
    ) -> list[str]:
        lines: list[str] = []
        preferred_types = {
            ProviderEventType.AGENT_THOUGHT_STARTED,
            ProviderEventType.AGENT_THOUGHT_DELTA,
            ProviderEventType.AGENT_THOUGHT_FINISHED,
            ProviderEventType.AGENT_COMMENTARY,
            ProviderEventType.AGENT_STRUCTURED_OUTPUT,
            ProviderEventType.AGENT_ERROR,
        }
        fallback_types = preferred_types | {ProviderEventType.AGENT_PROGRESS}

        for allowed_types in (preferred_types, fallback_types):
            lines = ProgressRenderer._summarize_provider_events(events, role, allowed_types)
            if lines:
                break

        return lines[-limit:]

    @staticmethod
    def _format_activity_event(event) -> str:
        text = str(getattr(event, "text", "") or event.summary).strip()
        if getattr(event, "block_id", None):
            return f"Thought: {text}"
        return text

    @staticmethod
    def _summarize_provider_events(events: list, role: str, allowed_types: set) -> list[str]:
        items: list[str] = []
        thought_positions: dict[str, tuple[int, int]] = {}

        for event in events:
            if getattr(event, "role", None) != role or getattr(event, "event_type", None) not in allowed_types:
                continue
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
                    items.append(f"Thought {thought_number}: {text}")
                else:
                    position, thought_number = existing
                    items[position] = f"Thought {thought_number}: {text}"
            else:
                items.append(text)

        return items

    def _verification_events(
        self,
        run_id: str,
        current_task: TaskStateRecord | None,
        run_events: list[EventRecord] | None = None,
    ) -> list[EventRecord]:
        """Get verification events for the current task's latest attempt."""
        if current_task is None:
            return []
        events = run_events if run_events is not None else self._run_events(run_id)
        results: list[EventRecord] = []
        for event in events:
            if event.event_type != "verification.result":
                continue
            if event.task_id == current_task.task_id and event.data.get("attempt") == current_task.attempt_count:
                results.append(event)
        return results

    @staticmethod
    def _truncate_command(command: str, max_len: int) -> str:
        if len(command) <= max_len:
            return command
        return command[: max_len - 1] + "\u2026"

    def _run_events(self, run_id: str) -> list[EventRecord]:
        return [
            event
            for event in self.events.read_events()
            if event.data.get("run_id") == run_id
        ]

    def _latest_review_summary(self, current_task: TaskStateRecord) -> tuple[str | None, list[str]]:
        attempt = self.store.get_latest_attempt(current_task.run_id, current_task.task_id)
        if attempt is None or attempt.review_path is None:
            return None, []
        path = Path(attempt.review_path)
        if not path.exists():
            return None, []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None, []
        findings_payload = payload.get("findings")
        if not isinstance(findings_payload, dict):
            return None, []
        decision = str(findings_payload.get("decision", "")).strip()
        confidence = findings_payload.get("confidence")
        summary_text = str(findings_payload.get("summary", "")).strip()
        summary = None
        if decision:
            if isinstance(confidence, (int, float)):
                summary = f"{decision} ({confidence:.2f})"
            else:
                summary = decision
        elif summary_text:
            summary = summary_text

        rendered_findings: list[str] = []
        raw_findings = findings_payload.get("findings")
        if isinstance(raw_findings, list):
            for finding in raw_findings:
                if not isinstance(finding, dict):
                    continue
                fid = str(finding.get("id", "?"))
                severity = str(finding.get("severity", "?"))
                category = str(finding.get("category", "?"))
                description = str(finding.get("description", "")).strip()
                if description:
                    rendered_findings.append(f"{fid} [{severity}] ({category}): {description}")
        return summary or summary_text or None, rendered_findings

    @staticmethod
    def _review_round_from_path(path: Path) -> int | None:
        name = path.name
        if "_round1" in name:
            return 1
        if "_round2" in name:
            return 2
        return None

    def _review_challenge_pending(self, run_id: str, current_task: TaskStateRecord) -> bool:
        attempt = self.store.get_latest_attempt(current_task.run_id, current_task.task_id)
        if attempt is None or attempt.review_path is None:
            return False
        if self._review_round_from_path(Path(attempt.review_path)) != 1:
            return False
        row = self.store.connection.execute(
            "SELECT phase_checkpoint FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        checkpoint = row["phase_checkpoint"]
        if not isinstance(checkpoint, str):
            return False
        prefixes = (
            f"challenge_executor:{current_task.task_id}:attempt:{current_task.attempt_count}",
            f"challenge_reviewer:{current_task.task_id}:attempt:{current_task.attempt_count}",
        )
        return checkpoint.startswith(prefixes)

    def _controller_problem(self, run_id: str) -> tuple[int, bool] | None:
        row = self.store.connection.execute(
            "SELECT controller_pid, last_heartbeat_at FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        controller_dead = False
        pid = row["controller_pid"]
        if pid is not None:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                controller_dead = True
        heartbeat = row["last_heartbeat_at"]
        if heartbeat is None:
            return (0, controller_dead) if controller_dead else None
        age = self._seconds_since(heartbeat)
        if age is None:
            return (0, controller_dead) if controller_dead else None
        if age >= STALE_RUN_SECONDS or controller_dead:
            return age, controller_dead
        return None

    @staticmethod
    def _latest_task_event(
        run_events: list[EventRecord],
        *,
        task_id: str,
        attempt_number: int,
        event_type: str | None = None,
    ) -> EventRecord | None:
        for event in reversed(run_events):
            if event.task_id != task_id:
                continue
            if event.data.get("attempt") not in (None, attempt_number):
                continue
            if event_type is not None and event.event_type != event_type:
                continue
            return event
        return None

    def _timeout_style(self, activity: ProviderActivity, role: str) -> str:
        """Color elapsed time based on timeout proximity."""
        if not activity.started_at:
            return "cyan"
        try:
            started = datetime.fromisoformat(activity.started_at)
            elapsed = (datetime.now(UTC) - started).total_seconds()
        except (ValueError, TypeError):
            return "cyan"

        if role == "executor":
            budget = self.config.timeouts.executor_call_seconds
        else:
            budget = self.config.timeouts.reviewer_call_seconds

        if elapsed >= budget * 0.95:
            return "bold red"
        if elapsed >= budget * 0.80:
            return "yellow"
        return "cyan"

    def _elapsed_label(self, activity: ProviderActivity, role: str) -> str:
        elapsed_seconds = self._activity_elapsed_seconds(activity)
        budget = self.config.timeouts.executor_call_seconds if role == "executor" else self.config.timeouts.reviewer_call_seconds
        return f"running for {self._format_clock_duration(elapsed_seconds)} / {self._format_clock_duration(budget)}"

    def _activity_elapsed_seconds(self, activity: ProviderActivity) -> int:
        if activity.started_at:
            elapsed = self._seconds_since(activity.started_at)
            if elapsed is not None:
                return elapsed
        return max(int(activity.elapsed_seconds), 0)

    @staticmethod
    def _seconds_since(iso_timestamp: str) -> int | None:
        try:
            start = datetime.fromisoformat(iso_timestamp)
            delta = datetime.now(UTC) - start
            total = int(delta.total_seconds())
            return max(total, 0)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _elapsed_since(iso_timestamp: str) -> str:
        try:
            start = datetime.fromisoformat(iso_timestamp)
            delta = datetime.now(UTC) - start
            total = int(delta.total_seconds())
            if total < 0:
                total = 0
            minutes, seconds = divmod(total, 60)
            hours, minutes = divmod(minutes, 60)
            if hours > 0:
                return f"{hours}h{minutes:02d}m"
            if minutes > 0:
                return f"{minutes}m{seconds:02d}s"
            return f"{seconds}s"
        except (ValueError, TypeError):
            return "?"

    @staticmethod
    def _format_time(iso_timestamp: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            return "??:??:??"

    @staticmethod
    def _format_duration_seconds(total_seconds: int | float) -> str:
        total = int(total_seconds)
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m{seconds:02d}s"

    @staticmethod
    def _format_clock_duration(total_seconds: int | float) -> str:
        total = max(int(total_seconds), 0)
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _humanize_error(raw: str, max_len: int = 200) -> str:
        """Extract a readable message from raw error text.

        Handles plain strings, JSON error objects (API errors), and
        multi-line error dumps.
        """
        text = raw.strip()
        if not text:
            return "(no details)"

        # Try parsing as JSON error blob
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                # API error format: {"type":"error","error":{"type":"...","message":"..."}}
                error = obj.get("error", obj)
                if isinstance(error, dict):
                    msg = error.get("message", "")
                    etype = error.get("type", "")
                    code = error.get("code", "")
                    parts = []
                    if etype:
                        parts.append(etype)
                    if code and code != etype:
                        parts.append(code)
                    if msg:
                        parts.append(msg)
                    if parts:
                        return ": ".join(parts)[:max_len]
                # Generic dict — try "message" or "reason" keys
                for key in ("message", "reason", "error", "detail"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()[:max_len]
        except (json.JSONDecodeError, TypeError):
            pass

        # Plain text — take the first meaningful line
        for line in text.splitlines():
            line = line.strip()
            if line and line not in ("{", "}", "[", "]"):
                return line[:max_len]

        return text[:max_len]

    @staticmethod
    def _event_detail(event: EventRecord) -> str:
        d = event.data
        t = event.event_type

        if t == "task.accepted":
            return f"commit {d.get('commit', '?')[:8]}"
        if t == "executor.started":
            return f"attempt {d.get('attempt', '?')}"
        if t == "executor.completed":
            status = d.get("status", "")
            error = d.get("error", "")
            if error and status != "completed":
                return ProgressRenderer._humanize_error(str(error), max_len=60)
            return str(status)
        if t == "reviewer.started":
            return f"round {d.get('round', '?')}"
        if t == "reviewer.verdict":
            decision = d.get("decision", "?")
            confidence = d.get("confidence", "?")
            return f"{decision} (confidence: {confidence})"
        if t == "verification.result":
            cmd = d.get("command", "?")
            if len(cmd) > 35:
                cmd = cmd[:34] + "\u2026"
            status = "pass" if d.get("success") else "FAIL"
            return f"{cmd}: {status}"
        if t == "task.transition":
            return f"{d.get('from_state', '?')} -> {d.get('to_state', '?')}"
        if t == "review.escalated":
            return f"risk: {d.get('risk', '?')}"
        if t == "review.risk_classified":
            return f"{d.get('risk', '?')} ({d.get('diff_lines', '?')} lines, {d.get('file_count', '?')} files)"
        if t == "run.stopped":
            reason = d.get("reason", "")
            return ProgressRenderer._humanize_error(reason, max_len=200) if reason else ""
        if t == "ratchet.added":
            cmd = d.get("command", "?")
            return f"+{cmd[:50]}"
        if t in ("challenge.executor_started", "challenge.reviewer_started"):
            return f"round {d.get('round', '?')}"
        return ""
