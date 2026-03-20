"""Implementation plan loading and parsing."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.types import StrictStr
import yaml
from yaml.tokens import AliasToken, AnchorToken


class PlanLoadError(ValueError):
    """Raised when a plan cannot be loaded or validated structurally."""


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    title: StrictStr
    description: StrictStr
    depends_on: list[StrictStr] = Field(default_factory=list)
    acceptance_criteria: list[StrictStr]
    verification_commands: list[StrictStr]
    expected_files: list[StrictStr]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[Task]


def _reject_anchors_and_aliases(source: str, source_name: str) -> None:
    try:
        for token in yaml.scan(source):
            if isinstance(token, (AnchorToken, AliasToken)):
                raise PlanLoadError(
                    f"YAML anchors and aliases are not allowed in {source_name}."
                )
    except yaml.YAMLError as exc:
        raise PlanLoadError(f"Invalid YAML in {source_name}: {exc}") from exc


def load_plan(path: str | Path) -> Plan:
    """Load and type-check a ralph plan file."""

    plan_path = Path(path)
    source = plan_path.read_text(encoding="utf-8")
    _reject_anchors_and_aliases(source, str(plan_path))

    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise PlanLoadError(f"Invalid YAML in {plan_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanLoadError(f"Plan {plan_path} must deserialize to a mapping.")

    try:
        return Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanLoadError(f"Invalid plan in {plan_path}: {exc}") from exc
