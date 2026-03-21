"""Tests for ralph_ng.preflight module."""

from __future__ import annotations

from ralph_ng.config import RalphConfig
from ralph_ng.preflight import PreflightCheck, PreflightResult, run_preflight


# ---------------------------------------------------------------------------
# PreflightResult
# ---------------------------------------------------------------------------

def test_preflight_result_all_passed():
    """PreflightResult with all-passing checks reports all_passed=True."""
    result = PreflightResult(checks=[
        PreflightCheck(name="Check A", passed=True, message="OK"),
        PreflightCheck(name="Check B", passed=True, message="OK"),
    ])
    assert result.all_passed is True


def test_preflight_result_some_failed():
    """PreflightResult with one failing check reports all_passed=False."""
    result = PreflightResult(checks=[
        PreflightCheck(name="Check A", passed=True, message="OK"),
        PreflightCheck(name="Check B", passed=False, message="Missing"),
    ])
    assert result.all_passed is False


def test_preflight_result_summary():
    """summary() output contains check names and PASS/FAIL labels."""
    result = PreflightResult(checks=[
        PreflightCheck(name="Git workspace", passed=True, message="Clean"),
        PreflightCheck(name="CLI (claude)", passed=False, message="Not found"),
    ])
    text = result.summary()
    assert "Git workspace" in text
    assert "CLI (claude)" in text
    assert "[PASS]" in text
    assert "[FAIL]" in text


# ---------------------------------------------------------------------------
# PreflightCheck dataclass
# ---------------------------------------------------------------------------

def test_preflight_check_dataclass():
    """PreflightCheck fields are accessible and hold expected values."""
    check = PreflightCheck(name="Test check", passed=True, message="All good")
    assert check.name == "Test check"
    assert check.passed is True
    assert check.message == "All good"


# ---------------------------------------------------------------------------
# run_preflight
# ---------------------------------------------------------------------------

def test_run_preflight_basic(tmp_path):
    """run_preflight returns a PreflightResult with a minimal config."""
    # Initialize a git repo so the git-status check doesn't fail with a non-repo error
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)

    config = RalphConfig()
    result = run_preflight(repo_root=tmp_path, config=config)
    assert isinstance(result, PreflightResult)
    # At minimum we expect the CLI check and the git workspace check
    assert len(result.checks) >= 1
