"""Tests for provider event schema."""

from __future__ import annotations

import json

import pytest

from ralph_ng.providers.events import (
    ProviderEvent,
    ProviderEventType,
    make_event,
)


class TestProviderEventType:
    def test_all_event_types_exist(self) -> None:
        expected = {
            "agent.started",
            "agent.commentary",
            "agent.thought_started",
            "agent.thought_delta",
            "agent.thought_finished",
            "agent.stderr",
            "agent.progress",
            "agent.structured_output",
            "agent.finished",
            "agent.error",
        }
        actual = {e.value for e in ProviderEventType}
        assert actual == expected

    def test_is_str_enum(self) -> None:
        assert isinstance(ProviderEventType.AGENT_STARTED, str)
        assert ProviderEventType.AGENT_STARTED == "agent.started"


class TestProviderEvent:
    def test_minimal_construction(self) -> None:
        event = ProviderEvent(
            run_id="run-1",
            task_id="task-1",
            attempt_number=0,
            provider="claude",
            role="executor",
            event_type=ProviderEventType.AGENT_STARTED,
        )
        assert event.run_id == "run-1"
        assert event.review_round == 0
        assert event.payload == {}
        assert event.timestamp  # auto-populated

    def test_full_construction(self) -> None:
        event = ProviderEvent(
            run_id="run-1",
            task_id="task-1",
            attempt_number=1,
            provider="codex",
            role="reviewer",
            review_round=2,
            event_type=ProviderEventType.AGENT_COMMENTARY,
            timestamp="2026-03-20T10:00:00+00:00",
            payload={"text": "reviewing code quality"},
        )
        assert event.provider == "codex"
        assert event.role == "reviewer"
        assert event.review_round == 2
        assert event.payload["text"] == "reviewing code quality"

    def test_review_round_values(self) -> None:
        """review_round: 0=executor, 1=first review, 2=challenge, 3=critical final."""
        for round_num in (0, 1, 2, 3):
            event = ProviderEvent(
                run_id="r",
                task_id="t",
                attempt_number=0,
                provider="claude",
                role="executor",
                review_round=round_num,
                event_type=ProviderEventType.AGENT_STARTED,
            )
            assert event.review_round == round_num

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            ProviderEvent(
                run_id="r",
                task_id="t",
                attempt_number=0,
                provider="claude",
                role="executor",
                event_type=ProviderEventType.AGENT_STARTED,
                bogus="nope",  # type: ignore[call-arg]
            )


class TestJsonlSerialization:
    def test_to_jsonl_line(self) -> None:
        event = ProviderEvent(
            run_id="run-1",
            task_id="task-1",
            attempt_number=0,
            provider="claude",
            role="executor",
            event_type=ProviderEventType.AGENT_STARTED,
            timestamp="2026-03-20T10:00:00+00:00",
        )
        line = event.to_jsonl_line()
        assert "\n" not in line
        parsed = json.loads(line)
        assert parsed["run_id"] == "run-1"
        assert parsed["event_type"] == "agent.started"

    def test_from_jsonl_line(self) -> None:
        event = ProviderEvent(
            run_id="run-1",
            task_id="task-1",
            attempt_number=0,
            provider="codex",
            role="reviewer",
            review_round=1,
            event_type=ProviderEventType.AGENT_FINISHED,
            timestamp="2026-03-20T10:00:00+00:00",
            payload={"duration": 5.2},
        )
        line = event.to_jsonl_line()
        restored = ProviderEvent.from_jsonl_line(line)
        assert restored == event

    def test_roundtrip_all_event_types(self) -> None:
        for event_type in ProviderEventType:
            event = ProviderEvent(
                run_id="r",
                task_id="t",
                attempt_number=0,
                provider="claude",
                role="executor",
                event_type=event_type,
                timestamp="2026-03-20T10:00:00+00:00",
            )
            restored = ProviderEvent.from_jsonl_line(event.to_jsonl_line())
            assert restored.event_type == event_type


class TestMakeEvent:
    def test_factory_creates_event(self) -> None:
        event = make_event(
            run_id="run-1",
            task_id="task-1",
            attempt_number=0,
            provider="claude",
            role="executor",
            review_round=0,
            event_type=ProviderEventType.AGENT_STARTED,
        )
        assert isinstance(event, ProviderEvent)
        assert event.payload == {}

    def test_factory_with_payload(self) -> None:
        event = make_event(
            run_id="run-1",
            task_id="task-1",
            attempt_number=0,
            provider="codex",
            role="reviewer",
            review_round=1,
            event_type=ProviderEventType.AGENT_COMMENTARY,
            payload={"text": "checking spec compliance"},
        )
        assert event.payload["text"] == "checking spec compliance"

    def test_factory_auto_timestamps(self) -> None:
        event = make_event(
            run_id="r",
            task_id="t",
            attempt_number=0,
            provider="claude",
            role="executor",
            review_round=0,
            event_type=ProviderEventType.AGENT_STARTED,
        )
        assert event.timestamp  # non-empty
