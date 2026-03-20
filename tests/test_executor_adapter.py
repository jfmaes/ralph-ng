import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.claude_executor import ClaudeExecutorAdapter


def make_stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_executor_parses_streaming_json_and_extracts_tokens(tmp_path: Path) -> None:
    stdout = make_stream(
        {"type": "message_start", "usage": {"input_tokens": 12, "output_tokens": 0}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Applied "}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "changes"}},
        {"type": "message_delta", "usage": {"input_tokens": 12, "output_tokens": 34}},
    )
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    with patch(
        "ralph_ng.adapters.claude_executor.subprocess.run",
        return_value=completed,
    ):
        adapter = ClaudeExecutorAdapter(model="sonnet")
        result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.output_text == "Applied changes"
    assert result.input_tokens == 12
    assert result.output_tokens == 34
    assert result.exit_status == 0


def test_executor_handles_timeout(tmp_path: Path) -> None:
    timeout = subprocess.TimeoutExpired(cmd=["claude"], timeout=10, output="partial", stderr="")

    with patch(
        "ralph_ng.adapters.claude_executor.subprocess.run",
        side_effect=timeout,
    ):
        adapter = ClaudeExecutorAdapter(model="sonnet")
        result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.TIMEOUT
    assert result.output_text == "partial"
    assert result.exit_status == -1


def test_executor_handles_rate_limit_response(tmp_path: Path) -> None:
    stdout = make_stream(
        {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "Rate limit exceeded"},
        }
    )
    completed = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout=stdout, stderr="")

    with patch(
        "ralph_ng.adapters.claude_executor.subprocess.run",
        return_value=completed,
    ):
        adapter = ClaudeExecutorAdapter(model="sonnet")
        result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.RATE_LIMITED
    assert "Rate limit exceeded" in (result.error_message or "")


def test_executor_retries_after_malformed_output_with_error_context(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    outputs = [
        subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="not-json\n", stderr=""),
        subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=make_stream(
                {"type": "content_block_delta", "delta": {"text": "retry ok"}},
                {"type": "message_delta", "usage": {"input_tokens": 7, "output_tokens": 9}},
            ),
            stderr="",
        ),
    ]

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return outputs[len(calls) - 1]

    with patch("ralph_ng.adapters.claude_executor.subprocess.run", side_effect=fake_run):
        adapter = ClaudeExecutorAdapter(model="sonnet", max_retries=1)
        result = adapter.execute("apply the task", timeout_seconds=10, cwd=tmp_path)

    assert result.status is InvocationStatus.COMPLETED
    assert result.output_text == "retry ok"
    assert len(calls) == 2
    assert any("malformed stream-json output" in part for part in calls[1])
