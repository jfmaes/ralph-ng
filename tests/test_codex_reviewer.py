"""Tests for CodexReviewerAdapter."""

from __future__ import annotations

import json

from ralph_ng.adapters.base import InvocationStatus, ReviewerAdapter
from ralph_ng.adapters.codex_reviewer import CodexReviewerAdapter
from ralph_ng.adapters.fake_transport import FakeTransport, make_completed_result, make_error_result
from ralph_ng.adapters.streaming import LiveActivityTracker
from ralph_ng.review_findings import FindingCategory, FindingSeverity


def _accept_json(**overrides) -> str:
    data = {
        "decision": "accept",
        "confidence": 0.95,
        "spec_compliance": "pass",
        "summary": "Code looks good",
        "findings": [],
        **overrides,
    }
    return json.dumps(data)


def _reject_json(**overrides) -> str:
    data = {
        "decision": "reject",
        "confidence": 0.85,
        "spec_compliance": "fail",
        "summary": "Issues found",
        "findings": [
            {
                "id": "F1",
                "severity": "critical",
                "category": "security",
                "description": "SQL injection",
                "evidence": "db.py:10",
            }
        ],
        "repair_notes": "Fix SQL injection",
        **overrides,
    }
    return json.dumps(data)


class TestCodexReviewerAdapter:
    def test_is_reviewer_adapter(self) -> None:
        assert issubclass(CodexReviewerAdapter, ReviewerAdapter)

    def test_accept_verdict(self) -> None:
        transport = FakeTransport([make_completed_result(_accept_json())])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None
        assert result.findings.decision == "accept"
        assert result.findings.confidence == 0.95

    def test_reject_verdict_with_findings(self) -> None:
        transport = FakeTransport([make_completed_result(_reject_json())])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None
        assert result.findings.decision == "reject"
        assert len(result.findings.findings) == 1
        assert result.findings.findings[0].id == "F1"
        assert result.findings.findings[0].severity == FindingSeverity.CRITICAL
        assert result.findings.findings[0].category == FindingCategory.SECURITY

    def test_timeout(self) -> None:
        transport = FakeTransport([make_error_result(InvocationStatus.TIMEOUT, "timed out")])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.TIMEOUT
        assert result.findings is None

    def test_provider_error(self) -> None:
        transport = FakeTransport([make_error_result(InvocationStatus.PROVIDER_ERROR, "crash")])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.PROVIDER_ERROR
        assert result.findings is None

    def test_retry_on_malformed_output(self) -> None:
        transport = FakeTransport([
            make_error_result(InvocationStatus.MALFORMED_OUTPUT, "no output"),
            make_completed_result(_accept_json()),
        ])
        adapter = CodexReviewerAdapter(transport=transport, max_retries=1)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None
        assert transport.call_count == 2
        assert "malformed output" in transport.calls[1]["prompt"].lower()

    def test_retry_on_invalid_json(self) -> None:
        transport = FakeTransport([
            make_completed_result("not valid json at all"),
            make_completed_result(_accept_json()),
        ])
        adapter = CodexReviewerAdapter(transport=transport, max_retries=1)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None

    def test_retry_reason_surfaces_in_tracker(self) -> None:
        transport = FakeTransport([
            make_completed_result("not valid json at all"),
            make_completed_result(_accept_json()),
        ])
        tracker = LiveActivityTracker()
        adapter = CodexReviewerAdapter(transport=transport, max_retries=1, tracker=tracker)

        adapter.review("review this", timeout_seconds=60)

        recent = tracker.recent(3)
        assert recent
        assert any("invalid json" in item.summary.lower() for item in recent)

    def test_exhausted_retries(self) -> None:
        transport = FakeTransport([
            make_completed_result("bad1"),
            make_completed_result("bad2"),
            make_completed_result("bad3"),
        ])
        adapter = CodexReviewerAdapter(transport=transport, max_retries=2)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.MALFORMED_OUTPUT
        assert result.findings is None
        assert "final reviewer outcome" in (result.error_message or "").lower()

    def test_passes_json_schema(self) -> None:
        transport = FakeTransport([make_completed_result(_accept_json())])
        adapter = CodexReviewerAdapter(transport=transport)
        adapter.review("review this", timeout_seconds=60)

        assert transport.calls[0]["json_schema"] is not None
        assert "decision" in str(transport.calls[0]["json_schema"])

    def test_extracts_json_from_markdown(self) -> None:
        text = f"Here is my review:\n```json\n{_accept_json()}\n```\n"
        transport = FakeTransport([make_completed_result(text)])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None

    def test_extracts_json_from_text_with_preamble(self) -> None:
        text = f"My analysis follows:\n{_accept_json()}\nEnd."
        transport = FakeTransport([make_completed_result(text)])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None

    def test_uses_last_valid_json_object_from_concatenated_output(self) -> None:
        placeholder = json.dumps({
            "decision": "reject",
            "confidence": 0.1,
            "spec_compliance": "fail",
            "summary": "placeholder",
            "findings": [],
            "repair_notes": "",
            "new_verification_commands": [],
        })
        text = "\n".join([placeholder, _accept_json(summary="final answer")])
        transport = FakeTransport([make_completed_result(text)])
        adapter = CodexReviewerAdapter(transport=transport)
        result = adapter.review("review this", timeout_seconds=60)

        assert result.status == InvocationStatus.COMPLETED
        assert result.findings is not None
        assert result.findings.summary == "final answer"

    def test_passes_cwd(self) -> None:
        transport = FakeTransport([make_completed_result(_accept_json())])
        adapter = CodexReviewerAdapter(transport=transport)
        adapter.review("review this", timeout_seconds=60, cwd="/tmp/repo")

        assert transport.calls[0]["cwd"] == "/tmp/repo"
