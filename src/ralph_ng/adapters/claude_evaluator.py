"""Claude CLI evaluator adapter."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import time

from pydantic import ValidationError

from ..review_bundle import ReviewBundle
from .base import (
    EvaluatorAdapter,
    EvaluatorResult,
    EvaluatorVerdict,
    InvocationStatus,
    MalformedStreamError,
    build_claude_command,
    classify_invocation_error,
    parse_stream_json,
)


class ClaudeEvaluatorAdapter(EvaluatorAdapter):
    """Invoke Claude CLI in stream-json mode for review verdicts."""

    def __init__(self, *, model: str, cli_path: str = "claude", max_retries: int = 2) -> None:
        self.model = model
        self.cli_path = cli_path
        self.max_retries = max_retries

    def evaluate(
        self,
        review_bundle: str | ReviewBundle,
        *,
        timeout_seconds: int | float,
        cwd: str | Path | None = None,
    ) -> EvaluatorResult:
        prompt = review_bundle.to_prompt() if isinstance(review_bundle, ReviewBundle) else review_bundle
        current_prompt = prompt
        attempt = 0
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
            except subprocess.TimeoutExpired:
                return EvaluatorResult(
                    status=InvocationStatus.TIMEOUT,
                    output_text="",
                    input_tokens=0,
                    output_tokens=0,
                    duration_seconds=time.monotonic() - started_at,
                    exit_status=-1,
                    error_message=f"Evaluator invocation timed out after {timeout_seconds} seconds",
                    verdict=None,
                )

            duration = time.monotonic() - started_at
            try:
                parsed = parse_stream_json(completed.stdout)
            except MalformedStreamError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    current_prompt = (
                        f"{prompt}\n\n"
                        "Previous attempt returned invalid JSON verdict output. "
                        "Return a single valid JSON verdict object only."
                    )
                    continue
                return EvaluatorResult(
                    status=InvocationStatus.MALFORMED_OUTPUT,
                    output_text="",
                    input_tokens=0,
                    output_tokens=0,
                    duration_seconds=duration,
                    exit_status=completed.returncode,
                    error_message=str(exc),
                    verdict=None,
                )

            classified = classify_invocation_error(
                parsed.error_type,
                parsed.error_message,
                completed.stderr,
                completed.returncode,
            )
            if classified is not None:
                return EvaluatorResult(
                    status=classified[0],
                    output_text=parsed.text,
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                    duration_seconds=duration,
                    exit_status=completed.returncode,
                    error_message=classified[1],
                    raw_events=parsed.events,
                    verdict=None,
                )

            try:
                verdict = EvaluatorVerdict.model_validate(json.loads(parsed.text))
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    current_prompt = (
                        f"{prompt}\n\n"
                        "Previous attempt returned invalid JSON verdict output. "
                        "Return a single valid JSON verdict object only."
                    )
                    continue
                return EvaluatorResult(
                    status=InvocationStatus.MALFORMED_OUTPUT,
                    output_text=parsed.text,
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                    duration_seconds=duration,
                    exit_status=completed.returncode,
                    error_message=str(exc),
                    raw_events=parsed.events,
                    verdict=None,
                )

            return EvaluatorResult(
                status=InvocationStatus.COMPLETED,
                output_text=parsed.text,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                duration_seconds=duration,
                exit_status=completed.returncode,
                raw_events=parsed.events,
                verdict=verdict,
            )
