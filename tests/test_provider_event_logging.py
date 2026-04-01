"""Tests for provider event persistence."""

from __future__ import annotations

from pathlib import Path

from ralph_ng.providers.event_store import ProviderEventStore
from ralph_ng.providers.events import ProviderEvent, ProviderEventType, make_event


def _make_event(
    *,
    run_id: str = "run-1",
    task_id: str = "task-1",
    attempt_number: int = 1,
    provider: str = "claude",
    role: str = "executor",
    review_round: int = 0,
    event_type: ProviderEventType = ProviderEventType.AGENT_STARTED,
    payload: dict | None = None,
) -> ProviderEvent:
    return make_event(
        run_id=run_id,
        task_id=task_id,
        attempt_number=attempt_number,
        provider=provider,
        role=role,
        review_round=review_round,
        event_type=event_type,
        payload=payload,
    )


class TestProviderEventStore:
    def test_log_and_read_roundtrip(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        event = _make_event(event_type=ProviderEventType.AGENT_STARTED)
        store.log(event)

        events = store.read_events("run-1", "task-1", 1)
        assert len(events) == 1
        assert events[0].event_type == ProviderEventType.AGENT_STARTED
        assert events[0].run_id == "run-1"

    def test_multiple_events_same_attempt(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(event_type=ProviderEventType.AGENT_STARTED))
        store.log(_make_event(event_type=ProviderEventType.AGENT_COMMENTARY, payload={"text": "working"}))
        store.log(_make_event(event_type=ProviderEventType.AGENT_FINISHED))

        events = store.read_events("run-1", "task-1", 1)
        assert len(events) == 3
        assert events[0].event_type == ProviderEventType.AGENT_STARTED
        assert events[1].event_type == ProviderEventType.AGENT_COMMENTARY
        assert events[2].event_type == ProviderEventType.AGENT_FINISHED

    def test_separate_attempts(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(attempt_number=1, event_type=ProviderEventType.AGENT_STARTED))
        store.log(_make_event(attempt_number=2, event_type=ProviderEventType.AGENT_STARTED))

        assert len(store.read_events("run-1", "task-1", 1)) == 1
        assert len(store.read_events("run-1", "task-1", 2)) == 1

    def test_separate_tasks(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(task_id="task-1"))
        store.log(_make_event(task_id="task-2"))

        assert len(store.read_events("run-1", "task-1", 1)) == 1
        assert len(store.read_events("run-1", "task-2", 1)) == 1

    def test_read_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        events = store.read_events("no-run", "no-task", 1)
        assert events == []

    def test_list_attempts(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(attempt_number=1))
        store.log(_make_event(attempt_number=3))

        attempts = store.list_attempts("run-1", "task-1")
        assert attempts == [1, 3]

    def test_list_attempts_empty(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        assert store.list_attempts("no-run", "no-task") == []

    def test_file_layout(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(run_id="run-42", task_id="task-7", attempt_number=2))

        expected = tmp_path / "runs" / "run-42" / "task-7" / "attempt_2" / "provider_events.jsonl"
        assert expected.exists()

    def test_tolerates_corrupt_lines(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event())

        # Append a corrupt line
        path = tmp_path / "runs" / "run-1" / "task-1" / "attempt_1" / "provider_events.jsonl"
        with path.open("a") as fh:
            fh.write("not-valid-json\n")
            fh.write("{}\n")  # valid JSON but invalid ProviderEvent

        events = store.read_events("run-1", "task-1", 1)
        assert len(events) == 1  # only the valid event

    def test_review_round_preserved(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event(role="reviewer", review_round=2, provider="codex"))

        events = store.read_events("run-1", "task-1", 1)
        assert events[0].review_round == 2
        assert events[0].provider == "codex"
        assert events[0].role == "reviewer"

    def test_tolerates_invalid_utf8_bytes(self, tmp_path: Path) -> None:
        store = ProviderEventStore(tmp_path)
        store.log(_make_event())

        path = tmp_path / "runs" / "run-1" / "task-1" / "attempt_1" / "provider_events.jsonl"
        with path.open("ab") as fh:
            fh.write(b"\x89PNG\r\n")

        events = store.read_events("run-1", "task-1", 1)
        assert len(events) == 1
