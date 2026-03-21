"""Tests for the FakeTransport test helper."""

from __future__ import annotations

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.fake_transport import (
    FakeTransport,
    make_completed_result,
    make_error_result,
)
from ralph_ng.adapters.transport import Transport, TransportResult


class TestFakeTransportProtocol:
    """FakeTransport must satisfy the Transport ABC."""

    def test_is_transport(self) -> None:
        assert issubclass(FakeTransport, Transport)

    def test_invoke_returns_transport_result(self) -> None:
        ft = FakeTransport([make_completed_result("hello")])
        result = ft.invoke("test", timeout_seconds=10.0)
        assert isinstance(result, TransportResult)
        assert result.output_text == "hello"

    def test_health_check_default(self) -> None:
        ft = FakeTransport()
        assert ft.health_check() is True

    def test_health_check_unhealthy(self) -> None:
        ft = FakeTransport(healthy=False)
        assert ft.health_check() is False


class TestFakeTransportBehavior:
    def test_replays_responses_in_order(self) -> None:
        ft = FakeTransport([
            make_completed_result("first"),
            make_completed_result("second"),
        ])
        assert ft.invoke("a", timeout_seconds=10.0).output_text == "first"
        assert ft.invoke("b", timeout_seconds=10.0).output_text == "second"

    def test_error_when_queue_exhausted(self) -> None:
        ft = FakeTransport([])
        result = ft.invoke("test", timeout_seconds=10.0)
        assert result.status == InvocationStatus.PROVIDER_ERROR
        assert "no more responses" in (result.error_message or "")

    def test_records_calls(self) -> None:
        ft = FakeTransport([make_completed_result()])
        ft.invoke("my prompt", timeout_seconds=30.0, cwd="/tmp", json_schema={"type": "object"})
        assert ft.call_count == 1
        assert ft.calls[0]["prompt"] == "my prompt"
        assert ft.calls[0]["timeout_seconds"] == 30.0
        assert ft.calls[0]["cwd"] == "/tmp"
        assert ft.calls[0]["json_schema"] == {"type": "object"}

    def test_last_prompt(self) -> None:
        ft = FakeTransport([make_completed_result(), make_completed_result()])
        assert ft.last_prompt is None
        ft.invoke("first", timeout_seconds=10.0)
        assert ft.last_prompt == "first"
        ft.invoke("second", timeout_seconds=10.0)
        assert ft.last_prompt == "second"

    def test_add_response(self) -> None:
        ft = FakeTransport()
        ft.add_response(make_completed_result("added"))
        result = ft.invoke("test", timeout_seconds=10.0)
        assert result.output_text == "added"

    def test_simulated_timeout(self) -> None:
        ft = FakeTransport([make_completed_result()], latency_seconds=100.0)
        result = ft.invoke("test", timeout_seconds=1.0)
        assert result.status == InvocationStatus.TIMEOUT

    def test_error_result_factory(self) -> None:
        result = make_error_result(
            InvocationStatus.RATE_LIMITED,
            "rate limited",
        )
        assert result.status == InvocationStatus.RATE_LIMITED
        assert result.error_message == "rate limited"

    def test_completed_result_factory_defaults(self) -> None:
        result = make_completed_result()
        assert result.status == InvocationStatus.COMPLETED
        assert result.output_text == "ok"
        assert result.input_tokens == 10
        assert result.output_tokens == 20
