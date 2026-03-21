"""Tests for CliCodexTransport."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.cli_codex_transport import CliCodexTransport
from ralph_ng.adapters.streaming import ActivityKind, LiveActivityTracker, StreamResult


def make_jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _fake_stream(stdout_text: str, *, returncode: int = 0, stderr: str = "", timed_out: bool = False):
    """Return a patched stream_subprocess that feeds lines to on_line."""
    def _mock(command, *, cwd=None, timeout_seconds=60, on_line=None, on_stderr_line=None, on_heartbeat=None, cancel_event=None, grace_seconds=5.0):
        if on_line is not None:
            for line in stdout_text.splitlines(keepends=True):
                on_line(line)
        return StreamResult(returncode=returncode, stderr_text=stderr, timed_out=timed_out)
    return _mock


class TestCliCodexTransport:
    def test_successful_invocation(self) -> None:
        stdout = make_jsonl(
            {"type": "thread.started", "thread_id": "th-123"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"decision": "accept"}'},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 30}},
        )

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review this code", timeout_seconds=60.0)

        assert result.status is InvocationStatus.COMPLETED
        assert '{"decision": "accept"}' in result.output_text
        assert result.input_tokens == 50
        assert result.output_tokens == 30

    def test_timeout(self) -> None:
        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream("", timed_out=True, returncode=-9)):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.TIMEOUT
        assert "timed out" in (result.error_message or "").lower()

    def test_error_event(self) -> None:
        stdout = make_jsonl(
            {"type": "error", "message": "Rate limit exceeded"},
        )

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.PROVIDER_ERROR
        assert "Rate limit exceeded" in (result.error_message or "")

    def test_turn_failed_event(self) -> None:
        stdout = make_jsonl(
            {"type": "turn.failed", "error": {"message": "context length exceeded"}},
        )

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.PROVIDER_ERROR
        assert "context length" in (result.error_message or "")

    def test_nonzero_exit_code(self) -> None:
        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream("", returncode=1, stderr="codex: command failed")):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.PROVIDER_ERROR
        assert "command failed" in (result.error_message or "")

    def test_empty_output(self) -> None:
        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream("")):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.MALFORMED_OUTPUT

    def test_tolerates_partial_lines(self) -> None:
        stdout = "not-json-garbage\n" + make_jsonl(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        )

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            result = transport.invoke("review", timeout_seconds=60.0)

        assert result.status is InvocationStatus.COMPLETED
        assert result.output_text == "ok"

    def test_cwd_passed(self) -> None:
        stdout = make_jsonl(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        )
        calls: list[dict] = []

        def capture(command, *, cwd=None, timeout_seconds=60, on_line=None, on_stderr_line=None, on_heartbeat=None, cancel_event=None, grace_seconds=5.0):
            calls.append({"cwd": cwd})
            if on_line is not None:
                for line in stdout.splitlines(keepends=True):
                    on_line(line)
            return StreamResult(returncode=0, stderr_text="", timed_out=False)

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess", side_effect=capture):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            transport.invoke("review", timeout_seconds=60.0, cwd="/tmp/repo")

        assert calls[0]["cwd"] == "/tmp/repo"

    def test_health_check_available(self) -> None:
        with patch("ralph_ng.adapters.cli_codex_transport.shutil.which", return_value="/usr/bin/codex"):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            assert transport.health_check() is True

    def test_health_check_missing(self) -> None:
        with patch("ralph_ng.adapters.cli_codex_transport.shutil.which", return_value=None):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            assert transport.health_check() is False

    def test_command_structure(self) -> None:
        transport = CliCodexTransport(model="gpt-5.3-codex", cli_path="/usr/bin/codex")
        cmd, schema_path = transport._build_command("hello world", None)
        assert cmd == [
            "/usr/bin/codex",
            "exec",
            "--json",
            "--model", "gpt-5.3-codex",
            "--skip-git-repo-check",
            "--yolo",
            "hello world",
        ]
        assert schema_path is None

    def test_command_with_codex_overrides(self) -> None:
        transport = CliCodexTransport(
            model="gpt-5.3-codex",
            reasoning_effort="high",
            service_tier="fast",
        )
        cmd, _ = transport._build_command("review", None)
        assert cmd[:6] == [
            "codex",
            "-c", 'service_tier="fast"',
            "-c", 'model_reasoning_effort="high"',
            "exec",
        ]

    def test_command_with_json_schema(self) -> None:
        transport = CliCodexTransport(model="gpt-5.3-codex")
        schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        cmd, schema_path = transport._build_command("review", schema)
        assert "--output-schema" in cmd
        schema_idx = cmd.index("--output-schema") + 1
        with open(cmd[schema_idx]) as f:
            loaded = json.load(f)
        assert loaded == schema
        # Prompt should still be last arg
        assert cmd[-1] == "review"
        assert schema_path == cmd[schema_idx]

    def test_invoke_cleans_up_schema_file(self) -> None:
        schema_paths: list[Path] = []

        def capture(command, *, cwd=None, timeout_seconds=60, on_line=None, on_stderr_line=None, on_heartbeat=None, cancel_event=None, grace_seconds=5.0):
            schema_idx = command.index("--output-schema") + 1
            schema_path = Path(command[schema_idx])
            schema_paths.append(schema_path)
            assert schema_path.exists()
            if on_line is not None:
                on_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}) + "\n")
            return StreamResult(returncode=0, stderr_text="", timed_out=False)

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess", side_effect=capture):
            transport = CliCodexTransport(model="gpt-5.3-codex")
            transport.invoke("review", timeout_seconds=60.0, json_schema={"type": "object"})

        assert len(schema_paths) == 1
        assert not schema_paths[0].exists()

    def test_event_callback(self) -> None:
        captured: list[dict] = []
        stdout = make_jsonl(
            {"type": "thread.started", "thread_id": "th-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        )

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex", on_event=captured.append)
            transport.invoke("review", timeout_seconds=60.0)

        assert len(captured) == 2
        assert captured[0]["type"] == "thread.started"

    def test_tracker_filters_json_output(self) -> None:
        """Structured JSON output from agent_message should NOT appear as commentary."""
        stdout = make_jsonl(
            {"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}},
            {"type": "item.completed", "item": {"type": "file_change", "file_path": "src/index.ts"}},
            {"type": "item.completed", "item": {
                "type": "agent_message",
                "text": '{"decision":"reject","confidence":0.34,"spec_compliance":"partial"}',
            }},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex", tracker=tracker)
            transport.invoke("review", timeout_seconds=60.0)

        # Structured output should be summarized, not echoed back verbatim.
        recent = tracker.recent_display(3)
        assert recent
        assert any("structured review output received" in e.summary for e in recent)
        # Verify the JSON wasn't pushed verbatim
        with tracker._lock:
            all_events = list(tracker._events)
        for e in all_events:
            assert "decision" not in e.summary
            assert "reject" not in e.summary

    def test_tracker_receives_progress_events(self) -> None:
        stdout = make_jsonl(
            {"type": "thread.started", "thread_id": "th-123"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 45}},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex", tracker=tracker)
            transport.invoke("review", timeout_seconds=60.0)

        recent = tracker.recent(5)
        summaries = [e.summary for e in recent]
        assert "review session started" in summaries
        assert "review turn started" in summaries
        assert "review turn completed" in summaries
        assert any("tokens 1.2K in / 45 out" in s for s in summaries)

    def test_tracker_receives_reasoning(self) -> None:
        """Reasoning items are surfaced as commentary."""
        stdout = make_jsonl(
            {"type": "item.started", "item": {"type": "reasoning", "text": "Let me check the auth module for vulnerabilities"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "The JWT validation looks correct but missing expiry check"}},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex", tracker=tracker)
            transport.invoke("review", timeout_seconds=60.0)

        recent = tracker.recent(3)
        assert len(recent) == 2
        assert "auth module" in recent[0].summary
        assert "JWT validation" in recent[1].summary

    def test_tracker_receives_output_text_delta(self) -> None:
        """Streaming text deltas are surfaced as commentary."""
        stdout = make_jsonl(
            {"type": "response.output_text.delta", "delta": "I need to review the authentication implementation carefully"},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_codex_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliCodexTransport(model="gpt-5.3-codex", tracker=tracker)
            transport.invoke("review", timeout_seconds=60.0)

        recent = tracker.recent(3)
        assert len(recent) == 1
        assert "authentication implementation" in recent[0].summary
