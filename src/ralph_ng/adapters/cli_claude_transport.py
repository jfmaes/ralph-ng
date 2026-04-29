"""CLI Claude transport — subprocess-based Transport implementation.

Uses ``stream_subprocess`` for line-by-line streaming so the TUI can
show live tool-use activity while Claude is running.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .base import InvocationStatus, _extract_text, _find_error, _find_usage
from .streaming import ActivityKind, LiveActivityTracker, make_activity, stream_subprocess
from .transport import ProviderEventCallback, Transport, TransportResult


# Map Claude tool names to ActivityKind.
_TOOL_KIND_MAP: dict[str, ActivityKind] = {
    "Edit": ActivityKind.TOOL_EDIT,
    "Write": ActivityKind.TOOL_WRITE,
    "Bash": ActivityKind.TOOL_BASH,
    "Read": ActivityKind.TOOL_READ,
    "Grep": ActivityKind.TOOL_GREP,
    "Glob": ActivityKind.TOOL_GREP,
}


def _summarize_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a human-readable summary line for a tool invocation."""
    if tool_name in ("Edit", "Write"):
        return f"[{tool_name}] {tool_input.get('file_path', '')}"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"[Bash] {cmd[:60]}"
    if tool_name in ("Read", "Grep", "Glob"):
        target = tool_input.get("file_path", "") or tool_input.get("pattern", "")
        return f"[{tool_name}] {target}"
    return f"[{tool_name}]"


