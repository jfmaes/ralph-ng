"""Tests for challenge round logic (Task 13)."""

from __future__ import annotations

from ralph_ng.config import ReviewConfig, ReviewChallengeConfig
from ralph_ng.orchestrator import should_challenge
from ralph_ng.review_findings import (
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewFindings,
)


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


class TestChallengePolicy:
    """Challenge trigger policy tests with configurable thresholds."""

    def test_skip_challenge_fast_path(self) -> None:
        """High confidence + low risk + no security + no critical → skip."""
        config = ReviewConfig()
        findings = _make_findings("accept", 0.96)
        assert should_challenge(findings, "low", config) is False

    def test_challenge_on_reject_low_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("reject", 0.65)
        assert should_challenge(findings, "low", config) is True

    def test_no_challenge_on_reject_high_confidence(self) -> None:
        """High-confidence reject goes straight to repair."""
        config = ReviewConfig()
        findings = _make_findings("reject", 0.92)
        assert should_challenge(findings, "low", config) is False

    def test_challenge_on_accept_with_critical_finding(self) -> None:
        config = ReviewConfig()
        findings = _make_findings(
            "accept",
            0.95,
            [
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.CRITICAL,
                    category=FindingCategory.CORRECTNESS,
                    description="crash",
                    evidence="main.py:1",
                ),
            ],
        )
        assert should_challenge(findings, "low", config) is True

    def test_challenge_on_accept_with_security_finding(self) -> None:
        config = ReviewConfig()
        findings = _make_findings(
            "accept",
            0.95,
            [
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.MINOR,
                    category=FindingCategory.SECURITY,
                    description="xss",
                    evidence="view.py:1",
                ),
            ],
        )
        assert should_challenge(findings, "low", config) is True

    def test_challenge_on_high_risk(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.96)
        assert should_challenge(findings, "high", config) is True
        assert should_challenge(findings, "critical", config) is True

    def test_no_challenge_on_medium_risk_high_confidence(self) -> None:
        config = ReviewConfig()
        findings = _make_findings("accept", 0.96)
        assert should_challenge(findings, "medium", config) is False

    def test_configurable_thresholds(self) -> None:
        from ralph_ng.config import ReviewThresholdsConfig
        config = ReviewConfig(
            thresholds=ReviewThresholdsConfig(
                challenge_below_confidence=0.90,
            ),
        )
        # 0.85 is below 0.90 → challenge
        findings = _make_findings("accept", 0.85)
        assert should_challenge(findings, "low", config) is True

        # 0.92 is above 0.90 → no challenge
        findings = _make_findings("accept", 0.92)
        assert should_challenge(findings, "low", config) is False
