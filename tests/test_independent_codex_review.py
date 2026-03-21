"""Tests for independent first-pass adversarial review (Task 12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralph_ng.adapters.base import InvocationStatus
from ralph_ng.adapters.fake_transport import FakeTransport, make_completed_result, make_error_result
from ralph_ng.adapters.codex_reviewer import CodexReviewerAdapter
from ralph_ng.config import ReviewConfig, RalphConfig, RolesConfig, RoleConfig
from ralph_ng.orchestrator import should_challenge
from ralph_ng.review_findings import ReviewFindings, FindingSeverity, FindingCategory


def _accept_findings(confidence: float = 0.95, **overrides) -> ReviewFindings:
    return ReviewFindings(
        decision="accept",
        confidence=confidence,
        spec_compliance="pass",
        summary="Looks good",
        findings=[],
        **overrides,
    )


def _reject_findings(confidence: float = 0.85, **overrides) -> ReviewFindings:
    from ralph_ng.review_findings import ReviewFinding
    return ReviewFindings(
        decision="reject",
        confidence=confidence,
        spec_compliance="fail",
        summary="Issues found",
        findings=[
            ReviewFinding(
                id="F1",
                severity=FindingSeverity.IMPORTANT,
                category=FindingCategory.CORRECTNESS,
                description="Missing edge case handling",
                evidence="src/foo.py:42",
            ),
        ],
        repair_notes="Fix edge case",
        **overrides,
    )


class TestShouldChallenge:
    def test_high_confidence_accept_no_challenge(self) -> None:
        config = ReviewConfig()
        findings = _accept_findings(confidence=0.96)
        assert should_challenge(findings, "low", config) is False

    def test_low_confidence_accept_triggers_challenge(self) -> None:
        config = ReviewConfig()
        findings = _accept_findings(confidence=0.70)
        assert should_challenge(findings, "low", config) is True

    def test_high_confidence_reject_no_challenge(self) -> None:
        config = ReviewConfig()
        findings = _reject_findings(confidence=0.95)
        assert should_challenge(findings, "low", config) is False

    def test_low_confidence_reject_triggers_challenge(self) -> None:
        config = ReviewConfig()
        findings = _reject_findings(confidence=0.65)
        assert should_challenge(findings, "low", config) is True

    def test_critical_findings_trigger_challenge(self) -> None:
        from ralph_ng.review_findings import ReviewFinding
        config = ReviewConfig()
        findings = ReviewFindings(
            decision="accept",
            confidence=0.95,
            spec_compliance="pass",
            summary="ok",
            findings=[
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.CRITICAL,
                    category=FindingCategory.SECURITY,
                    description="SQL injection",
                    evidence="db.py:10",
                ),
            ],
        )
        assert should_challenge(findings, "low", config) is True

    def test_security_findings_trigger_challenge(self) -> None:
        from ralph_ng.review_findings import ReviewFinding
        config = ReviewConfig()
        findings = ReviewFindings(
            decision="accept",
            confidence=0.95,
            spec_compliance="pass",
            summary="ok",
            findings=[
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.MINOR,
                    category=FindingCategory.SECURITY,
                    description="Potential XSS",
                    evidence="view.py:5",
                ),
            ],
        )
        assert should_challenge(findings, "low", config) is True

    def test_high_risk_triggers_challenge(self) -> None:
        config = ReviewConfig()
        findings = _accept_findings(confidence=0.95)
        assert should_challenge(findings, "high", config) is True

    def test_critical_risk_triggers_challenge(self) -> None:
        config = ReviewConfig()
        findings = _accept_findings(confidence=0.95)
        assert should_challenge(findings, "critical", config) is True

    def test_medium_risk_high_confidence_no_challenge(self) -> None:
        config = ReviewConfig()
        findings = _accept_findings(confidence=0.96)
        assert should_challenge(findings, "medium", config) is False


class TestReviewerDoesNotSeeExecutorRationale:
    """The first-pass reviewer must not see executor reasoning."""

    def test_first_pass_prompt_excludes_rationale(self) -> None:
        from ralph_ng.prompts.reviewer_first_pass import build_first_pass_prompt
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=["crit"],
            git_diff="+ code",
            changed_file_snippets={},
            verification_results=[],
        )
        # The prompt should not contain executor-specific terms before the independence protocol
        assert "INDEPENDENCE PROTOCOL" in prompt
        # Check no "executor" word appears in the task contract section
        task_contract_section = prompt.split("# Task Contract")[1].split("# Git Diff")[0]
        assert "executor" not in task_contract_section.lower()
