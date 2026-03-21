"""Tests for the failure classification module."""

from __future__ import annotations

from ralph_ng.failure import (
    RECOVERY_POLICY,
    FailureClass,
    FailureDetail,
    classify_failure,
    classify_verification_failure,
)


def test_classify_provider_timeout():
    result = classify_failure(exit_code=None, duration_seconds=30.0, stderr="", timed_out=True)
    assert result is FailureClass.PROVIDER_TIMEOUT


def test_classify_provider_crash():
    result = classify_failure(exit_code=1, duration_seconds=0.5, stderr="")
    assert result is FailureClass.PROVIDER_CRASH


def test_classify_provider_rate_limit():
    result = classify_failure(exit_code=1, duration_seconds=10.0, stderr="Error: rate limit exceeded")
    assert result is FailureClass.PROVIDER_RATE_LIMIT


def test_classify_provider_schema_error():
    result = classify_failure(
        exit_code=1,
        duration_seconds=5.0,
        stderr="",
        error_message="invalid_json_schema: missing required field",
    )
    assert result is FailureClass.PROVIDER_SCHEMA_ERROR


def test_classify_provider_malformed_output():
    result = classify_failure(
        exit_code=1,
        duration_seconds=5.0,
        stderr="",
        error_message="Failed to parse response body",
    )
    assert result is FailureClass.PROVIDER_MALFORMED_OUTPUT


def test_classify_provider_interrupted():
    result = classify_failure(exit_code=143, duration_seconds=10.0, stderr="")
    assert result is FailureClass.PROVIDER_INTERRUPTED


def test_classify_provider_sigkill():
    result = classify_failure(exit_code=137, duration_seconds=60.0, stderr="")
    assert result is FailureClass.PROVIDER_TIMEOUT


def test_classify_missing_binary():
    result = classify_failure(exit_code=127, duration_seconds=0.1, stderr="")
    assert result is FailureClass.VERIFICATION_MISSING_BINARY


def test_classify_command_not_found():
    result = classify_failure(
        exit_code=1,
        duration_seconds=5.0,
        stderr="bash: pytest: command not found",
    )
    assert result is FailureClass.VERIFICATION_MISSING_BINARY


def test_classify_internal_error_default():
    result = classify_failure(exit_code=2, duration_seconds=10.0, stderr="something unexpected")
    assert result is FailureClass.INTERNAL_ERROR


def test_classify_normal_exit_not_crash():
    result = classify_failure(exit_code=1, duration_seconds=5.0, stderr="")
    assert result is FailureClass.INTERNAL_ERROR


def test_classify_verification_failure_exit_code():
    result = classify_verification_failure(exit_code=1, stderr="", command="pytest")
    assert result is FailureClass.VERIFICATION_TEST_FAILURE


def test_classify_verification_failure_missing_binary():
    result = classify_verification_failure(exit_code=127, stderr="", command="pytest")
    assert result is FailureClass.VERIFICATION_MISSING_BINARY


def test_classify_verification_failure_timeout():
    result = classify_verification_failure(exit_code=None, stderr="", command="pytest")
    assert result is FailureClass.VERIFICATION_TIMEOUT


def test_recovery_policy_keys():
    for fc in FailureClass:
        assert fc in RECOVERY_POLICY, f"FailureClass.{fc.name} missing from RECOVERY_POLICY"


def test_failure_detail_model():
    detail = FailureDetail(
        failure_class=FailureClass.PROVIDER_TIMEOUT,
        summary="Provider timed out after 60s",
        retries=1,
        evidence_paths=["/tmp/log.txt"],
        recovery_actions_taken=["retried once"],
        auto_recoverable=True,
    )
    assert detail.failure_class is FailureClass.PROVIDER_TIMEOUT
    assert detail.summary == "Provider timed out after 60s"
    assert detail.retries == 1
    assert detail.evidence_paths == ["/tmp/log.txt"]
    assert detail.recovery_actions_taken == ["retried once"]
    assert detail.auto_recoverable is True
