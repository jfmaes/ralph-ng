"""Provider event persistence — JSONL files per attempt."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .events import ProviderEvent


class ProviderEventStore:
    """Persist and replay provider events as JSONL files.

    Layout: ``<state_root>/runs/<run_id>/<task_id>/attempt_<n>/provider_events.jsonl``
    """

    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root)

    def log(self, event: ProviderEvent) -> None:
        """Append a single event to the appropriate JSONL file."""
        path = self._event_file(
            event.run_id,
            event.task_id,
            event.attempt_number,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_jsonl_line())
            fh.write("\n")
            fh.flush()

    def read_events(
        self,
        run_id: str,
        task_id: str,
        attempt_number: int,
    ) -> list[ProviderEvent]:
        """Read all events for a specific attempt."""
        path = self._event_file(run_id, task_id, attempt_number)
        if not path.exists():
            return []
        events: list[ProviderEvent] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(ProviderEvent.from_jsonl_line(line))
                except (json.JSONDecodeError, ValidationError):
                    continue
        return events

    def list_attempts(self, run_id: str, task_id: str) -> list[int]:
        """Return sorted attempt numbers that have provider event files."""
        task_dir = self.state_root / "runs" / run_id / task_id
        if not task_dir.exists():
            return []
        attempts: list[int] = []
        for entry in task_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("attempt_"):
                try:
                    num = int(entry.name.split("_", 1)[1])
                    if (entry / "provider_events.jsonl").exists():
                        attempts.append(num)
                except (ValueError, IndexError):
                    continue
        return sorted(attempts)

    def _event_file(self, run_id: str, task_id: str, attempt_number: int) -> Path:
        return (
            self.state_root
            / "runs"
            / run_id
            / task_id
            / f"attempt_{attempt_number}"
            / "provider_events.jsonl"
        )
