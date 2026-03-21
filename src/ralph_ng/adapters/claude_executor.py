"""Claude CLI executor adapter."""

from __future__ import annotations

from pathlib import Path

from .base import ExecutorAdapter, ExecutorResult, InvocationStatus
from .transport import Transport, TransportResult


class ClaudeExecutorAdapter(ExecutorAdapter):
    """Invoke Claude via a Transport for code execution tasks."""

    def __init__(self, *, transport: Transport, max_retries: int = 1) -> None:
        self.transport = transport
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
        workdir = str(cwd) if cwd is not None else None

        while True:
            result = self.transport.invoke(
                current_prompt,
                timeout_seconds=float(timeout_seconds),
                cwd=workdir,
            )

            if result.status == InvocationStatus.MALFORMED_OUTPUT and attempt < self.max_retries:
                attempt += 1
                current_prompt = (
                    f"{prompt}\n\n"
                    "Previous attempt returned malformed stream-json output. "
                    "Return only valid stream-json."
                )
                continue

            return self._to_executor_result(result)

    @staticmethod
    def _to_executor_result(result: TransportResult) -> ExecutorResult:
        # Propagate real exit status from transport instead of collapsing to 0
        if result.exit_status is not None:
            exit_status = result.exit_status
        elif result.status == InvocationStatus.TIMEOUT:
            exit_status = -1
        else:
            exit_status = 0
        return ExecutorResult(
            status=result.status,
            output_text=result.output_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_seconds=result.duration_seconds,
            exit_status=exit_status,
            error_message=result.error_message,
            stderr_text=result.stderr_text,
            raw_events=result.raw_events,
        )
