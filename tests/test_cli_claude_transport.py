"""Tests for CliClaudeTransport."""

from __future__ import annotations

import json
from unittest.mock import patch

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.cli_claude_transport import CliClaudeTransport
from ralph_ng.adapters.streaming import ActivityKind, LiveActivityTracker, StreamResult


def make_stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _fake_stream(stdout_text: str, *, returncode: int = 0, stderr: str = "", timed_out: bool = False):
    """Return a patched stream_subprocess that feeds lines to on_line."""
    def _mock(command, *, cwd=None, timeout_seconds=10, on_line=None, on_stderr_line=None, on_heartbeat=None, cancel_event=None, grace_seconds=5.0):
        if on_line is not None:
            for line in stdout_text.splitlines(keepends=True):
                on_line(line)
        return StreamResult(returncode=returncode, stderr_text=stderr, timed_out=timed_out)
    return _mock


class TestCliClaudeTransport:
    def test_successful_invocation(self) -> None:
        stdout = make_stream(
            {"type": "message_start", "usage": {"input_tokens": 12, "output_tokens": 0}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
            {"type": "message_delta", "usage": {"input_tokens": 12, "output_tokens": 34}},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess", side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0)

        assert result.status is InvocationStatus.COMPLETED
        assert result.output_text == "Hello world"
        assert result.input_tokens == 12
        assert result.output_tokens == 34
        assert result.exit_status == 0
        assert result.stderr_text == ""

    def test_timeout(self) -> None:
        stdout = make_stream(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout, timed_out=True, returncode=-9)):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=5.0)

        assert result.status is InvocationStatus.TIMEOUT
        assert "partial" in result.output_text
        assert "timed out" in (result.error_message or "").lower()

    def test_malformed_output_empty(self) -> None:
        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream("")):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0)

        assert result.status is InvocationStatus.MALFORMED_OUTPUT

    def test_rate_limit_error(self) -> None:
        stdout = make_stream(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout, returncode=1)):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0)

        assert result.status is InvocationStatus.RATE_LIMITED
        assert "Rate limit exceeded" in (result.error_message or "")

    def test_provider_error_nonzero_exit(self) -> None:
        stdout = make_stream({"type": "content_block_delta", "delta": {"text": "partial"}})

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout, returncode=1, stderr="segfault")):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0)

        assert result.status is InvocationStatus.PROVIDER_ERROR
        assert "segfault" in (result.error_message or "")

    def test_cwd_passed_to_subprocess(self) -> None:
        stdout = make_stream({"type": "content_block_delta", "delta": {"text": "ok"}})
        calls: list[dict] = []

        def capture(command, *, cwd=None, timeout_seconds=10, on_line=None, on_stderr_line=None, on_heartbeat=None, cancel_event=None, grace_seconds=5.0):
            calls.append({"cwd": cwd})
            if on_line is not None:
                for line in stdout.splitlines(keepends=True):
                    on_line(line)
            return StreamResult(returncode=0, stderr_text="", timed_out=False)

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess", side_effect=capture):
            transport = CliClaudeTransport(model="sonnet")
            transport.invoke("test", timeout_seconds=10.0, cwd="/tmp/workdir")

        assert calls[0]["cwd"] == "/tmp/workdir"

    def test_health_check_when_available(self) -> None:
        with patch("ralph_ng.adapters.cli_claude_transport.shutil.which", return_value="/usr/bin/claude"):
            transport = CliClaudeTransport(model="sonnet")
            assert transport.health_check() is True

    def test_health_check_when_missing(self) -> None:
        with patch("ralph_ng.adapters.cli_claude_transport.shutil.which", return_value=None):
            transport = CliClaudeTransport(model="sonnet")
            assert transport.health_check() is False

    def test_command_includes_model_format_verbose_and_permissions(self) -> None:
        transport = CliClaudeTransport(model="opus", cli_path="/usr/bin/claude")
        cmd = transport._build_command("hello world", None)
        assert cmd == [
            "/usr/bin/claude", "-p", "hello world",
            "--model", "opus",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

    def test_command_with_json_schema(self) -> None:
        transport = CliClaudeTransport(model="sonnet")
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        cmd = transport._build_command("test", schema)
        assert "--json-schema" in cmd
        schema_idx = cmd.index("--json-schema") + 1
        loaded = json.loads(cmd[schema_idx])
        assert loaded == schema

    def test_extracts_last_structured_output(self) -> None:
        stdout = make_stream(
            {"type": "result", "structured_output": {"answer": "draft"}},
            {"type": "result", "structured_output": {"answer": "final"}},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0, json_schema={"type": "object"})

        assert result.status is InvocationStatus.COMPLETED
        assert result.output_text == '{"answer":"final"}'

    def test_preserves_string_structured_output(self) -> None:
        stdout = make_stream(
            {"type": "result", "structured_output": '{"answer":"final"}'},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet")
            result = transport.invoke("test", timeout_seconds=10.0, json_schema={"type": "object"})

        assert result.status is InvocationStatus.COMPLETED
        assert result.output_text == '{"answer":"final"}'

    def test_tracker_receives_progress_and_result(self) -> None:
        stdout = make_stream(
            {"type": "message_start", "usage": {"input_tokens": 12, "output_tokens": 0}},
            {"type": "result", "structured_output": {"answer": "42"}},
            {"type": "message_stop"},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0, json_schema={"type": "object"})

        summaries = [e.summary for e in tracker.recent(5)]
        assert "response started" in summaries
        assert any("tokens 12 in / 0 out" in s for s in summaries)
        assert "structured output received" in summaries
        assert "response complete" in summaries

    def test_tracker_handles_real_assistant_tool_use_shape(self) -> None:
        stdout = make_stream(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "I should inspect the config file first."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/repo/pyproject.toml"}},
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 8},
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_result": {
                                "file": {"filePath": "/tmp/repo/pyproject.toml"},
                            },
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "OK"}]},
            },
            {"type": "result", "usage": {"input_tokens": 4, "output_tokens": 20}},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        summaries = [e.summary for e in tracker.recent_display(10)]
        assert any("inspect the config file first" in s for s in summaries)
        with tracker._lock:
            all_summaries = [e.summary for e in tracker._events]
        assert "[Read] /tmp/repo/pyproject.toml" in all_summaries
        assert "read completed /tmp/repo/pyproject.toml" in all_summaries
        assert any("tokens 3 in / 8 out" in s for s in all_summaries)

    def test_tracker_handles_real_system_init_shape(self) -> None:
        stdout = make_stream(
            {
                "type": "system",
                "subtype": "init",
                "mcp_servers": [
                    {"name": "playwright", "status": "connected"},
                    {"name": "calendar", "status": "needs-auth"},
                ],
            },
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        summaries = [e.summary for e in tracker.recent(5)]
        assert "session initialized (1 MCP connected)" in summaries

    def test_event_callback(self) -> None:
        captured: list[dict] = []
        stdout = make_stream(
            {"type": "message_start", "usage": {"input_tokens": 5, "output_tokens": 0}},
            {"type": "content_block_delta", "delta": {"text": "hi"}},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", on_event=captured.append)
            transport.invoke("test", timeout_seconds=10.0)

        assert len(captured) == 2
        assert captured[0]["type"] == "message_start"

    def test_tracker_reports_unexpected_raw_output(self) -> None:
        stdout = "not-json-at-all\n" + make_stream(
            {"type": "message_start", "usage": {"input_tokens": 1, "output_tokens": 0}},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            result = transport.invoke("test", timeout_seconds=10.0)

        assert result.status is InvocationStatus.COMPLETED
        summaries = [e.summary for e in tracker.recent_display(5)]
        assert any("unexpected claude output" in s for s in summaries)

    def test_tracker_receives_tool_activity(self) -> None:
        """Real Claude stream-json format: content_block_start/delta/stop sequence."""
        stdout = make_stream(
            # Edit tool
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "Edit"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "src/main.py", "old_string": "x", "new_string": "y"}'}},
            {"type": "content_block_stop", "index": 0},
            # Bash tool
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t2", "name": "Bash"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command": "npm test"}'}},
            {"type": "content_block_stop", "index": 1},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        # Tool calls are NOT significant (commentary is)
        recent = tracker.recent(3)
        assert len(recent) == 0
        # But they are tracked in the full event list
        with tracker._lock:
            all_events = list(tracker._events)
        assert len(all_events) == 2
        assert "[Edit] src/main.py" in all_events[0].summary
        assert "[Bash] npm test" in all_events[1].summary

    def test_tracker_receives_commentary_large_chunk(self) -> None:
        """A single large text_delta (>= 40 chars) is flushed immediately."""
        stdout = make_stream(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me analyze the authentication flow in this codebase"}},
            {"type": "content_block_stop", "index": 0},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        recent = tracker.recent(3)
        assert len(recent) == 1
        assert "authentication flow" in recent[0].summary

    def test_tracker_accumulates_small_text_chunks(self) -> None:
        """Small text_delta chunks are accumulated and flushed at block boundary."""
        stdout = make_stream(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "check "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "the auth"}},
            {"type": "content_block_stop", "index": 0},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        recent = tracker.recent(3)
        assert len(recent) == 1
        assert "Let me check the auth" in recent[0].summary

    def test_tracker_multi_chunk_tool_input(self) -> None:
        """Tool input JSON arrives in multiple delta chunks — tracked but not significant."""
        stdout = make_stream(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "Edit"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'path": "src/auth.ts"}'}},
            {"type": "content_block_stop", "index": 0},
        )
        tracker = LiveActivityTracker()

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet", tracker=tracker)
            transport.invoke("test", timeout_seconds=10.0)

        # Tool calls are tracked but not significant
        assert tracker.recent(3) == []
        with tracker._lock:
            all_events = list(tracker._events)
        assert len(all_events) == 1
        assert "[Edit] src/auth.ts" in all_events[0].summary

    def test_session_id_captured_from_result(self) -> None:
        """Session ID is extracted from stream events for --resume."""
        stdout = make_stream(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
            {"type": "result", "session_id": "sess-abc123"},
        )

        with patch("ralph_ng.adapters.cli_claude_transport.stream_subprocess",
                    side_effect=_fake_stream(stdout)):
            transport = CliClaudeTransport(model="sonnet")
            assert transport.session_id is None
            transport.invoke("test", timeout_seconds=10.0)

        assert transport.session_id == "sess-abc123"

    def test_resume_flag_when_session_exists(self) -> None:
        """_build_command includes --resume when session_id is set."""
        transport = CliClaudeTransport(model="sonnet", cli_path="claude")
        transport.session_id = "sess-abc123"
        cmd = transport._build_command("continue work", None)
        assert "--resume" in cmd
        resume_idx = cmd.index("--resume") + 1
        assert cmd[resume_idx] == "sess-abc123"
        assert "-p" in cmd
        assert "continue work" in cmd

    def test_no_resume_flag_without_session(self) -> None:
        """_build_command does NOT include --resume when no session_id."""
        transport = CliClaudeTransport(model="sonnet", cli_path="claude")
        cmd = transport._build_command("fresh start", None)
        assert "--resume" not in cmd

    def test_clear_session(self) -> None:
        """clear_session resets the session ID."""
        transport = CliClaudeTransport(model="sonnet")
        transport.session_id = "sess-abc123"
        transport.clear_session()
        assert transport.session_id is None
