"""Tests for ReviewFindings and FindingResponse schemas."""

from __future__ import annotations

import json

import pytest

from ralph_ng.review_findings import (
    FindingCategory,
    FindingResponse,
    FindingResponseVerdict,
    FindingSeverity,
    ReviewFinding,
    ReviewFindings,
    ReviewResult,
    finding_response_json_schema,
    parse_finding_responses,
    parse_review_findings,
    review_findings_json_schema,
)


class TestFindingSeverity:
    def test_values(self) -> None:
        assert set(FindingSeverity) == {"critical", "important", "minor"}

    def test_is_str(self) -> None:
        assert isinstance(FindingSeverity.CRITICAL, str)


class TestFindingCategory:
    def test_values(self) -> None:
        expected = {"spec_compliance", "correctness", "security", "testing", "architecture", "style"}
        assert set(FindingCategory) == expected


class TestReviewFinding:
    def test_minimal(self) -> None:
        f = ReviewFinding(
            id="F1",
            severity=FindingSeverity.MINOR,
            category=FindingCategory.STYLE,
            description="Missing docstring",
            evidence="src/foo.py:42",
        )
        assert f.id == "F1"
        assert f.machine_checkable is False
        assert f.verification_command == ""
        assert f.suggested_fix == ""

    def test_machine_checkable(self) -> None:
        f = ReviewFinding(
            id="F2",
            severity=FindingSeverity.IMPORTANT,
            category=FindingCategory.TESTING,
            description="Missing test",
            evidence="No test file for module",
            machine_checkable=True,
            verification_command="pytest tests/test_foo.py",
        )
        assert f.machine_checkable is True

    def test_machine_checkable_requires_command(self) -> None:
        with pytest.raises(Exception, match="verification_command"):
            ReviewFinding(
                id="F3",
                severity=FindingSeverity.CRITICAL,
                category=FindingCategory.SECURITY,
                description="SQL injection",
                evidence="src/db.py:10",
                machine_checkable=True,
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding(
                id="F1",
                severity="minor",
                category="style",
                description="x",
                evidence="y",
                bogus="z",  # type: ignore[call-arg]
            )


class TestReviewFindings:
    def test_accept_with_findings(self) -> None:
        rf = ReviewFindings(
            decision="accept",
            confidence=0.92,
            spec_compliance="pass",
            summary="Looks good overall",
            findings=[
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.MINOR,
                    category=FindingCategory.STYLE,
                    description="Nitpick",
                    evidence="foo.py:1",
                )
            ],
        )
        assert rf.decision == "accept"
        assert len(rf.findings) == 1

    def test_accept_no_findings(self) -> None:
        rf = ReviewFindings(
            decision="accept",
            confidence=0.99,
            spec_compliance="pass",
            summary="Clean",
        )
        assert rf.findings == []

    def test_reject_requires_findings(self) -> None:
        with pytest.raises(Exception, match="finding"):
            ReviewFindings(
                decision="reject",
                confidence=0.85,
                spec_compliance="fail",
                summary="Bad",
            )

    def test_reject_with_findings(self) -> None:
        rf = ReviewFindings(
            decision="reject",
            confidence=0.85,
            spec_compliance="fail",
            summary="Security issue",
            findings=[
                ReviewFinding(
                    id="F1",
                    severity=FindingSeverity.CRITICAL,
                    category=FindingCategory.SECURITY,
                    description="SQL injection",
                    evidence="db.py:10",
                    suggested_fix="Use parameterized queries",
                )
            ],
            repair_notes="Fix SQL injection before merge",
        )
        assert rf.decision == "reject"
        assert rf.repair_notes != ""

    def test_confidence_bounds(self) -> None:
        with pytest.raises(Exception):
            ReviewFindings(
                decision="accept",
                confidence=1.5,
                spec_compliance="pass",
                summary="x",
            )
        with pytest.raises(Exception):
            ReviewFindings(
                decision="accept",
                confidence=-0.1,
                spec_compliance="pass",
                summary="x",
            )

    def test_no_risk_field(self) -> None:
        """Risk is orchestrator-computed, not in ReviewFindings."""
        assert "risk" not in ReviewFindings.model_fields

    def test_spec_compliance_values(self) -> None:
        for val in ("pass", "fail", "partial"):
            rf = ReviewFindings(
                decision="accept",
                confidence=0.9,
                spec_compliance=val,
                summary="ok",
            )
            assert rf.spec_compliance == val


