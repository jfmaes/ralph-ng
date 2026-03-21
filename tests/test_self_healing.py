"""Tests for ralph_ng.self_healing module."""

from __future__ import annotations

from pathlib import Path

from ralph_ng.config import SelfHealingConfig
from ralph_ng.failure import FailureClass
from ralph_ng.self_healing import (
    DiagnosisResult,
    RecoveryOutcome,
    _validate_auto_fix_command,
    attempt_self_healing,
    is_failure_eligible_for_diagnosis,
)


# ---------------------------------------------------------------------------
# is_failure_eligible_for_diagnosis
# ---------------------------------------------------------------------------

def test_is_failure_eligible_provider_crash():
    """PROVIDER_CRASH is eligible for diagnosis."""
    assert is_failure_eligible_for_diagnosis(FailureClass.PROVIDER_CRASH) is True


def test_is_failure_not_eligible_operator_pause():
    """OPERATOR_PAUSE is NOT eligible for diagnosis."""
    assert is_failure_eligible_for_diagnosis(FailureClass.OPERATOR_PAUSE) is False


def test_is_failure_not_eligible_workspace_drift():
    """WORKSPACE_DRIFT is NOT eligible for diagnosis."""
    assert is_failure_eligible_for_diagnosis(FailureClass.WORKSPACE_DRIFT) is False


# ---------------------------------------------------------------------------
# _validate_auto_fix_command
# ---------------------------------------------------------------------------

def test_validate_auto_fix_safe_command():
    """'npm install' is a safe command and should be allowed."""
    assert _validate_auto_fix_command("npm install") is True


def test_validate_auto_fix_blocked_sudo():
    """'sudo apt install' should be blocked."""
    assert _validate_auto_fix_command("sudo apt install") is False


def test_validate_auto_fix_blocked_apt():
    """'apt install foo' should be blocked."""
    assert _validate_auto_fix_command("apt install foo") is False


def test_validate_auto_fix_blocked_rm():
    """'rm -rf /' should be blocked."""
    assert _validate_auto_fix_command("rm -rf /") is False


def test_validate_auto_fix_blocked_git_reset():
    """'git reset --hard' should be blocked."""
    assert _validate_auto_fix_command("git reset --hard") is False


# ---------------------------------------------------------------------------
# attempt_self_healing
# ---------------------------------------------------------------------------

def test_attempt_self_healing_disabled():
    """When config.enabled=False, attempt_self_healing returns attempted=False."""
    config = SelfHealingConfig(enabled=False)
    outcome = attempt_self_healing(
        config=config,
        task_id="t1",
        task_title="Test task",
        task_description="A test",
        failure_class=FailureClass.PROVIDER_CRASH,
        error_message="boom",
        cwd=Path("/tmp"),
    )
    assert outcome.attempted is False
    assert outcome.succeeded is False
    assert outcome.diagnosis is None


def test_attempt_self_healing_ineligible_failure():
    """WORKSPACE_DRIFT is ineligible; attempt_self_healing returns attempted=False."""
    config = SelfHealingConfig(enabled=True)
    outcome = attempt_self_healing(
        config=config,
        task_id="t2",
        task_title="Test task",
        task_description="A test",
        failure_class=FailureClass.WORKSPACE_DRIFT,
        error_message="drift detected",
        cwd=Path("/tmp"),
    )
    assert outcome.attempted is False
    assert outcome.succeeded is False
    assert outcome.diagnosis is None


# ---------------------------------------------------------------------------
# Dataclass structure tests
# ---------------------------------------------------------------------------

def test_recovery_outcome_structure():
    """RecoveryOutcome fields are accessible and hold expected values."""
    outcome = RecoveryOutcome(
        attempted=True,
        succeeded=False,
        diagnosis=None,
        actions_taken=["diagnosis_attempted"],
        summary="Test summary",
    )
    assert outcome.attempted is True
    assert outcome.succeeded is False
    assert outcome.diagnosis is None
    assert outcome.actions_taken == ["diagnosis_attempted"]
    assert outcome.summary == "Test summary"


def test_diagnosis_result_structure():
    """DiagnosisResult fields are accessible and hold expected values."""
    diagnosis = DiagnosisResult(
        failure_class="provider_crash",
        root_cause_summary="OOM in provider",
        confidence=0.85,
        can_auto_fix=True,
        auto_fix_commands=["npm install"],
        human_actions=[],
        notes_for_resume="Retry after fix",
    )
    assert diagnosis.failure_class == "provider_crash"
    assert diagnosis.root_cause_summary == "OOM in provider"
    assert diagnosis.confidence == 0.85
    assert diagnosis.can_auto_fix is True
    assert diagnosis.auto_fix_commands == ["npm install"]
    assert diagnosis.human_actions == []
    assert diagnosis.notes_for_resume == "Retry after fix"
    assert diagnosis.raw_output == ""
