"""Transport abstraction for model provider invocations.

The Transport ABC defines a synchronous, provider-neutral interface for
invoking models.  CLI transports spawn subprocesses; future API transports
will make HTTP calls.  Both return the same ``TransportResult``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .base import InvocationStatus


class TransportResult(BaseModel):
    """Provider-neutral result of a single model invocation.

    exit_status and stderr_text carry CLI transport internals needed
    for failure classification.  API transports set exit_status to the
    HTTP status code and stderr_text to the error body.
    """

    model_config = ConfigDict(extra="forbid")

    status: InvocationStatus
    output_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    exit_status: int | None = None
    stderr_text: str = ""
    raw_events: list[dict[str, Any]] = Field(default_factory=list)


# Callback signature for provider event emission.
# Transports call this with a dict representing a normalized provider event.
ProviderEventCallback = Callable[[dict[str, Any]], None]


class Transport(ABC):
    """Synchronous, provider-neutral model invocation interface.

    ralph is turn-based — even future API transports will be blocking
    calls with a timeout.
    """

    @abstractmethod
    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> TransportResult:
        """Send a prompt and block until the model responds.

        Parameters
        ----------
        prompt:
            The full prompt text.
        timeout_seconds:
            Maximum wall-clock seconds before the invocation is aborted.
        cwd:
            Working directory for the invocation (relevant for CLI transports).
        json_schema:
            Optional JSON schema for structured output enforcement.
            CLI transports write this to a temp file; API transports pass it
            as ``response_format``.
        """

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the transport can reach its provider."""
