from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FailureClass(StrEnum):
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CRASH = "provider_crash"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_SCHEMA_ERROR = "provider_schema_error"
    PROVIDER_MALFORMED_OUTPUT = "provider_malformed_output"
    PROVIDER_INTERRUPTED = "provider_interrupted"
    VERIFICATION_TEST_FAILURE = "verification_test_failure"
    VERIFICATION_MISSING_BINARY = "verification_missing_binary"
    VERIFICATION_TIMEOUT = "verification_timeout"
    WORKSPACE_DRIFT = "workspace_drift"
    REVIEW_DISAGREEMENT = "review_disagreement"
    DEPENDENCY_MISSING_PROJECT = "dependency_missing_project"
    DEPENDENCY_MISSING_SYSTEM = "dependency_missing_system"
    OPERATOR_PAUSE = "operator_pause"
    INTERNAL_ERROR = "internal_error"


class FailureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: FailureClass
    summary: str
    retries: int = 0
    evidence_paths: list[str] = Field(default_factory=list)
    recovery_actions_taken: list[str] = Field(default_factory=list)
    auto_recoverable: bool = False


def classify_failure(
    exit_code: int | None,
    duration_seconds: float,
    stderr: str,
    timed_out: bool = False,
    error_message: str = "",
) -> FailureClass:
    """Classify a provider/process failure into a FailureClass based on evidence."""
    if timed_out:
        return FailureClass.PROVIDER_TIMEOUT

    if exit_code == 143:
        return FailureClass.PROVIDER_INTERRUPTED

    if exit_code == 137:
        return FailureClass.PROVIDER_TIMEOUT

    if exit_code == 127:
        return FailureClass.VERIFICATION_MISSING_BINARY

    if exit_code == 1 and duration_seconds < 2.0:
        return FailureClass.PROVIDER_CRASH

    if "rate limit" in stderr.lower():
        return FailureClass.PROVIDER_RATE_LIMIT

    if "invalid_json_schema" in error_message or "invalid_json_schema" in stderr:
        return FailureClass.PROVIDER_SCHEMA_ERROR

    if "malformed" in error_message.lower() or "Failed to parse" in error_message:
        return FailureClass.PROVIDER_MALFORMED_OUTPUT

    if "command not found" in stderr.lower():
        return FailureClass.VERIFICATION_MISSING_BINARY

    return FailureClass.INTERNAL_ERROR


def classify_verification_failure(
    exit_code: int | None,
    stderr: str,
    command: str,
) -> FailureClass:
    """Classify a verification step failure into a FailureClass."""
    stderr_lower = stderr.lower()

    if exit_code == 127 or "command not found" in stderr_lower or "no such file" in stderr_lower:
        return FailureClass.VERIFICATION_MISSING_BINARY

    if exit_code is None:
        return FailureClass.VERIFICATION_TIMEOUT

    return FailureClass.VERIFICATION_TEST_FAILURE


RECOVERY_POLICY: dict[FailureClass, dict] = {
    FailureClass.PROVIDER_TIMEOUT: {
        "max_retries": 1,
        "backoff": False,
        "description": "Provider timed out; retry once without backoff.",
    },
    FailureClass.PROVIDER_CRASH: {
        "max_retries": 2,
        "backoff": False,
        "description": "Provider crashed rapidly; retry up to twice.",
    },
    FailureClass.PROVIDER_RATE_LIMIT: {
        "max_retries": 3,
        "backoff": True,
        "description": "Rate limited by provider; retry with exponential backoff.",
    },
    FailureClass.PROVIDER_SCHEMA_ERROR: {
        "max_retries": 1,
        "backoff": False,
        "description": "Schema validation error from provider; retry once.",
    },
    FailureClass.PROVIDER_MALFORMED_OUTPUT: {
        "max_retries": 0,
        "backoff": False,
        "description": "Malformed output already handled by codex_reviewer retry loop.",
    },
    FailureClass.PROVIDER_INTERRUPTED: {
        "max_retries": 0,
        "backoff": False,
        "description": "Provider interrupted by operator intent; do not retry.",
    },
    FailureClass.VERIFICATION_MISSING_BINARY: {
        "max_retries": 0,
        "backoff": False,
        "description": "Required binary missing; prompt-driven resolution needed.",
    },
    FailureClass.VERIFICATION_TEST_FAILURE: {
        "max_retries": 0,
        "backoff": False,
        "description": "Verification tests failed; handled by normal repair loop.",
    },
    FailureClass.VERIFICATION_TIMEOUT: {
        "max_retries": 1,
        "backoff": False,
        "description": "Verification step timed out; retry once.",
    },
    FailureClass.WORKSPACE_DRIFT: {
        "max_retries": 0,
        "backoff": False,
        "description": "Workspace state drifted; cannot auto-recover.",
    },
    FailureClass.REVIEW_DISAGREEMENT: {
        "max_retries": 0,
        "backoff": False,
        "description": "Reviewers disagree; handled by escalation path.",
    },
    FailureClass.DEPENDENCY_MISSING_PROJECT: {
        "max_retries": 0,
        "backoff": False,
        "description": "Project dependency missing; diagnosis-driven resolution.",
    },
    FailureClass.DEPENDENCY_MISSING_SYSTEM: {
        "max_retries": 0,
        "backoff": False,
        "description": "System dependency missing; cannot auto-recover.",
    },
    FailureClass.OPERATOR_PAUSE: {
        "max_retries": 0,
        "backoff": False,
        "description": "Operator paused execution; do not retry.",
    },
    FailureClass.INTERNAL_ERROR: {
        "max_retries": 0,
        "backoff": False,
        "description": "Unexpected internal error; do not retry.",
    },
}
