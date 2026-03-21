"""Protocol tests for the Transport ABC.

These tests verify that any Transport implementation satisfies the contract.
They are parameterized so new transports can be plugged in.
"""

from __future__ import annotations

import pytest

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.transport import Transport, TransportResult


# ---------------------------------------------------------------------------
# Minimal concrete transport for protocol testing
# ---------------------------------------------------------------------------


class StubTransport(Transport):
    """Minimal transport that returns canned responses."""

    def __init__(
        self,
        *,
        output_text: str = "hello",
        status: InvocationStatus = InvocationStatus.COMPLETED,
        error_message: str | None = None,
        healthy: bool = True,
    ) -> None:
        self._output_text = output_text
        self._status = status
        self._error_message = error_message
        self._healthy = healthy

    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict | None = None,
    ) -> TransportResult:
        return TransportResult(
            status=self._status,
            output_text=self._output_text,
            duration_seconds=0.001,
            error_message=self._error_message,
        )

    def health_check(self) -> bool:
        return self._healthy


# ---------------------------------------------------------------------------
# TransportResult tests
# ---------------------------------------------------------------------------


class TestTransportResult:
    def test_defaults(self) -> None:
        result = TransportResult(
            status=InvocationStatus.COMPLETED,
            duration_seconds=1.5,
        )
        assert result.output_text == ""
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.error_message is None
        assert result.raw_events == []

    def test_full_construction(self) -> None:
        result = TransportResult(
            status=InvocationStatus.TIMEOUT,
            output_text="partial output",
            input_tokens=100,
            output_tokens=50,
            duration_seconds=30.0,
            error_message="timed out",
            raw_events=[{"type": "debug"}],
        )
        assert result.status == InvocationStatus.TIMEOUT
        assert result.output_text == "partial output"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.error_message == "timed out"
        assert len(result.raw_events) == 1

    def test_exit_status_field_exists(self) -> None:
        """TransportResult carries exit_status for failure classification."""
        assert "exit_status" in TransportResult.model_fields
        assert "stderr_text" in TransportResult.model_fields

    def test_serialization_roundtrip(self) -> None:
        result = TransportResult(
            status=InvocationStatus.COMPLETED,
            output_text="ok",
            input_tokens=10,
            output_tokens=20,
            duration_seconds=0.5,
        )
        data = result.model_dump()
        restored = TransportResult.model_validate(data)
        assert restored == result

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            TransportResult(
                status=InvocationStatus.COMPLETED,
                duration_seconds=1.0,
                some_unknown_field="bad",  # type: ignore[call-arg]
            )

    def test_exit_status_accepted(self) -> None:
        result = TransportResult(
            status=InvocationStatus.COMPLETED,
            duration_seconds=1.0,
            exit_status=0,
            stderr_text="some stderr",
        )
        assert result.exit_status == 0
        assert result.stderr_text == "some stderr"


# ---------------------------------------------------------------------------
# Transport protocol compliance tests
# ---------------------------------------------------------------------------


class TestTransportProtocol:
    """Tests that validate any Transport implementation."""

    @pytest.fixture
    def transport(self) -> Transport:
        return StubTransport()

    def test_invoke_returns_transport_result(self, transport: Transport) -> None:
        result = transport.invoke("test prompt", timeout_seconds=10.0)
        assert isinstance(result, TransportResult)

    def test_invoke_result_has_required_fields(self, transport: Transport) -> None:
        result = transport.invoke("test", timeout_seconds=10.0)
        assert hasattr(result, "status")
        assert hasattr(result, "output_text")
        assert hasattr(result, "input_tokens")
        assert hasattr(result, "output_tokens")
        assert hasattr(result, "duration_seconds")
        assert hasattr(result, "error_message")
        assert hasattr(result, "raw_events")

    def test_invoke_status_is_invocation_status(self, transport: Transport) -> None:
        result = transport.invoke("test", timeout_seconds=10.0)
        assert isinstance(result.status, InvocationStatus)

    def test_invoke_with_cwd(self, transport: Transport) -> None:
        result = transport.invoke("test", timeout_seconds=10.0, cwd="/tmp")
        assert isinstance(result, TransportResult)

    def test_invoke_with_json_schema(self, transport: Transport) -> None:
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        result = transport.invoke("test", timeout_seconds=10.0, json_schema=schema)
        assert isinstance(result, TransportResult)

    def test_health_check_returns_bool(self, transport: Transport) -> None:
        result = transport.health_check()
        assert isinstance(result, bool)

    def test_healthy_transport(self) -> None:
        transport = StubTransport(healthy=True)
        assert transport.health_check() is True

    def test_unhealthy_transport(self) -> None:
        transport = StubTransport(healthy=False)
        assert transport.health_check() is False

    def test_error_result(self) -> None:
        transport = StubTransport(
            status=InvocationStatus.PROVIDER_ERROR,
            error_message="boom",
            output_text="",
        )
        result = transport.invoke("test", timeout_seconds=10.0)
        assert result.status == InvocationStatus.PROVIDER_ERROR
        assert result.error_message == "boom"

    def test_timeout_result(self) -> None:
        transport = StubTransport(
            status=InvocationStatus.TIMEOUT,
            error_message="timed out after 5s",
            output_text="",
        )
        result = transport.invoke("test", timeout_seconds=5.0)
        assert result.status == InvocationStatus.TIMEOUT

    def test_is_abstract(self) -> None:
        """Transport cannot be instantiated directly."""
        with pytest.raises(TypeError):
            Transport()  # type: ignore[abstract]
