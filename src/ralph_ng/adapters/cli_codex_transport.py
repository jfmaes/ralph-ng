"""CLI Codex transport — subprocess-based Transport for Codex exec.

Uses ``stream_subprocess`` for line-by-line streaming so the TUI can
show live tool-use activity while Codex is running.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .base import InvocationStatus
from .streaming import ActivityKind, LiveActivityTracker, make_activity, stream_subprocess
from .transport import ProviderEventCallback, Transport, TransportResult


class CliCodexTransport(Transport):
    """Invoke Codex CLI via ``codex exec --json`` with live activity streaming.

    JSONL events are parsed line-by-line with tolerance for partial lines.
    """

    def __init__(
        self,
        *,
        model: str,
        cli_path: str = "codex",
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        on_event: ProviderEventCallback | None = None,
        tracker: LiveActivityTracker | None = None,
    ) -> None:
        self.model = model
        self.cli_path = cli_path
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier
        self._on_event = on_event
        self.tracker = tracker
        # Cancel event for graceful mid-stream interruption
        self.cancel_event: __import__("threading").Event | None = None
        # Buffer for accumulating streaming text deltas into full thoughts
        self._text_buffer: list[str] = []
        self._thought_counter = 0
        self._current_thought_id: str | None = None
        # Track unhandled event types for diagnostics
        self._seen_event_types: set[str] = set()
        self._handled_event_types: set[str] = set()

    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> TransportResult:
        command, schema_path, prompt_path = self._build_command(prompt, json_schema)
        workdir = str(Path(cwd)) if cwd is not None else None

        # Accumulated state from streaming
        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        error_message: str | None = None
        events: list[dict[str, Any]] = []
        unexpected_lines: list[str] = []

        def _on_line(raw_line: str) -> None:
            nonlocal input_tokens, output_tokens, error_message

            line = raw_line.strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                unexpected_lines.append(line)
                self._report_unexpected_output(line)
                return  # tolerate partial/corrupt lines

            if not isinstance(event, dict):
                unexpected_lines.append(line)
                self._report_unexpected_output(line)
                return

            events.append(event)
            event_type = event.get("type", "")

            # Extract text from completed agent messages
            if event_type == "item.completed":
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text", "")
                    if text:
                        text_parts.append(text)

            # Error events
            elif event_type in ("error", "turn.failed"):
                msg = event.get("message") or ""
                err = event.get("error", {})
                if isinstance(err, dict):
                    msg = msg or err.get("message", "")
                if msg:
                    error_message = msg

            # Token usage
            usage = event.get("usage")
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", input_tokens)
                output_tokens = usage.get("output_tokens", output_tokens)

            # Extract tool activity for live tracker
            if self.tracker is not None:
                try:
                    self._extract_activity(event)
                except Exception as exc:
                    self._report_tracker_error(exc, event_type)

            # Emit raw event for persistence
            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception as exc:
                    self._report_tracker_error(exc, f"persist {event_type}")

        started_at = time.monotonic()
        try:
            stream_result = stream_subprocess(
                command,
                cwd=workdir,
                timeout_seconds=timeout_seconds,
                stdin_path=prompt_path,
                on_line=_on_line,
                cancel_event=self.cancel_event,
            )
        finally:
            for path in (schema_path, prompt_path):
                if path is None:
                    continue
                try:
                    os.unlink(path)
                except OSError:
                    pass
        # Flush any remaining buffered text after stream ends
        if self.tracker is not None:
            self._flush_text_buffer(final=True)
            self._emit_unhandled_diagnostics()
        duration = time.monotonic() - started_at

        if stream_result.timed_out:
            return TransportResult(
                status=InvocationStatus.TIMEOUT,
                output_text="\n".join(text_parts) if text_parts else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=duration,
                error_message=f"Codex CLI timed out after {timeout_seconds}s",
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        # Check for errors
        if error_message:
            return TransportResult(
                status=InvocationStatus.PROVIDER_ERROR,
                output_text="\n".join(text_parts) if text_parts else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=duration,
                error_message=error_message,
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        if stream_result.returncode != 0:
            stderr_msg = stream_result.stderr_text.strip() if stream_result.stderr_text else f"Codex exited with status {stream_result.returncode}"
            return TransportResult(
                status=InvocationStatus.PROVIDER_ERROR,
                output_text="\n".join(text_parts) if text_parts else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=duration,
                error_message=stderr_msg,
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        if not text_parts and not events:
            return TransportResult(
                status=InvocationStatus.MALFORMED_OUTPUT,
                output_text="\n".join(unexpected_lines),
                duration_seconds=duration,
                error_message="Codex produced no parseable JSON output",
                exit_status=stream_result.returncode,
                stderr_text=stream_result.stderr_text,
                raw_events=events,
            )

        return TransportResult(
            status=InvocationStatus.COMPLETED,
            output_text="\n".join(text_parts) if text_parts else "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration,
            exit_status=stream_result.returncode,
            stderr_text=stream_result.stderr_text,
            raw_events=events,
        )

    def health_check(self) -> bool:
        """Check if the codex CLI is available on PATH."""
        return shutil.which(self.cli_path) is not None

    def _emit_unhandled_diagnostics(self) -> None:
        """Emit a diagnostic if we saw event types we didn't handle."""
        unhandled = self._seen_event_types - self._handled_event_types
        # Ignore types that are intentionally unhandled (usage-only, etc.)
        unhandled.discard("")
        if unhandled and self.tracker is not None:
            types_str = ", ".join(sorted(unhandled))
            self.tracker.push(make_activity(
                ActivityKind.PROGRESS,
                f"unhandled codex event types: {types_str[:120]}",
            ))
        self._seen_event_types.clear()
        self._handled_event_types.clear()

    def _start_thought(self) -> None:
        self._thought_counter += 1
        self._current_thought_id = f"codex-thought-{self._thought_counter}"

    def _extract_activity(self, event: dict[str, Any]) -> None:
        """Extract reasoning and tool activity from a Codex JSONL event."""
        assert self.tracker is not None
        event_type = event.get("type", "")
        self._seen_event_types.add(event_type)
        matched = True

        if event_type == "thread.started":
            self._flush_text_buffer(final=True)
            self.tracker.push(make_activity(ActivityKind.PROGRESS, "review session started"))

        elif event_type == "turn.started":
            self._flush_text_buffer(final=True)
            self.tracker.push(make_activity(ActivityKind.PROGRESS, "review turn started"))

        elif event_type == "turn.completed":
            self._flush_text_buffer(final=True)
            self.tracker.push(make_activity(ActivityKind.RESULT, "review turn completed"))

        elif event_type == "item.started":
            item = event.get("item", {})
            if not isinstance(item, dict):
                return
            item_type = item.get("type", "")
            if item_type == "command_execution":
                cmd = item.get("command", "")
                self.tracker.push(make_activity(
                    ActivityKind.TOOL_BASH,
                    f"[Bash] {cmd[:60]}",
                ))
            elif item_type == "reasoning":
                text = item.get("text", "") or item.get("summary", "")
                if text:
                    self._push_reasoning(text, final=True, force_new=True)
                else:
                    self.tracker.push(make_activity(ActivityKind.PROGRESS, "reasoning started"))
            elif item_type == "agent_message":
                self.tracker.push(make_activity(ActivityKind.PROGRESS, "drafting review output"))

        elif event_type == "item.completed":
            item = event.get("item", {})
            if not isinstance(item, dict):
                return
            item_type = item.get("type", "")
            if item_type == "file_change":
                file_path = item.get("file_path", "")
                self.tracker.push(make_activity(
                    ActivityKind.TOOL_EDIT,
                    f"[Edit] {file_path}",
                ))
            elif item_type == "reasoning":
                self._push_reasoning(
                    item.get("text", "") or item.get("summary", ""),
                    final=True,
                    force_new=True,
                )
            elif item_type == "agent_message":
                # Look for reasoning in content blocks only — skip
                # structured JSON output (the final answer).
                content = item.get("content", [])
                emitted_reasoning = False
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") in ("output_text", "text"):
                            text = block.get("text", "")
                            if text and not self._looks_like_json(text):
                                self._push_reasoning(text, final=True, force_new=True)
                                emitted_reasoning = True
                if not emitted_reasoning:
                    text = item.get("text", "")
                    if isinstance(text, str) and text.strip():
                        if self._looks_like_json(text):
                            self.tracker.push(make_activity(
                                ActivityKind.RESULT,
                                self._summarize_structured_message(text),
                                text=self._summarize_structured_message(text),
                            ))
                        else:
                            self._push_reasoning(text, final=True, force_new=True)
                            emitted_reasoning = True
                if not emitted_reasoning:
                    self.tracker.push(make_activity(ActivityKind.RESULT, "structured review output received"))

        # Streaming reasoning summaries (gpt-5.x reasoning models)
        elif event_type == "response.reasoning_summary_text.delta":
            delta = event.get("delta", "")
            if isinstance(delta, str) and delta:
                self._text_buffer.append(delta)
                if "\n" in delta:
                    self._flush_text_buffer()
                else:
                    buffered = "".join(self._text_buffer)
                    if len(buffered) >= 40:
                        self._flush_text_buffer()

        elif event_type == "response.reasoning_summary_text.done":
            self._flush_text_buffer(final=True)

        elif event_type in (
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        ):
            text = event.get("part", {}).get("text", "") if isinstance(event.get("part"), dict) else ""
            if text:
                self._push_reasoning(text, final=True, force_new=True)

        # Streaming message deltas (output text as it arrives)
        elif event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if isinstance(delta, str) and not self._looks_like_json(delta):
                self._text_buffer.append(delta)
                # Flush on sentence boundaries or when buffer is large
                if "\n" in delta:
                    self._flush_text_buffer()
                else:
                    buffered = "".join(self._text_buffer)
                    if len(buffered) >= 40:
                        self._flush_text_buffer()

        # Incremental item content (alternative streaming path)
        elif event_type == "item.streaming":
            item = event.get("item", {})
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "reasoning":
                    text = item.get("text", "") or item.get("summary", "")
                    if text:
                        self._text_buffer.append(text)
                        self._flush_text_buffer()

        elif event_type == "response.output_item.done":
            self._flush_text_buffer(final=True)
            self.tracker.push(make_activity(ActivityKind.RESULT, "response item completed"))

        # Error events
        elif event_type in ("error", "turn.failed"):
            msg = event.get("message") or ""
            err = event.get("error", {})
            if isinstance(err, dict):
                msg = msg or err.get("message", "")
            self.tracker.push(make_activity(
                ActivityKind.ERROR,
                f"Error: {msg[:80]}",
            ))

        else:
            matched = False

        if matched:
            self._handled_event_types.add(event_type)

        usage = event.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) or isinstance(output_tokens, int):
                self.tracker.push(make_activity(
                    ActivityKind.PROGRESS,
                    f"tokens {self._fmt_tokens(input_tokens or 0)} in / {self._fmt_tokens(output_tokens or 0)} out",
                ))

    def _flush_text_buffer(self, *, final: bool = False) -> None:
        """Flush accumulated streaming text as commentary events."""
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
        """Push reasoning text as a grouped thought block."""
        assert self.tracker is not None
        if not text or len(text.strip()) < 3:
            return
        stripped = text.strip()
        if self._looks_like_json(stripped):
            return
        if force_new or self._current_thought_id is None:
            self._start_thought()

        summary = stripped
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if line and not self._looks_like_json(line):
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

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        """Quick check if text is structured JSON output, not reasoning."""
        stripped = text.strip()
        return stripped.startswith("{") or stripped.startswith("[")

    @staticmethod
    def _fmt_tokens(value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    def _report_unexpected_output(self, line: str) -> None:
        if self.tracker is not None:
            snippet = line[:160]
            self.tracker.push(make_activity(
                ActivityKind.PROGRESS,
                f"unexpected codex output: {snippet}",
                text=f"unexpected codex output: {snippet}",
            ))

    def _report_tracker_error(self, exc: Exception, context: str) -> None:
        if self.tracker is not None:
            self.tracker.push(make_activity(
                ActivityKind.ERROR,
                f"codex stream handling error ({context}): {exc}",
                text=f"codex stream handling error ({context}): {exc}",
            ))

    @staticmethod
    def _summarize_structured_message(text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return "structured review output received"
        if not isinstance(payload, dict):
            return "structured review output received"
        decision = str(payload.get("decision", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        confidence = payload.get("confidence")
        parts: list[str] = []
        if decision:
            if isinstance(confidence, (int, float)):
                parts.append(f"{decision} ({confidence:.2f})")
            else:
                parts.append(decision)
        if summary:
            parts.append(summary)
        return " - ".join(parts) if parts else "structured review output received"

    def _build_command(
        self,
        prompt: str,
        json_schema: dict[str, Any] | None,
    ) -> tuple[list[str], str | None, str]:
        cmd = [
            self.cli_path,
            "exec",
            "--json",
            "--model", self.model,
            "--skip-git-repo-check",
            "--yolo",
            "-",
        ]
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        )
        prompt_file.write(prompt)
        prompt_file.close()
        prompt_path = prompt_file.name
        schema_path: str | None = None
        if json_schema is not None:
            schema_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                encoding="utf-8",
            )
            json.dump(json_schema, schema_file, ensure_ascii=False, separators=(",", ":"))
            schema_file.close()
            schema_path = schema_file.name
            cmd.insert(-1, "--output-schema")
            cmd.insert(-1, schema_file.name)
        if self.reasoning_effort is not None:
            cmd[1:1] = ["-c", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"]
        if self.service_tier is not None:
            cmd[1:1] = ["-c", f"service_tier={json.dumps(self.service_tier)}"]
        return cmd, schema_path, prompt_path