class CliClaudeTransport(Transport):
    """Invoke Claude CLI in stream-json mode with live activity streaming.

    This transport spawns ``claude -p <prompt> --model <model> --output-format stream-json``
    and parses the JSONL output line-by-line, emitting ActivityEvents as tools are used.
    """

    def __init__(
        self,
        *,
        model: str,
        cli_path: str = "claude",
        on_event: ProviderEventCallback | None = None,
        tracker: LiveActivityTracker | None = None,
    ) -> None:
        self.model = model
        self.cli_path = cli_path
        self._on_event = on_event
        self.tracker = tracker
        # State for accumulating tool-use input across content_block_delta events
        self._pending_tool_name: str = ""
        self._pending_tool_input_chunks: list[str] = []
        # Buffer for accumulating text_delta chunks into readable commentary
        self._text_buffer: list[str] = []
        self._thought_counter = 0
        self._current_thought_id: str | None = None
        # Session ID for --resume (set after each successful invocation)
        self.session_id: str | None = None
        # Cancel event for graceful mid-stream interruption
        self.cancel_event: __import__("threading").Event | None = None

    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> TransportResult:
        # Linux caps a single argv entry at MAX_ARG_STRLEN (128 KiB by
        # default), so passing the prompt via `-p <prompt>` blows up with
        # E2BIG once the prompt includes a long plan body + diff + review
        # context. `claude -p` without a positional prompt reads from stdin,
        # so for large prompts we write to a tempfile and hand it to the
        # subprocess via stdin_path. Small prompts keep the legacy argv
        # path so existing tests (which mock stream_subprocess without
        # stdin_path support) continue to work.
        _STDIN_PROMPT_THRESHOLD = 100 * 1024  # 100 KiB, well under 128 KiB
        use_stdin_prompt = len(prompt.encode("utf-8")) > _STDIN_PROMPT_THRESHOLD
        command = self._build_command(
            None if use_stdin_prompt else prompt, json_schema
        )
        workdir = str(Path(cwd)) if cwd is not None else None
        prompt_path: str | None = None
        if use_stdin_prompt:
            import tempfile as _tempfile, os as _os
            prompt_fd, prompt_path = _tempfile.mkstemp(
                prefix="ralph-claude-prompt-", suffix=".txt"
            )
            try:
                with _os.fdopen(prompt_fd, "w", encoding="utf-8") as _pf:
                    _pf.write(prompt)
            except Exception:
                try:
                    _os.unlink(prompt_path)
                except OSError:
                    pass
                raise

        # Reset accumulation state
        self._pending_tool_name = ""
        self._pending_tool_input_chunks = []
        self._text_buffer = []
        self._current_thought_id = None

        # Accumulated state from streaming
        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        error_type: str | None = None
        error_message: str | None = None
        events: list[dict[str, Any]] = []
        captured_session_id: list[str] = []  # mutable container for closure
        unexpected_lines: list[str] = []

        def _on_line(raw_line: str) -> None:
            nonlocal input_tokens, output_tokens, error_type, error_message

            line = raw_line.strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                unexpected_lines.append(line)
                self._report_unexpected_output(line)
                return

            if not isinstance(event, dict):
                unexpected_lines.append(line)
                self._report_unexpected_output(line)
                return

            events.append(event)

            # Extract session_id from result event for --resume
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                captured_session_id.clear()
                captured_session_id.append(sid)

            # Extract text
            text_parts.extend(_extract_text(event))

            # Extract usage
            usage = _find_usage(event)
            if usage is not None:
                input_tokens = usage.get("input_tokens", input_tokens)
                output_tokens = usage.get("output_tokens", output_tokens)

            # Extract errors
            evt_error_type, evt_error_message = _find_error(event)
            if evt_error_type is not None:
                error_type = evt_error_type
                error_message = evt_error_message

            # Extract tool activity for live tracker
            if self.tracker is not None:
                try:
                    self._extract_activity(event)
                except Exception as exc:
                    self._report_tracker_error(exc, str(event.get("type", "")))

            # Emit raw event for persistence
            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception as exc:
                    self._report_tracker_error(exc, f"persist {event.get('type', '')}")

        started_at = time.monotonic()
        try:
            stream_kwargs: dict[str, Any] = dict(
                cwd=workdir,
                timeout_seconds=timeout_seconds,
                on_line=_on_line,
                cancel_event=self.cancel_event,
            )
            if prompt_path is not None:
                stream_kwargs["stdin_path"] = prompt_path
            stream_result = stream_subprocess(command, **stream_kwargs)
        finally:
            if prompt_path is not None:
                try:
                    import os as _os2
                    _os2.unlink(prompt_path)
                except OSError:
                    pass
        duration = time.monotonic() - started_at

        # Persist session_id for potential --resume on next call
        if captured_session_id:
            self.session_id = captured_session_id[0]

        if stream_result.timed_out:
            return TransportResult(
                status=InvocationStatus.TIMEOUT,
                output_text="".join(text_parts),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=duration,
                error_message=f"Claude CLI timed out after {timeout_seconds}s",
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        # When --json-schema is used, output lands in structured_output
        output_text = "".join(text_parts)
        if not output_text:
            output_text = self._extract_structured_output(events)

        # If no events were parsed at all, treat it as malformed
        if not events and not output_text:
            return TransportResult(
                status=InvocationStatus.MALFORMED_OUTPUT,
                output_text="\n".join(unexpected_lines),
                duration_seconds=duration,
                error_message="Claude produced no parseable output",
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
            )

        # Classify errors
        classified = self._classify_error(
            error_type,
            error_message,
            stream_result.stderr_text,
            stream_result.returncode,
        )
        if classified is not None:
            return TransportResult(
                status=classified[0],
                output_text=output_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=duration,
                error_message=classified[1],
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        return TransportResult(
            status=InvocationStatus.COMPLETED,
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration,
            exit_status=stream_result.returncode,
            stderr_text=stream_result.stderr_text,
            raw_events=events,
        )

    def _report_unexpected_output(self, line: str) -> None:
        if self.tracker is not None:
            snippet = line[:160]
            self.tracker.push(make_activity(
                ActivityKind.PROGRESS,
                f"unexpected claude output: {snippet}",
                text=f"unexpected claude output: {snippet}",
            ))

    def _report_tracker_error(self, exc: Exception, context: str) -> None:
        if self.tracker is not None:
            self.tracker.push(make_activity(
                ActivityKind.ERROR,
                f"claude stream handling error ({context}): {exc}",
                text=f"claude stream handling error ({context}): {exc}",
            ))

    def health_check(self) -> bool:
        """Check if the claude CLI is available on PATH."""
        return shutil.which(self.cli_path) is not None

    def _start_thought(self) -> None:
        self._thought_counter += 1
        self._current_thought_id = f"claude-thought-{self._thought_counter}"

    def _flush_text_buffer(self, *, final: bool = False) -> None:
        """Flush accumulated text_delta chunks as a commentary event."""
        if not self._text_buffer or self.tracker is None:
            return
        full = "".join(self._text_buffer).strip()
        self._text_buffer = []
        if not full:
            return
        self._push_reasoning(full, final=final)

    def _push_reasoning(
        self,
        text: str,
        *,
        final: bool = False,
        force_new: bool = False,
    ) -> None:
        """Push commentary text as a grouped thought block."""
        assert self.tracker is not None
        if not text:
            return
        stripped = text.strip()
        if not stripped:
            return
        if force_new or self._current_thought_id is None:
            self._start_thought()

        summary = stripped
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if line:
                summary = line
                break

        block_state = "update"
        if force_new:
            block_state = "start"
        elif final:
            block_state = "finish"

        self.tracker.push(make_activity(
            ActivityKind.COMMENTARY,
            summary[:80],
            text=stripped,
            block_id=self._current_thought_id,
            block_state=block_state,
        ))
        if final:
            self._current_thought_id = None

    def _extract_activity(self, event: dict[str, Any]) -> None:
        """Extract tool-use and commentary activity from Claude stream-json events.

        Claude stream-json emits tool use as a sequence:
          content_block_start  → {content_block: {type: "tool_use", name: "Edit"}}
          content_block_delta  → {delta: {type: "input_json_delta", partial_json: "..."}}
          content_block_stop   → finalize tool, parse accumulated input

        Text commentary (CoT) arrives as many small text_delta chunks that
        are accumulated in ``_text_buffer`` and flushed when a block boundary
        is reached or the buffer grows large enough.
        """
        assert self.tracker is not None
        event_type = event.get("type", "")

        if event_type == "system":
            self._extract_system_activity(event)
            self._push_usage_progress(event)
            return

        if event_type == "assistant":
            self._extract_assistant_activity(event)
            self._push_usage_progress(event.get("message", {}))
            return

        if event_type == "user":
            self._extract_user_activity(event)
            return

        # ── Tool use: start ──────────────────────────────────────────
        if event_type == "message_start":
            self.tracker.push(make_activity(ActivityKind.PROGRESS, "response started"))

        elif event_type == "content_block_start":
            # Flush any pending text before starting a new block
            self._flush_text_buffer()
            cb = event.get("content_block", {})
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                self._pending_tool_name = cb.get("name", "")
                self._pending_tool_input_chunks = []
            elif isinstance(cb, dict) and cb.get("type") == "text":
                self._start_thought()
                self.tracker.push(make_activity(ActivityKind.PROGRESS, "thinking"))

        # ── Tool use: accumulate input / CoT text ────────────────────
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            if isinstance(delta, dict):
                delta_type = delta.get("type", "")

                # Tool input JSON fragments
                if delta_type == "input_json_delta" and self._pending_tool_name:
                    chunk = delta.get("partial_json", "")
                    if chunk:
                        self._pending_tool_input_chunks.append(chunk)

                # CoT / commentary text — accumulate small chunks
                elif delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        self._text_buffer.append(text)
                        # Flush on sentence boundaries or when buffer is large enough
                        if "\n" in text:
                            self._flush_text_buffer()
                        else:
                            buffered = "".join(self._text_buffer)
                            if len(buffered) >= 40:
                                self._flush_text_buffer()

        # ── Block end: finalize tool or flush text ────────────────────
        elif event_type == "content_block_stop":
            if self._pending_tool_name:
                tool_name = self._pending_tool_name
                kind = _TOOL_KIND_MAP.get(tool_name, ActivityKind.TOOL_OTHER)

                # Try to parse accumulated input for file paths / commands
                tool_input: dict[str, Any] = {}
                if self._pending_tool_input_chunks:
                    try:
                        tool_input = json.loads("".join(self._pending_tool_input_chunks))
                    except json.JSONDecodeError:
                        pass
                    if not isinstance(tool_input, dict):
                        tool_input = {}

                summary = _summarize_tool(tool_name, tool_input)
                self.tracker.push(make_activity(kind, summary))

                self._pending_tool_name = ""
                self._pending_tool_input_chunks = []
            else:
                # End of a text block — flush remaining text
                self._flush_text_buffer(final=True)

        # ── Message boundaries ────────────────────────────────────────
        elif event_type in ("message_stop", "message_delta"):
            self._flush_text_buffer(final=(event_type == "message_stop"))
            if event_type == "message_stop":
                self.tracker.push(make_activity(ActivityKind.RESULT, "response complete"))

        # ── Error events ─────────────────────────────────────────────
        elif event_type == "error":
            msg = ""
            err = event.get("error", event)
            if isinstance(err, dict):
                msg = err.get("message", "")
            self.tracker.push(make_activity(ActivityKind.ERROR, f"Error: {msg[:80]}"))

        elif event_type == "result":
            if event.get("structured_output") is not None:
                self.tracker.push(make_activity(ActivityKind.RESULT, "structured output received"))

        self._push_usage_progress(event)

    def _extract_system_activity(self, event: dict[str, Any]) -> None:
        assert self.tracker is not None
        subtype = event.get("subtype", "")
        if subtype == "init":
            mcp_servers = event.get("mcp_servers", [])
            if isinstance(mcp_servers, list):
                connected = sum(
                    1 for server in mcp_servers
                    if isinstance(server, dict) and server.get("status") == "connected"
                )
                self.tracker.push(make_activity(
                    ActivityKind.PROGRESS,
                    f"session initialized ({connected} MCP connected)",
                ))
            else:
                self.tracker.push(make_activity(ActivityKind.PROGRESS, "session initialized"))
        elif subtype == "hook_started":
            hook_name = event.get("hook_name", "hook")
            self.tracker.push(make_activity(ActivityKind.PROGRESS, f"{hook_name} started"))
        elif subtype == "hook_response":
            hook_name = event.get("hook_name", "hook")
            self.tracker.push(make_activity(ActivityKind.PROGRESS, f"{hook_name} finished"))

    def _extract_assistant_activity(self, event: dict[str, Any]) -> None:
        assert self.tracker is not None
        message = event.get("message", {})
        if not isinstance(message, dict):
            return
        content = message.get("content", [])
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "thinking":
                self._push_reasoning(block.get("thinking", ""), final=True, force_new=True)
            elif block_type == "tool_use":
                tool_name = block.get("name", "")
                kind = _TOOL_KIND_MAP.get(tool_name, ActivityKind.TOOL_OTHER)
                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                self.tracker.push(make_activity(kind, _summarize_tool(tool_name, tool_input)))
            elif block_type == "text":
                text = block.get("text", "")
                if text and not self._looks_like_json(text):
                    self._push_reasoning(text, final=True, force_new=True)
                elif text:
                    self.tracker.push(make_activity(ActivityKind.RESULT, "structured text output received"))

    def _extract_user_activity(self, event: dict[str, Any]) -> None:
        assert self.tracker is not None
        message = event.get("message", {})
        if not isinstance(message, dict):
            return
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool_use_result = block.get("tool_use_result", {})
                if not isinstance(tool_use_result, dict):
                    continue
                file_info = tool_use_result.get("file", {})
                if isinstance(file_info, dict):
                    file_path = file_info.get("filePath", "")
                    if file_path:
                        self.tracker.push(make_activity(ActivityKind.PROGRESS, f"read completed {file_path}"))
                        return
        if event.get("tool_use_result") is not None:
            self.tracker.push(make_activity(ActivityKind.PROGRESS, "tool result received"))

    def _push_usage_progress(self, node: dict[str, Any]) -> None:
        assert self.tracker is not None
        usage = node.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) or isinstance(output_tokens, int):
                self.tracker.push(make_activity(
                    ActivityKind.PROGRESS,
                    f"tokens {self._fmt_tokens(input_tokens or 0)} in / {self._fmt_tokens(output_tokens or 0)} out",
                ))

    @staticmethod
    def _extract_structured_output(events: list[dict[str, Any]]) -> str:
        """Extract structured_output from the result event as JSON text."""
        for event in reversed(events):
            so = event.get("structured_output")
            if so is not None:
                if isinstance(so, str):
                    return so
                return json.dumps(so, ensure_ascii=False, separators=(",", ":"))
        return ""

    @staticmethod
    def _fmt_tokens(value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        stripped = text.strip()
        return stripped.startswith("{") or stripped.startswith("[")

    def clear_session(self) -> None:
        """Clear the stored session ID, forcing a fresh conversation on the next call."""
        self.session_id = None

    def _build_command(
        self,
        prompt: str | None,
        json_schema: dict[str, Any] | None,
    ) -> list[str]:
        """Build the claude argv.

        When ``prompt`` is None, the caller is responsible for supplying the
        prompt via stdin. When a prompt string is passed, it is included as
        the `-p` positional argument (legacy path, subject to the 128 KiB
        argv limit).
        """
        cmd = [self.cli_path]
        if self.session_id is not None:
            cmd.extend(["--resume", self.session_id])
        cmd.append("-p")
        if prompt is not None:
            cmd.append(prompt)
        cmd.extend([
            "--model", self.model,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ])
        if json_schema is not None:
            cmd.extend(["--json-schema", json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))])
        return cmd

    @staticmethod
    def _classify_error(
        error_type: str | None,
        error_message: str | None,
        stderr: str,
        returncode: int,
    ) -> tuple[InvocationStatus, str] | None:
        """Classify CLI-level errors into InvocationStatus."""
        stderr_lower = stderr.lower()
        if error_type is not None or "rate limit" in stderr_lower:
            message = error_message or stderr or "provider reported an error"
            if "rate_limit" in (error_type or "") or "rate limit" in message.lower():
                return InvocationStatus.RATE_LIMITED, message
            return InvocationStatus.PROVIDER_ERROR, message
        if returncode != 0:
            return InvocationStatus.PROVIDER_ERROR, stderr or f"Claude exited with status {returncode}"
        return None
