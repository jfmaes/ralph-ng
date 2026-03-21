import json
from pathlib import Path

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.claude_executor import ClaudeExecutorAdapter
from ralph_ng.adapters.transport import Transport, TransportResult


class FakeTransport(Transport):
    """Test transport that returns pre-configured responses."""

    def __init__(self, responses: list[TransportResult] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict | None = None,
    ) -> TransportResult:
        self.calls.append({"prompt": prompt, "timeout_seconds": timeout_seconds, "cwd": cwd})
        return self._responses.pop(0)

    def health_check(self) -> bool:
        return True


def test_executor_parses_streaming_json_and_extracts_tokens(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            TransportResult(
                status=InvocationStatus.COMPLETED,
                output_text="Applied changes",
                input_tokens=12,
                output_tokens=34,
                duration_seconds=1.0,
            )
        ]
    )
    adapter = ClaudeExecutorAdapter(transport=transport)
    result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.output_text == "Applied changes"
    assert result.input_tokens == 12
    assert result.output_tokens == 34


def test_executor_handles_timeout(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            TransportResult(
                status=InvocationStatus.TIMEOUT,
                output_text="partial",
                duration_seconds=10.0,
                error_message="Claude CLI timed out after 10s",
            )
        ]
    )
    adapter = ClaudeExecutorAdapter(transport=transport)
    result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.TIMEOUT
    assert result.output_text == "partial"
    assert result.exit_status == -1


def test_executor_handles_rate_limit_response(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            TransportResult(
                status=InvocationStatus.RATE_LIMITED,
                duration_seconds=0.5,
                error_message="Rate limit exceeded",
            )
        ]
    )
    adapter = ClaudeExecutorAdapter(transport=transport)
    result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.RATE_LIMITED
    assert "Rate limit exceeded" in (result.error_message or "")


def test_executor_retries_after_malformed_output_with_error_context(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            TransportResult(
                status=InvocationStatus.MALFORMED_OUTPUT,
                duration_seconds=0.5,
                error_message="Invalid stream-json line",
            ),
            TransportResult(
                status=InvocationStatus.COMPLETED,
                output_text="retry ok",
                input_tokens=7,
                output_tokens=9,
                duration_seconds=1.0,
            ),
        ]
    )
    adapter = ClaudeExecutorAdapter(transport=transport, max_retries=1)
    result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.output_text == "retry ok"
    assert len(transport.calls) == 2
    assert "malformed stream-json output" in transport.calls[1]["prompt"]
