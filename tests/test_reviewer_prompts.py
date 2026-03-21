"""Tests for reviewer prompt templates."""

from __future__ import annotations

from ralph_ng.prompts.reviewer_first_pass import build_first_pass_prompt
from ralph_ng.prompts.challenge_executor import build_challenge_executor_prompt
from ralph_ng.prompts.challenge_reviewer import build_challenge_reviewer_prompt
from ralph_ng.review_findings import (
    FindingCategory,
    FindingResponse,
    FindingResponseVerdict,
    FindingSeverity,
    ReviewFinding,
)


def _sample_finding() -> ReviewFinding:
    return ReviewFinding(
        id="F1",
        severity=FindingSeverity.CRITICAL,
        category=FindingCategory.SECURITY,
        description="SQL injection in query builder",
        evidence="src/db.py:42",
        suggested_fix="Use parameterized queries",
        machine_checkable=True,
        verification_command="pytest tests/test_db_injection.py",
    )


class TestFirstPassPrompt:
    def test_contains_system_prompt(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="task-1",
            task_title="Add login",
            task_description="Implement login flow",
            acceptance_criteria=["User can log in", "Passwords are hashed"],
            git_diff="+ added line",
            changed_file_snippets={"src/auth.py": "code here"},
            verification_results=[],
        )
        assert "INDEPENDENCE PROTOCOL" in prompt
        assert "ANTI-TRUST RULES" in prompt
        assert "Stage 1" in prompt
        assert "Stage 2" in prompt

    def test_contains_task_contract(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="task-1",
            task_title="Add login",
            task_description="Implement login flow",
            acceptance_criteria=["User can log in"],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "task-1" in prompt
        assert "Add login" in prompt
        assert "User can log in" in prompt

    def test_contains_diff(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="+ new code\n- old code",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "+ new code" in prompt

    def test_contains_json_schema(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "ReviewFindings JSON Schema" in prompt
        assert '"decision"' in prompt

    def test_does_not_leak_executor_rationale(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "executor" not in prompt.lower().split("independence protocol")[0]

    def test_contains_verification_results(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[
                {"command": "pytest", "success": True, "exit_code": 0, "stdout": "ok", "stderr": ""},
            ],
        )
        assert "pytest" in prompt
        assert "success: True" in prompt

    def test_contains_finding_id_instructions(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "F1, F2, F3" in prompt

    def test_anti_trust_model_agnostic(self) -> None:
        prompt = build_first_pass_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "regardless of who or what wrote it" in prompt


class TestChallengeExecutorPrompt:
    def test_contains_system_prompt(self) -> None:
        prompt = build_challenge_executor_prompt(
            task_id="task-1",
            task_title="Add login",
            findings=[_sample_finding()],
            reviewer_summary="Security issues found",
        )
        assert "REPAIR FIRST" in prompt
        assert "FORBIDDEN" in prompt
        assert "concede_and_fixed" in prompt
        assert "already_correct" in prompt

    def test_contains_findings(self) -> None:
        prompt = build_challenge_executor_prompt(
            task_id="task-1",
            task_title="Add login",
            findings=[_sample_finding()],
            reviewer_summary="Security issues found",
        )
        assert "F1" in prompt
        assert "SQL injection" in prompt
        assert "src/db.py:42" in prompt

    def test_contains_schema(self) -> None:
        prompt = build_challenge_executor_prompt(
            task_id="task-1",
            task_title="Add login",
            findings=[_sample_finding()],
            reviewer_summary="Issues",
        )
        assert "FindingResponse JSON Schema" in prompt
        assert '"finding_id"' in prompt

    def test_references_finding_ids(self) -> None:
        prompt = build_challenge_executor_prompt(
            task_id="task-1",
            task_title="Add login",
            findings=[_sample_finding()],
            reviewer_summary="Issues",
        )
        assert "F1 [critical]" in prompt

    def test_accepts_dict_findings(self) -> None:
        prompt = build_challenge_executor_prompt(
            task_id="task-1",
            task_title="t",
            findings=[{"id": "F1", "severity": "critical", "category": "security",
                       "description": "Bad", "evidence": "file:1"}],
            reviewer_summary="Issues",
        )
        assert "F1" in prompt


class TestChallengeReviewerPrompt:
    def test_contains_system_prompt(self) -> None:
        prompt = build_challenge_reviewer_prompt(
            task_id="task-1",
            task_title="Add login",
            task_description="Implement login",
            acceptance_criteria=["Login works"],
            original_findings=[_sample_finding()],
            executor_responses=[
                FindingResponse(
                    finding_id="F1",
                    verdict=FindingResponseVerdict.CONCEDE_AND_FIXED,
                    explanation="Fixed SQL injection with parameterized queries",
                ),
            ],
            new_git_diff="+ parameterized",
            changed_file_snippets={"src/db.py": "new code"},
            verification_results=[],
        )
        assert "FINAL verdict" in prompt
        assert "Do not anchor on your previous findings" in prompt

    def test_contains_original_findings(self) -> None:
        prompt = build_challenge_reviewer_prompt(
            task_id="task-1",
            task_title="Add login",
            task_description="d",
            acceptance_criteria=[],
            original_findings=[_sample_finding()],
            executor_responses=[],
            new_git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "F1" in prompt
        assert "SQL injection" in prompt

    def test_contains_executor_responses(self) -> None:
        prompt = build_challenge_reviewer_prompt(
            task_id="task-1",
            task_title="Add login",
            task_description="d",
            acceptance_criteria=[],
            original_findings=[_sample_finding()],
            executor_responses=[
                FindingResponse(
                    finding_id="F1",
                    verdict=FindingResponseVerdict.CONCEDE_AND_FIXED,
                    explanation="Fixed it",
                ),
            ],
            new_git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "concede_and_fixed" in prompt
        assert "Fixed it" in prompt

    def test_contains_new_diff(self) -> None:
        prompt = build_challenge_reviewer_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            original_findings=[],
            executor_responses=[],
            new_git_diff="+ new repair code",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "+ new repair code" in prompt

    def test_contains_schema(self) -> None:
        prompt = build_challenge_reviewer_prompt(
            task_id="t",
            task_title="t",
            task_description="d",
            acceptance_criteria=[],
            original_findings=[],
            executor_responses=[],
            new_git_diff="diff",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "ReviewFindings JSON Schema" in prompt
