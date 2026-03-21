"""Tests for streaming infrastructure."""

from __future__ import annotations

import sys
import threading
import time

from ralph_ng.adapters.streaming import (
    ActivityEvent,
    ActivityKind,
    LiveActivityTracker,
    StreamResult,
    make_activity,
    stream_subprocess,
)


class TestActivityEvent:
    def test_make_activity_commentary_significant(self) -> None:
        event = make_activity(ActivityKind.COMMENTARY, "Analyzing the auth flow")
        assert event.kind is ActivityKind.COMMENTARY
        assert event.significant is True

    def test_make_activity_progress_significant(self) -> None:
        event = make_activity(ActivityKind.PROGRESS, "review session started")
        assert event.significant is True

    def test_make_activity_tool_not_significant(self) -> None:
        event = make_activity(ActivityKind.TOOL_EDIT, "[Edit] src/main.py")
        assert event.significant is False

    def test_make_activity_bash_not_significant(self) -> None:
        event = make_activity(ActivityKind.TOOL_BASH, "[Bash] npm test")
        assert event.significant is False

    def test_make_activity_error_significant(self) -> None:
        event = make_activity(ActivityKind.ERROR, "Error: something broke")
        assert event.significant is True

    def test_make_activity_read_not_significant(self) -> None:
        event = make_activity(ActivityKind.TOOL_READ, "[Read] src/main.py")
        assert event.significant is False

    def test_frozen(self) -> None:
        event = make_activity(ActivityKind.TOOL_EDIT, "[Edit] foo.py")
        try:
            event.kind = ActivityKind.TOOL_BASH  # type: ignore[misc]
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestLiveActivityTracker:
    def test_push_and_recent(self) -> None:
        tracker = LiveActivityTracker()
        tracker.push(make_activity(ActivityKind.COMMENTARY, "Analyzing auth flow"))
        tracker.push(make_activity(ActivityKind.TOOL_EDIT, "[Edit] a.py"))
        tracker.push(make_activity(ActivityKind.COMMENTARY, "Running tests now"))

        recent = tracker.recent(3)
        assert len(recent) == 2  # Only commentary is significant
        assert recent[0].summary == "Analyzing auth flow"
        assert recent[1].summary == "Running tests now"

    def test_recent_display_falls_back_to_any_activity(self) -> None:
        tracker = LiveActivityTracker()
        tracker.push(make_activity(ActivityKind.TOOL_READ, "[Read] src/main.py"))
        tracker.push(make_activity(ActivityKind.TOOL_BASH, "[Bash] npm test"))

        recent = tracker.recent_display(3)
        assert len(recent) == 2
        assert recent[0].summary == "[Read] src/main.py"
        assert recent[1].summary == "[Bash] npm test"

    def test_recent_respects_limit(self) -> None:
        tracker = LiveActivityTracker()
        for i in range(10):
            tracker.push(make_activity(ActivityKind.COMMENTARY, f"Step {i}"))

        recent = tracker.recent(3)
        assert len(recent) == 3
        assert recent[0].summary == "Step 7"
        assert recent[2].summary == "Step 9"

    def test_clear(self) -> None:
        tracker = LiveActivityTracker()
        tracker.push(make_activity(ActivityKind.COMMENTARY, "thinking"))
        tracker.clear()
        assert tracker.recent(3) == []

    def test_sink_receives_pushed_events(self) -> None:
        tracker = LiveActivityTracker()
        seen = []
        tracker.set_sink(lambda event: seen.append(event.summary))

        tracker.push(make_activity(ActivityKind.COMMENTARY, "Analyzing auth flow"))

        assert seen == ["Analyzing auth flow"]

    def test_thread_safety(self) -> None:
        tracker = LiveActivityTracker()
        errors: list[str] = []

        def writer() -> None:
            for i in range(100):
                tracker.push(make_activity(ActivityKind.COMMENTARY, f"Step {i}"))

        def reader() -> None:
            for _ in range(100):
                recent = tracker.recent(3)
                if len(recent) > 3:
                    errors.append(f"Got {len(recent)} events, expected <= 3")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_maxlen_respected(self) -> None:
        tracker = LiveActivityTracker(maxlen=3)
        for i in range(10):
            tracker.push(make_activity(ActivityKind.COMMENTARY, f"Step {i}"))

        # Only 3 events retained
        recent = tracker.recent(10)
        assert len(recent) == 3

    def test_empty_recent(self) -> None:
        tracker = LiveActivityTracker()
        assert tracker.recent(3) == []


class TestStreamSubprocess:
    def test_captures_stdout_lines(self) -> None:
        lines: list[str] = []
        result = stream_subprocess(
            [sys.executable, "-c", "print('line1'); print('line2')"],
            timeout_seconds=10.0,
            on_line=lines.append,
        )
        assert result.returncode == 0
        assert result.timed_out is False
        assert len(lines) == 2
        assert "line1" in lines[0]
        assert "line2" in lines[1]

    def test_captures_stderr(self) -> None:
        result = stream_subprocess(
            [sys.executable, "-c", "import sys; sys.stderr.write('err\\n')"],
            timeout_seconds=10.0,
        )
        assert "err" in result.stderr_text

    def test_timeout_kills_process(self) -> None:
        lines: list[str] = []
        result = stream_subprocess(
            [sys.executable, "-c", "import time; print('start'); time.sleep(30)"],
            timeout_seconds=1.0,
            on_line=lines.append,
        )
        assert result.timed_out is True
        # returncode should be non-zero (killed)
        assert result.returncode != 0

    def test_timeout_kills_process_group(self) -> None:
        started = time.monotonic()
        result = stream_subprocess(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                    "print('parent-started', flush=True); "
                    "time.sleep(30)"
                ),
            ],
            timeout_seconds=1.0,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert result.returncode != 0
        assert elapsed < 5.0

    def test_nonzero_exit_code(self) -> None:
        result = stream_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            timeout_seconds=10.0,
        )
        assert result.returncode == 42
        assert result.timed_out is False

    def test_cwd_parameter(self, tmp_path) -> None:
        lines: list[str] = []
        result = stream_subprocess(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=str(tmp_path),
            timeout_seconds=10.0,
            on_line=lines.append,
        )
        assert result.returncode == 0
        assert str(tmp_path) in lines[0]

    def test_on_stderr_line_callback(self) -> None:
        stderr_lines: list[str] = []
        result = stream_subprocess(
            [sys.executable, "-c", "import sys; sys.stderr.write('warn1\\nwarn2\\n')"],
            timeout_seconds=10.0,
            on_stderr_line=stderr_lines.append,
        )
        assert len(stderr_lines) == 2
        assert "warn1" in stderr_lines[0]
