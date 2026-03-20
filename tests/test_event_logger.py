from pathlib import Path

from ralph_ng.events import EventLogger


def test_events_are_appended_with_consistent_schema(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "events.jsonl")

    logger.log("run.started", data={"run_id": "run-001"})
    logger.log("task.transition", task_id="TASK-001", data={"state": "ready"})

    events = logger.read_events()

    assert len(events) == 2
    assert events[0].event_type == "run.started"
    assert events[1].task_id == "TASK-001"
    assert events[1].data["state"] == "ready"
    assert events[0].timestamp


def test_event_log_can_be_tailed_while_writing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path)
    logger.log("run.started", data={"run_id": "run-001"})

    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
        logger.log("run.stopped", data={"reason": "completed"})
        second_line = handle.readline()

    assert "run.started" in first_line
    assert "run.stopped" in second_line


def test_event_reader_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "{\"timestamp\":\"t\",\"event_type\":\"run.started\",\"task_id\":null,\"data\":{}}\n"
        "{\"timestamp\":\"broken\",\n",
        encoding="utf-8",
    )

    events = EventLogger(path).read_events()

    assert len(events) == 1
    assert events[0].event_type == "run.started"
