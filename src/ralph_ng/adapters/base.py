"""Shared types and helpers for model CLI adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ..review_findings import ReviewResult


class InvocationStatus(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_OUTPUT = "malformed_output"


class AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: InvocationStatus
    output_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float
    exit_status: int
    error_message: str | None = None
    stderr_text: str = ""
    raw_events: list[dict[str, Any]] = Field(default_factory=list)


class ExecutorResult(AdapterResult):
    model_config = ConfigDict(extra="forbid")


@dataclass(slots=True)
class ParsedStream:
    text: str
    input_tokens: int
    output_tokens: int
    error_type: str | None
    error_message: str | None
    events: list[dict[str, Any]]


class MalformedStreamError(ValueError):
    """Raised when a stream-json payload cannot be parsed safely."""


class ExecutorAdapter(ABC):
    @abstractmethod
    def execute(self, prompt: str, *, timeout_seconds: int | float, cwd: str | None = None) -> ExecutorResult:
        """Run the executor model."""


class ReviewerAdapter(ABC):
    """ABC for reviewer adapters.

    Produces ReviewResult with severity-graded, evidence-backed findings.
    """

    @abstractmethod
    def review(
        self,
        prompt: str,
        *,
        timeout_seconds: int | float,
        cwd: str | None = None,
    ) -> ReviewResult:
        """Run the reviewer model and return structured findings."""


def parse_stream_json(stdout: str) -> ParsedStream:
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    error_type: str | None = None
    error_message: str | None = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedStreamError(f"Invalid stream-json line: {raw_line}") from exc
        if not isinstance(event, dict):
            raise MalformedStreamError("Each stream-json line must decode to an object")

        events.append(event)
        text_parts.extend(_extract_text(event))

        usage = _find_usage(event)
        if usage is not None:
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)

        event_error_type, event_error_message = _find_error(event)
        if event_error_type is not None:
            error_type = event_error_type
            error_message = event_error_message

    return ParsedStream(
        text="".join(text_parts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error_type=error_type,
        error_message=error_message,
        events=events,
    )


def _extract_text(node: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str):
            texts.append(text)
        for key in ("delta", "message", "content", "completion"):
            if key in node:
                texts.extend(_extract_text(node[key]))
    elif isinstance(node, list):
        for item in node:
            texts.extend(_extract_text(item))
    elif isinstance(node, str):
        return []
    return texts


def _find_usage(node: Any) -> dict[str, int] | None:
    if isinstance(node, dict):
        usage = node.get("usage")
        if isinstance(usage, dict):
            resolved: dict[str, int] = {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int):
                resolved["input_tokens"] = input_tokens
            if isinstance(output_tokens, int):
                resolved["output_tokens"] = output_tokens
            if resolved:
                return resolved
        for value in node.values():
            nested = _find_usage(value)
            if nested is not None:
                return nested
    elif isinstance(node, list):
        for item in node:
            nested = _find_usage(item)
            if nested is not None:
                return nested
    return None


def _find_error(node: Any) -> tuple[str | None, str | None]:
    if isinstance(node, dict):
        if node.get("type") == "error":
            payload = node.get("error", node)
            if isinstance(payload, dict):
                error_type = payload.get("type")
                error_message = payload.get("message")
                return (
                    error_type if isinstance(error_type, str) else "error",
                    error_message if isinstance(error_message, str) else None,
                )
        error = node.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            error_message = error.get("message")
            if isinstance(error_type, str) or isinstance(error_message, str):
                return (
                    error_type if isinstance(error_type, str) else "error",
                    error_message if isinstance(error_message, str) else None,
                )
        for value in node.values():
            error_type, error_message = _find_error(value)
            if error_type is not None:
                return error_type, error_message
    elif isinstance(node, list):
        for item in node:
            error_type, error_message = _find_error(item)
            if error_type is not None:
                return error_type, error_message
    return None, None
