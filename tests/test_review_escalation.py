"""Tests for human escalation policy (Task 14)."""

from __future__ import annotations

from ralph_ng.config import ReviewConfig, ReviewEscalationConfig
from ralph_ng.orchestrator import should_escalate
from ralph_ng.review_findings import (
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewFindings,
)
from ralph_ng.risk import RiskLevel


def _make_findings(
    decision: str = "accept",
    confidence: float = 0.95,
    findings: list[ReviewFinding] | None = None,
) -> ReviewFindings:
    if findings is None:
        findings = []
    if decision == "reject" and not findings:
        findings = [
            ReviewFinding(
                id="F1",
                severity=FindingSeverity.IMPORTANT,
                category=FindingCategory.CORRECTNESS,
                description="Issue",
                evidence="file:1",
            )
        ]
    return ReviewFindings(
        decision=decision,
        confidence=confidence,
        spec_compliance="pass" if decision == "accept" else "fail",
        summary="test",
        findings=findings,
        repair_notes="fix" if decision == "reject" else "",
    )


class TestShouldEscalate:
    def test_no_escalation_happy_path(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.95)
        assert should_escalate(findings, "low", config) is False

    def test_rejection_loop_exhaustion(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("reject", 0.85)
        assert should_escalate(findings, "low", config, rejection_count=3) is True

    def test_accept_does_not_trigger_rejection_loop_exhaustion(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.85)
        assert should_escalate(findings, "low", config, rejection_count=99) is False

    def test_rejection_loop_not_exhausted(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("reject", 0.85)
        assert should_escalate(findings, "low", config, rejection_count=2) is False

    def test_all_rounds_below_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.55)
        assert should_escalate(findings, "low", config) is True

    def test_critical_risk_below_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.75)
        assert should_escalate(findings, "critical", config) is True

    def test_critical_risk_above_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.85)
        assert should_escalate(findings, "critical", config) is False

    def test_security_findings_below_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings(
            "accept",
            0.80,
            [
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.IMPORTANT,
                    category=FindingCategory.SECURITY,
                    description="auth bypass",
                    evidence="auth.py:10",
                ),
            ],
        )
        assert should_escalate(findings, "low", config) is True

    def test_security_findings_above_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings(
            "accept",
            0.90,
            [
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.MINOR,
                    category=FindingCategory.SECURITY,
                    description="minor xss",
                    evidence="view.py:1",
                ),
            ],
        )
        assert should_escalate(findings, "low", config) is False

    def test_persistent_critical_findings_after_repair(self) -> None:
        config = ReviewConfig()
        findings = _make_findings(
            "reject",
            0.85,
            [
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.CRITICAL,
                    category=FindingCategory.SECURITY,
                    description="SQL injection persists",
                    evidence="db.py:10",
                ),
            ],
        )
        assert should_escalate(findings, "low", config, rejection_count=1) is False
        assert should_escalate(findings, "low", config, rejection_count=2) is True

    def test_configurable_rejection_loops(self) -> None:
        config = ReviewConfig(
            escalation=ReviewEscalationConfig(max_rejection_loops=5),
        )
        findings = _make_findings("reject", 0.85)
        assert should_escalate(findings, "low", config, rejection_count=4) is False
        assert should_escalate(findings, "low", config, rejection_count=5) is True

    def test_uses_risk_level_enum(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.75)
        assert should_escalate(findings, RiskLevel.CRITICAL, config) is True
