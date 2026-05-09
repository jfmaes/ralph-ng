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
from ralph_ng.state import StateStore, utc_now


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


def test_validate_auto_fix_blocked_project_git_removal():
    """Project-destructive removal should be blocked."""
    assert _validate_auto_fix_command("rm -rf .git") is False


def test_validate_auto_fix_blocks_shell_indirected_dangerous_command():
    """Shell metacharacters must not bypass destructive command checks."""
    assert _validate_auto_fix_command("npm install && rm -rf .git") is False
    assert _validate_auto_fix_command("sh -c 'rm -rf .git'") is False


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


def test_attempt_self_healing_respects_diagnosis_budget(tmp_path):
    config = SelfHealingConfig(enabled=True, max_diagnosis_per_task=1)
    store = StateStore(tmp_path / "state.db")
    store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
    store.connection.execute(
        """
        INSERT INTO recovery_attempts
        (run_id, task_id, attempt_number, failure_class, recovery_kind, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "T1", 1, "provider_crash", "diagnosis", "failed", utc_now()),
    )
    store.connection.commit()

    outcome = attempt_self_healing(
        config=config,
        task_id="T1",
        task_title="Task",
        task_description="Desc",
        failure_class=FailureClass.PROVIDER_CRASH,
        error_message="boom",
        cwd=tmp_path,
        store=store,
        run_id="run-1",
    )

    assert outcome.attempted is False
    assert "Diagnosis budget exhausted" in outcome.summary


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


def test_diagnosis_appends_disable_slash_for_claude(monkeypatch, tmp_path):
    from ralph_ng import self_healing
    from ralph_ng.config import SelfHealingConfig

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class _Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""
        return _Result()

    monkeypatch.setattr(self_healing.subprocess, "run", fake_run)

    cfg = SelfHealingConfig(
        diagnosis_provider="claude",
        diagnosis_cli_path="claude",
        disable_plugins_and_slash_commands=True,
    )
    self_healing.run_diagnosis_session(
        config=cfg,
        task_id="TASK-001",
        task_title="t",
        task_description="d",
        failure_class="provider_crash",
        error_message="boom",
        cwd=tmp_path,
    )
    assert "--disable-slash-commands" in captured["cmd"]


def test_diagnosis_inserts_disable_plugins_for_codex(monkeypatch, tmp_path):
    from ralph_ng import self_healing
    from ralph_ng.config import SelfHealingConfig

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class _Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""
        return _Result()

    monkeypatch.setattr(self_healing.subprocess, "run", fake_run)

    cfg = SelfHealingConfig(
        diagnosis_provider="codex",
        diagnosis_cli_path="codex",
        disable_plugins_and_slash_commands=True,
    )
    self_healing.run_diagnosis_session(
        config=cfg,
        task_id="TASK-001",
        task_title="t",
        task_description="d",
        failure_class="provider_crash",
        error_message="boom",
        cwd=tmp_path,
    )
    cmd = captured["cmd"]
    assert "--disable" in cmd
    assert cmd[cmd.index("--disable") + 1] == "plugins"
    assert cmd.index("--disable") < cmd.index("exec")


def test_diagnosis_omits_disable_when_flag_off(monkeypatch, tmp_path):
    from ralph_ng import self_healing
    from ralph_ng.config import SelfHealingConfig

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class _Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""
        return _Result()

    monkeypatch.setattr(self_healing.subprocess, "run", fake_run)

    cfg = SelfHealingConfig(diagnosis_provider="claude", diagnosis_cli_path="claude")
    self_healing.run_diagnosis_session(
        config=cfg,
        task_id="TASK-001",
        task_title="t",
        task_description="d",
        failure_class="provider_crash",
        error_message="boom",
        cwd=tmp_path,
    )
    assert "--disable-slash-commands" not in captured["cmd"]
