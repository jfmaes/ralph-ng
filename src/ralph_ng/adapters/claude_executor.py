"""Claude CLI executor adapter."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

from .base import (
    ExecutorAdapter,
    ExecutorResult,
    InvocationStatus,
    MalformedStreamError,
    build_claude_command,
    classify_invocation_error,
    parse_stream_json,
)
from ..utils import coerce_text


class ClaudeExecutorAdapter(ExecutorAdapter):
    """Invoke Claude CLI in stream-json mode for code execution tasks."""

    def __init__(self, *, model: str, cli_path: str = "claude", max_retries: int = 1) -> None:
        self.model = model
        self.cli_path = cli_path
        self.max_retries = max_retries

    def execute(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | Path | None = None,
    ) -> ExecutorResult:
        attempt = 0
        current_prompt = prompt
        workdir = None if cwd is None else Path(cwd)

        while True:
            started_at = time.monotonic()
            command = build_claude_command(self.cli_path, self.model, current_prompt)
            try:
                completed = subprocess.run(
                    command,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return ExecutorResult(
                    status=InvocationStatus.TIMEOUT,
                    output_text=coerce_text(exc.stdout if hasattr(exc, "stdout") else exc.output),
                    input_tokens=0,
                    output_tokens=0,
                    duration_seconds=time.monotonic() - started_at,
                    exit_status=-1,
                    error_message=f"Executor invocation timed out after {timeout_seconds} seconds",
                )

            duration = time.monotonic() - started_at
            try:
                parsed = parse_stream_json(completed.stdout)
            except MalformedStreamError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    current_prompt = (
                        f"{prompt}\n\n"
                        "Previous attempt returned malformed stream-json output. "
                        "Return only valid stream-json."
                    )
                    continue
                return ExecutorResult(
                    status=InvocationStatus.MALFORMED_OUTPUT,
                    output_text="",
                    input_tokens=0,
                    output_tokens=0,
                    duration_seconds=duration,
                    exit_status=completed.returncode,
                    error_message=str(exc),
                )

            classified = classify_invocation_error(
                parsed.error_type,
                parsed.error_message,
                completed.stderr,
                completed.returncode,
            )
            if classified is not None:
                return ExecutorResult(
                    status=classified[0],
                    output_text=parsed.text,
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                    duration_seconds=duration,
                    exit_status=completed.returncode,
                    error_message=classified[1],
                    raw_events=parsed.events,
                )

            return ExecutorResult(
                status=InvocationStatus.COMPLETED,
                output_text=parsed.text,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                duration_seconds=duration,
                exit_status=completed.returncode,
                raw_events=parsed.events,
            )
