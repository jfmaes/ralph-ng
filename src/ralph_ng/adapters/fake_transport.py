"""Fake transport for testing — replays canned responses without real CLIs."""

from __future__ import annotations

import time
from typing import Any

from .base import InvocationStatus
from .transport import Transport, TransportResult


class FakeTransport(Transport):
    """Provider-neutral fake transport for adapter and orchestrator tests.

    Replays a sequence of ``TransportResult`` objects.  Optionally simulates
    latency or error injection.
    """

    def __init__(
        self,
        responses: list[TransportResult] | None = None,
        *,
        latency_seconds: float = 0.0,
        healthy: bool = True,
    ) -> None:
        self._responses: list[TransportResult] = list(responses or [])
        self._latency = latency_seconds
        self._healthy = healthy
        self.calls: list[dict[str, Any]] = []

    def add_response(self, response: TransportResult) -> None:
        """Append a response to the replay queue."""
        self._responses.append(response)

    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> TransportResult:
        self.calls.append(
            {
                "prompt": prompt,
                "timeout_seconds": timeout_seconds,
                "cwd": cwd,
                "json_schema": json_schema,
            }
        )

        if not self._responses:
            return TransportResult(
                status=InvocationStatus.PROVIDER_ERROR,
                duration_seconds=0.0,
                error_message="FakeTransport: no more responses in queue",
            )

        if self._latency > 0:
            if self._latency > timeout_seconds:
                return TransportResult(
                    status=InvocationStatus.TIMEOUT,
                    duration_seconds=timeout_seconds,
                    error_message=f"FakeTransport: simulated timeout after {timeout_seconds}s",
                )
            time.sleep(self._latency)

        return self._responses.pop(0)

    def health_check(self) -> bool:
        return self._healthy

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_prompt(self) -> str | None:
        if not self.calls:
            return None
        return self.calls[-1]["prompt"]


def make_completed_result(
    output_text: str = "ok",
    *,
    input_tokens: int = 10,
    output_tokens: int = 20,
    duration_seconds: float = 1.0,
    raw_events: list[dict[str, Any]] | None = None,
) -> TransportResult:
    """Convenience factory for a successful TransportResult."""
    return TransportResult(
        status=InvocationStatus.COMPLETED,
        output_text=output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_seconds=duration_seconds,
        raw_events=raw_events or [],
    )


def make_error_result(
    status: InvocationStatus = InvocationStatus.PROVIDER_ERROR,
    error_message: str = "simulated error",
    *,
    output_text: str = "",
    duration_seconds: float = 0.5,
) -> TransportResult:
    """Convenience factory for an error TransportResult."""
    return TransportResult(
        status=status,
        output_text=output_text,
        duration_seconds=duration_seconds,
        error_message=error_message,
    )
