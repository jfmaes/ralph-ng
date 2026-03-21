"""Smoke tests for Codex CLI transport with real binary.

Opt-in: requires `codex` on PATH. Marked with @pytest.mark.smoke for CI skip.
Strict 60s runtime cap per invocation.
"""

from __future__ import annotations

import shutil

import pytest

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.cli_codex_transport import CliCodexTransport

pytestmark = pytest.mark.smoke

TIMEOUT = 60.0


@pytest.fixture
def codex_transport():
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not found on PATH")
    return CliCodexTransport(model="gpt-5.3-codex")


class TestCodexCliSmoke:
    def test_health_check(self, codex_transport: CliCodexTransport) -> None:
        assert codex_transport.health_check() is True

    def test_simple_invoke(self, codex_transport: CliCodexTransport, tmp_path) -> None:
        result = codex_transport.invoke(
            "Respond with exactly: CODEX_SMOKE_OK",
            timeout_seconds=TIMEOUT,
            cwd=str(tmp_path),
        )
        assert result.status == InvocationStatus.COMPLETED
        assert "CODEX_SMOKE_OK" in result.output_text
        assert result.duration_seconds > 0
