"""Append-only JSONL event logging."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    event_type: str
    task_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventLogger:
    """Write append-only event records to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> EventRecord:
        record = EventRecord(
            timestamp=datetime.now(UTC).isoformat(),
            event_type=event_type,
            task_id=task_id,
            data={} if data is None else data,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
        return record

    def read_events(self) -> list[EventRecord]:
        if not self.path.exists():
            return []
        records: list[EventRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    records.append(EventRecord.model_validate(payload))
                except (json.JSONDecodeError, ValidationError):
                    continue
        return records

    def read_events_tail(self, n: int) -> list[EventRecord]:
        """Read the last *n* events efficiently by reading from the end."""
        if not self.path.exists():
            return []
        lines: list[str] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                lines.append(line)
                if len(lines) > n:
                    lines.pop(0)
        records: list[EventRecord] = []
        for line in lines:
            try:
                payload = json.loads(line)
                records.append(EventRecord.model_validate(payload))
            except (json.JSONDecodeError, ValidationError):
                continue
        return records
