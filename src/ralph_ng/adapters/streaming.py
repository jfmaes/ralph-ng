"""Shared streaming infrastructure for CLI transports.

Provides line-by-line subprocess streaming via Popen and a thread-safe
LiveActivityTracker that bridges transport threads with the TUI renderer.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class ActivityKind(StrEnum):
    PROGRESS = "progress"
    TOOL_EDIT = "tool_edit"
    TOOL_WRITE = "tool_write"
    TOOL_BASH = "tool_bash"
    TOOL_READ = "tool_read"
    TOOL_GREP = "tool_grep"
    TOOL_OTHER = "tool_other"
    COMMENTARY = "commentary"
    ERROR = "error"
    RESULT = "result"


# Which kinds are "significant" — shown in the activity panel.
# Commentary (CoT / reasoning) is what operators care about most.
# Tool calls are noise; errors always matter.
_SIGNIFICANT_KINDS = frozenset({
    ActivityKind.PROGRESS,
    ActivityKind.COMMENTARY,
    ActivityKind.ERROR,
    ActivityKind.RESULT,
})


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """A single activity observation from a streaming transport."""

    kind: ActivityKind
    summary: str
    significant: bool


def make_activity(kind: ActivityKind, summary: str) -> ActivityEvent:
    """Create an ActivityEvent, auto-setting significance from kind."""
    return ActivityEvent(
        kind=kind,
        summary=summary,
        significant=kind in _SIGNIFICANT_KINDS,
    )


class LiveActivityTracker:
    """Thread-safe bridge: transports push events, renderer reads them.

    Holds a bounded deque of recent ActivityEvents.
    """

    def __init__(self, maxlen: int = 10) -> None:
        self._events: deque[ActivityEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._sink: Callable[[ActivityEvent], None] | None = None

    def push(self, event: ActivityEvent) -> None:
        with self._lock:
            self._events.append(event)
            sink = self._sink
        if sink is not None:
            sink(event)

    def recent(self, n: int = 3) -> list[ActivityEvent]:
        """Return the last *n* significant events, most-recent last."""
        with self._lock:
            significant = [e for e in self._events if e.significant]
        return significant[-n:]

    def recent_display(self, n: int = 3) -> list[ActivityEvent]:
        """Return operator-facing activity, preferring textual output.

        Preference order:
        1. Commentary / result / error text
        2. Progress updates
        3. Raw activity fallback (tool calls)
        """
        with self._lock:
            events = list(self._events)

        textual = [
            event for event in events
            if event.kind in (ActivityKind.COMMENTARY, ActivityKind.RESULT, ActivityKind.ERROR)
        ]
        if textual:
            return textual[-n:]

        progress = [event for event in events if event.kind == ActivityKind.PROGRESS]
        if progress:
            return progress[-n:]

        return events[-n:]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def set_sink(self, sink: Callable[[ActivityEvent], None] | None) -> None:
        with self._lock:
            self._sink = sink


@dataclass(slots=True)
class StreamResult:
    """Result of a streamed subprocess execution."""

    returncode: int
    stderr_text: str
    timed_out: bool
    cancelled: bool = False


def stream_subprocess(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout_seconds: float,
    on_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    on_heartbeat: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
    grace_seconds: float = 5.0,
) -> StreamResult:
    """Run *command* and stream stdout line-by-line.

    * ``on_line`` is called for each stdout line in the calling thread.
    * ``on_heartbeat`` is called on each stdout line to update liveness.
    * ``cancel_event`` is checked periodically; when set, the subprocess is
      terminated gracefully (SIGTERM → grace period → SIGKILL).
    * stderr is drained in a background thread to prevent pipe deadlock.
    * Timeout uses SIGTERM first, then SIGKILL after ``grace_seconds``.

    Returns a ``StreamResult`` with returncode, collected stderr, and
    whether the process was killed due to timeout.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        cwd=cwd,
        start_new_session=(os.name != "nt"),
    )

    timed_out = False
    cancelled = False

    def _graceful_kill(*, is_cancel: bool = False) -> None:
        """SIGTERM first, then SIGKILL after grace period."""
        nonlocal timed_out, cancelled
        if is_cancel:
            cancelled = True
        else:
            timed_out = True
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        # Wait for graceful exit, then force kill
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    timer = threading.Timer(timeout_seconds, _graceful_kill)
    timer.start()

    # Drain stderr in background thread
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line)
            if on_stderr_line is not None:
                on_stderr_line(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Read stdout line-by-line in calling thread
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if on_heartbeat is not None:
                on_heartbeat()
            if on_line is not None:
                on_line(line)
            # Check for cancellation between lines
            if cancel_event is not None and cancel_event.is_set():
                _graceful_kill(is_cancel=True)
                break
    finally:
        process.wait()
        timer.cancel()
        stderr_thread.join(timeout=5.0)

    return StreamResult(
        returncode=process.returncode,
        stderr_text="".join(stderr_lines),
        timed_out=timed_out,
        cancelled=cancelled,
    )
