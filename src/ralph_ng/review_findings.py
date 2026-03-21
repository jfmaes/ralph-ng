"""ReviewFindings and FindingResponse schemas for adversarial review.

These models express severity-graded, evidence-backed findings with stable
IDs for cross-round tracking between reviewer findings and executor responses.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    MINOR = "minor"


class FindingCategory(StrEnum):
    SPEC_COMPLIANCE = "spec_compliance"
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTING = "testing"
    ARCHITECTURE = "architecture"
    STYLE = "style"


class ReviewFinding(BaseModel):
    """A single finding from the reviewer."""

    model_config = ConfigDict(extra="forbid")

    id: str  # stable identifier, e.g. "F1", "F2"
    severity: FindingSeverity
    category: FindingCategory
    description: str
    evidence: str  # file:line reference, diff hunk, or test case
    suggested_fix: str = ""
    machine_checkable: bool = False
    verification_command: str = ""

    @model_validator(mode="after")
    def validate_machine_checkable(self) -> ReviewFinding:
        if self.machine_checkable and not self.verification_command:
            raise ValueError("machine_checkable findings must include a verification_command")
        return self


class ReviewFindings(BaseModel):
    """Structured output from the reviewer — NO risk field (orchestrator-computed)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
    confidence: float = Field(ge=0.0, le=1.0)
    spec_compliance: Literal["pass", "fail", "partial"]
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    repair_notes: str = ""
    new_verification_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reject_has_findings(self) -> ReviewFindings:
        if self.decision == "reject" and not self.findings:
            raise ValueError("reject decisions must include at least one finding")
        return self


class FindingResponseVerdict(StrEnum):
    CONCEDE_AND_FIXED = "concede_and_fixed"
    ALREADY_CORRECT = "already_correct"


class FindingResponse(BaseModel):
    """Executor's structured response to a single finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    verdict: FindingResponseVerdict
    explanation: str


class ReviewResult(BaseModel):
    """Result of a reviewer invocation, wrapping transport result + parsed findings."""

    model_config = ConfigDict(extra="forbid")

    status: str  # InvocationStatus value
    findings: ReviewFindings | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    raw_output_text: str = ""
    raw_events: list[dict[str, Any]] = Field(default_factory=list)


_SCHEMA_METADATA_KEYS = frozenset({
    "default",
    "title",
    "description",
    "examples",
})


def _normalize_provider_schema(schema: Any, *, in_properties: bool = False) -> Any:
    """Normalize Pydantic JSON schema for provider structured-output APIs.

    Goals:
    - remove metadata that bloats prompts or is rejected/ignored by CLIs
    - ensure every object with properties has a complete `required` array
    - ensure object schemas are closed unless explicitly opened
    """
    if isinstance(schema, list):
        return [_normalize_provider_schema(item) for item in schema]

    if not isinstance(schema, dict):
        return schema

    normalized: dict[str, Any] = {}
    for key, value in schema.items():
        if not in_properties and key in _SCHEMA_METADATA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            normalized[key] = {
                property_name: _normalize_provider_schema(property_schema)
                for property_name, property_schema in value.items()
            }
            continue
        normalized[key] = _normalize_provider_schema(value)

    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["required"] = list(properties.keys())
        normalized.setdefault("additionalProperties", False)

    return normalized


def review_findings_json_schema() -> dict[str, Any]:
    """Export the ReviewFindings JSON schema for embedding in prompts."""
    return _normalize_provider_schema(ReviewFindings.model_json_schema())


def finding_response_json_schema() -> dict[str, Any]:
    """Export the FindingResponse JSON schema for embedding in prompts."""
    return _normalize_provider_schema(FindingResponse.model_json_schema())


def parse_review_findings(text: str) -> ReviewFindings:
    """Parse ReviewFindings from JSON text with validation."""
    return ReviewFindings.model_validate_json(text)


def parse_finding_responses(text: str) -> list[FindingResponse]:
    """Parse a JSON array of FindingResponse objects.

    Accepts either a JSON array or a JSON object with a "responses" key.
    """
    import json

    data = json.loads(text)
    if isinstance(data, list):
        return [FindingResponse.model_validate(item) for item in data]
    if isinstance(data, dict) and "responses" in data:
        return [FindingResponse.model_validate(item) for item in data["responses"]]
    raise ValueError("Expected a JSON array or object with 'responses' key")
