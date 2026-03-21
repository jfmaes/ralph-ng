"""Smoke tests for Claude CLI transport with real binary.

Opt-in: requires `claude` on PATH. Marked with @pytest.mark.smoke for CI skip.
Strict 60s runtime cap per invocation.
"""

from __future__ import annotations

import shutil

import pytest

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.cli_claude_transport import CliClaudeTransport

pytestmark = pytest.mark.smoke

TIMEOUT = 60.0


@pytest.fixture
def claude_transport():
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found on PATH")
    return CliClaudeTransport(model="sonnet")


class TestClaudeCliSmoke:
    def test_health_check(self, claude_transport: CliClaudeTransport) -> None:
        assert claude_transport.health_check() is True

    def test_simple_invoke(self, claude_transport: CliClaudeTransport, tmp_path) -> None:
        result = claude_transport.invoke(
            "Respond with exactly: SMOKE_TEST_OK",
            timeout_seconds=TIMEOUT,
            cwd=str(tmp_path),
        )
        assert result.status == InvocationStatus.COMPLETED
        assert "SMOKE_TEST_OK" in result.output_text
        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.duration_seconds > 0

    def test_invoke_with_json_schema(self, claude_transport: CliClaudeTransport, tmp_path) -> None:
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
            },
            "required": ["answer"],
        }
        result = claude_transport.invoke(
            'Return a JSON object with key "answer" and value "42"',
            timeout_seconds=TIMEOUT,
            cwd=str(tmp_path),
            json_schema=schema,
        )
        assert result.status == InvocationStatus.COMPLETED
        assert "42" in result.output_text
