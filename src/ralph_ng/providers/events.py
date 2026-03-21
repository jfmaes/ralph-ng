"""Normalized provider event schema.

Provider events are emitted by transports during model invocations and
persisted as JSONL for replay and observability.  They are provider-neutral —
Claude and Codex activity is represented with the same event types.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderEventType(StrEnum):
    """Normalized event types emitted by transports."""

    AGENT_STARTED = "agent.started"
    AGENT_COMMENTARY = "agent.commentary"
    AGENT_STDERR = "agent.stderr"
    AGENT_PROGRESS = "agent.progress"
    AGENT_STRUCTURED_OUTPUT = "agent.structured_output"
    AGENT_FINISHED = "agent.finished"
    AGENT_ERROR = "agent.error"


class ProviderEvent(BaseModel):
    """A single normalized provider event.

    Fields
    ------
    run_id:
        Identifies the ralph run.
    task_id:
        The task being worked on.
    attempt_number:
        Which attempt within the task (0-indexed).
    provider:
        Which provider emitted this (e.g. "claude", "codex").
    role:
        "executor" or "reviewer".
    review_round:
        0 for executor, 1 for first review, 2 for challenge, 3 for
        critical final pass.
    event_type:
        One of the ``ProviderEventType`` values.
    timestamp:
        ISO 8601 timestamp.
    payload:
        Arbitrary event-specific data.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    attempt_number: int
    provider: str
    role: str
    review_round: int = 0
    event_type: ProviderEventType
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_jsonl_line(self) -> str:
        """Serialize to a single JSONL line (no trailing newline)."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl_line(cls, line: str) -> ProviderEvent:
        """Deserialize from a single JSONL line."""
        return cls.model_validate_json(line)


def make_event(
    *,
    run_id: str,
    task_id: str,
    attempt_number: int,
    provider: str,
    role: str,
    review_round: int,
    event_type: ProviderEventType,
    payload: dict[str, Any] | None = None,
) -> ProviderEvent:
    """Convenience factory for creating provider events."""
    return ProviderEvent(
        run_id=run_id,
        task_id=task_id,
        attempt_number=attempt_number,
        provider=provider,
        role=role,
        review_round=review_round,
        event_type=event_type,
        payload=payload or {},
    )
