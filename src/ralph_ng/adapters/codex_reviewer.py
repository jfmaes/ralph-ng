"""Codex reviewer adapter — adversarial code review via Codex CLI."""

from __future__ import annotations

import json

from pydantic import ValidationError

from ..review_findings import (
    ReviewFindings,
    ReviewResult,
    review_findings_json_schema,
)
from .base import InvocationStatus, ReviewerAdapter
from .streaming import ActivityKind, LiveActivityTracker, make_activity
from .transport import Transport, TransportResult


class CodexReviewerAdapter(ReviewerAdapter):
    """Invoke Codex via a Transport for adversarial code review.

    Uses the ReviewFindings JSON schema for structured output enforcement.
    Retries with a schema reminder on malformed output.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        max_retries: int = 2,
        tracker: LiveActivityTracker | None = None,
    ) -> None:
        self.transport = transport
        self.max_retries = max_retries
        self.tracker = tracker

    def review(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | None = None,
    ) -> ReviewResult:
        attempt = 0
        current_prompt = prompt
        workdir = str(cwd) if cwd is not None else None
        schema = review_findings_json_schema()
        retry_history: list[str] = []

        while True:
            result = self.transport.invoke(
                current_prompt,
                timeout_seconds=float(timeout_seconds),
                cwd=workdir,
                json_schema=schema,
            )

            if result.status == InvocationStatus.TIMEOUT:
                return self._to_review_result(
                    result,
                    findings=None,
                    error_message=self._compose_error_message(
                        retry_history,
                        result.error_message or f"Reviewer timed out after {timeout_seconds}s",
                    ),
                )

            if result.status == InvocationStatus.MALFORMED_OUTPUT:
                if attempt < self.max_retries:
                    attempt += 1
                    retry_note = (
                        f"Reviewer attempt {attempt} returned malformed output; "
                        f"retrying ({attempt}/{self.max_retries}) with a JSON-only reminder."
                    )
                    retry_history.append(retry_note)
                    self._push_progress(retry_note)
                    current_prompt = (
                        f"{prompt}\n\n"
                        "Previous attempt returned malformed output. "
                        "Return a single valid JSON object matching the ReviewFindings schema only."
                    )
                    continue
                return self._to_review_result(
                    result,
                    findings=None,
                    error_message=self._compose_error_message(
                        retry_history,
                        result.error_message or "Reviewer returned malformed output",
                    ),
                )

            if result.status != InvocationStatus.COMPLETED:
                return self._to_review_result(
                    result,
                    findings=None,
                    error_message=self._compose_error_message(
                        retry_history,
                        result.error_message or f"Reviewer status: {result.status}",
                    ),
                )

            # Try to parse ReviewFindings from the output
            findings = self._parse_findings(result.output_text)
            if findings is None:
                if attempt < self.max_retries:
                    attempt += 1
                    retry_note = (
                        f"Reviewer attempt {attempt} returned invalid JSON; "
                        f"retrying ({attempt}/{self.max_retries}) with a schema reminder."
                    )
                    retry_history.append(retry_note)
                    self._push_progress(retry_note)
                    current_prompt = (
                        f"{prompt}\n\n"
                        "Previous attempt returned invalid JSON. "
                        "Return a single valid JSON object matching the ReviewFindings schema only."
                    )
                    continue
                return ReviewResult(
                    status=InvocationStatus.MALFORMED_OUTPUT,
                    findings=None,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    duration_seconds=result.duration_seconds,
                    error_message=self._compose_error_message(
                        retry_history,
                        "Failed to parse ReviewFindings from output",
                    ),
                    raw_output_text=result.output_text,
                    raw_events=result.raw_events,
                )

            self._push_result(findings)
            return ReviewResult(
                status=InvocationStatus.COMPLETED,
                findings=findings,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_seconds=result.duration_seconds,
                raw_output_text=result.output_text,
                raw_events=result.raw_events,
            )

    @staticmethod
    def _parse_findings(text: str) -> ReviewFindings | None:
        """Try to parse ReviewFindings from text, returning None on failure."""
        # Try direct JSON parse
        try:
            return ReviewFindings.model_validate_json(text)
        except (json.JSONDecodeError, ValidationError):
            pass

        # Try extracting JSON from markdown code block
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                if block.startswith("{"):
                    try:
                        return ReviewFindings.model_validate_json(block)
                    except (json.JSONDecodeError, ValidationError):
                        continue

        # Codex may emit multiple standalone JSON objects while it refines a
        # final answer. Prefer the last valid ReviewFindings payload.
        decoder = json.JSONDecoder()
        idx = 0
        candidates: list[dict] = []
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            if text[idx] != "{":
                next_start = text.find("{", idx)
                if next_start < 0:
                    break
                idx = next_start
            try:
                obj, end = decoder.raw_decode(text, idx)
            except json.JSONDecodeError:
                idx += 1
                continue
            if isinstance(obj, dict):
                candidates.append(obj)
            idx = end

        for candidate in reversed(candidates):
            try:
                return ReviewFindings.model_validate(candidate)
            except ValidationError:
                continue

        return None

    @staticmethod
    def _to_review_result(
        result: TransportResult,
        *,
        findings: ReviewFindings | None,
        error_message: str | None = None,
    ) -> ReviewResult:
        return ReviewResult(
            status=result.status,
            findings=findings,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_seconds=result.duration_seconds,
            error_message=error_message if error_message is not None else result.error_message,
            raw_output_text=result.output_text,
            raw_events=result.raw_events,
        )

    def _push_progress(self, message: str) -> None:
        if self.tracker is None:
            return
        self.tracker.push(make_activity(ActivityKind.COMMENTARY, message))

    def _push_result(self, findings: ReviewFindings) -> None:
        if self.tracker is None:
            return
        summary = findings.summary.strip()
        if summary:
            self.tracker.push(make_activity(ActivityKind.RESULT, summary[:160]))

    @staticmethod
    def _compose_error_message(retry_history: list[str], final_message: str) -> str:
        if not retry_history:
            return final_message
        return " ".join([*retry_history, f"Final reviewer outcome: {final_message}"])
