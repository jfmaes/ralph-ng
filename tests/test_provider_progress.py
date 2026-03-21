"""Tests for provider activity in progress renderer (Task 15)."""

from __future__ import annotations

from ralph_ng.progress import ProviderActivity, ProgressRenderer


class TestProviderActivity:
    def test_defaults(self) -> None:
        pa = ProviderActivity()
        assert pa.provider == ""
        assert pa.role == ""
        assert pa.review_round == 0
        assert pa.latest_commentary == ""
        assert pa.current_action == ""
        assert pa.elapsed_seconds == 0.0
        assert pa.active is False

    def test_set_active(self) -> None:
        pa = ProviderActivity()
        pa.active = True
        pa.provider = "claude"
        pa.role = "executor"
        pa.current_action = "editing files"
        assert pa.active is True
        assert pa.provider == "claude"
