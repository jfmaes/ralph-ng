"""Rich-based progress rendering from persisted state and events."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from .events import EventLogger
from .plan import Plan
from .state import StateStore, TaskStateRecord


class ProgressRenderer:
    """Render the current run summary from state and event logs."""

    def __init__(self, *, store: StateStore, events: EventLogger) -> None:
        self.store = store
        self.events = events

    def render_text(self, run_id: str, *, plan: Plan | None = None) -> str:
        console = Console(width=120, record=True)
        console.print(self.build_renderable(run_id, plan=plan))
        return console.export_text()

    def build_renderable(self, run_id: str, *, plan: Plan | None = None):
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")

        summary = self.store.get_run_summary(run_id)
        task_states = self.store.list_task_states(run_id)
        task_titles = {} if plan is None else {task.id: task.title for task in plan.tasks}

        current_task_id = self._current_task_id(task_states)
        current_task = None if current_task_id is None else task_states[current_task_id]
        verification_lines = self._verification_lines(run_id)
        input_tokens = summary["invocations"]["input_tokens"]
        output_tokens = summary["invocations"]["output_tokens"]
        estimated_cost = ((input_tokens + output_tokens) / 1000) * 0.01

        header = Table.grid(expand=True)
        header.add_column()
        header.add_row(f"Run {run_id}")
        header.add_row(f"Status: {run.status}")

        body = Table.grid(padding=(0, 1))
        body.add_column()
        if current_task is not None:
            title = task_titles.get(current_task.task_id, "(unknown task)")
            body.add_row(
                f"Current Task: {current_task.task_id} | {title} | {current_task.state} | "
                f"Attempt {current_task.attempt_count}"
            )
        else:
            body.add_row("Current Task: none")

        if verification_lines:
            body.add_row("Verification:")
            for line in verification_lines:
                body.add_row(line)

        body.add_row(f"Tokens: in={input_tokens} out={output_tokens}")
        body.add_row(f"Estimated Cost (flat heuristic): ${estimated_cost:.2f}")
        if current_task is not None and current_task.last_error:
            body.add_row(f"Blocker: {current_task.last_error}")

        state_counts = ", ".join(
            f"{state}: {count}" for state, count in sorted(summary["task_states"].items())
        )
        body.add_row(f"Task Summary: {state_counts or '(none)'}")

        return Group(Panel.fit(header, title="ralph-ng"), body)

    def _current_task_id(self, task_states: dict[str, TaskStateRecord]) -> str | None:
        active_records = [
            record
            for record in task_states.values()
            if record.state not in {"pending", "done", "failed_exhausted"}
        ]
        if not active_records:
            return None
        latest = max(active_records, key=lambda item: item.updated_at)
        return latest.task_id

    def _verification_lines(self, run_id: str) -> list[str]:
        lines: list[str] = []
        for event in self.events.read_events():
            if event.event_type != "verification.result":
                continue
            if event.data.get("run_id") != run_id:
                continue
            status = "pass" if event.data.get("success") else "fail"
            lines.append(f"- {event.data.get('command')}: {status}")
        return lines