class TestFindingResponse:
    def test_concede_and_fixed(self) -> None:
        fr = FindingResponse(
            finding_id="F1",
            verdict=FindingResponseVerdict.CONCEDE_AND_FIXED,
            explanation="Fixed the SQL injection",
        )
        assert fr.verdict == "concede_and_fixed"

    def test_already_correct(self) -> None:
        fr = FindingResponse(
            finding_id="F2",
            verdict=FindingResponseVerdict.ALREADY_CORRECT,
            explanation="The code already uses parameterized queries",
        )
        assert fr.verdict == "already_correct"


class TestReviewResult:
    def test_minimal(self) -> None:
        rr = ReviewResult(status="completed")
        assert rr.findings is None
        assert rr.input_tokens == 0

    def test_with_findings(self) -> None:
        findings = ReviewFindings(
            decision="accept",
            confidence=0.95,
            spec_compliance="pass",
            summary="ok",
        )
        rr = ReviewResult(
            status="completed",
            findings=findings,
            input_tokens=100,
            output_tokens=50,
            duration_seconds=2.5,
        )
        assert rr.findings is not None
        assert rr.findings.decision == "accept"


class TestJsonSchema:
    def test_review_findings_schema(self) -> None:
        schema = review_findings_json_schema()
        assert "properties" in schema
        assert "decision" in schema["properties"]
        assert "confidence" in schema["properties"]
        assert "risk" not in schema["properties"]
        assert "required" in schema
        assert set(schema["required"]) == set(schema["properties"].keys())
        assert "default" not in json.dumps(schema)
        assert schema.get("title") is None
        assert schema.get("description") is None

        finding_schema = schema["$defs"]["ReviewFinding"]
        assert set(finding_schema["required"]) == set(finding_schema["properties"].keys())
        assert finding_schema["additionalProperties"] is False
        assert finding_schema.get("title") is None
        assert finding_schema.get("description") is None
        assert "description" in finding_schema["properties"]

    def test_finding_response_schema(self) -> None:
        schema = finding_response_json_schema()
        assert "properties" in schema
        assert "finding_id" in schema["properties"]
        assert set(schema["required"]) == set(schema["properties"].keys())
        assert "default" not in json.dumps(schema)
        assert schema.get("title") is None


class TestParsing:
    def test_parse_review_findings(self) -> None:
        data = {
            "decision": "accept",
            "confidence": 0.9,
            "spec_compliance": "pass",
            "summary": "ok",
            "findings": [],
        }
        rf = parse_review_findings(json.dumps(data))
        assert rf.decision == "accept"

    def test_parse_finding_responses_array(self) -> None:
        data = [
            {"finding_id": "F1", "verdict": "concede_and_fixed", "explanation": "Fixed"},
            {"finding_id": "F2", "verdict": "already_correct", "explanation": "Was correct"},
        ]
        responses = parse_finding_responses(json.dumps(data))
        assert len(responses) == 2
        assert responses[0].finding_id == "F1"

    def test_parse_finding_responses_object(self) -> None:
        data = {
            "responses": [
                {"finding_id": "F1", "verdict": "concede_and_fixed", "explanation": "Fixed"},
            ]
        }
        responses = parse_finding_responses(json.dumps(data))
        assert len(responses) == 1

    def test_parse_finding_responses_invalid(self) -> None:
        with pytest.raises(ValueError):
            parse_finding_responses(json.dumps({"not_responses": []}))
