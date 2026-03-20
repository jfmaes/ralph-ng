"""Read-only review bundle assembly for evaluator invocations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .plan import Task
from .verification import VerificationResult


class ReviewBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    git_diff: str
    changed_file_snippets: dict[str, str]
    verification_results: list[VerificationResult]
    repair_notes: str | None = None
    ratchet_overlay_history: list[str] = Field(default_factory=list)

    def to_prompt(self) -> str:
        sections = [
            "# Ralph Evaluator Review Bundle",
            "",
            "## Task Contract",
            f"- ID: {self.task.id}",
            f"- Title: {self.task.title}",
            "- Description:",
            self.task.description,
            "- Acceptance criteria:",
            *[f"  - {criterion}" for criterion in self.task.acceptance_criteria],
            "",
            "## Git Diff",
            self.git_diff or "(no diff provided)",
            "",
            "## Verification Results",
        ]
        for result in self.verification_results:
            sections.extend(
                [
                    f"### {result.command}",
                    f"- success: {result.success}",
                    f"- exit_code: {result.exit_code}",
                    f"- timed_out: {result.timed_out}",
                    "- stdout:",
                    result.stdout or "(empty)",
                    "- stderr:",
                    result.stderr or "(empty)",
                    "",
                ]
            )
        sections.extend(["## Changed File Snippets"])
        for path, snippet in self.changed_file_snippets.items():
            sections.extend([f"### {path}", snippet or "(file missing)", ""])
        sections.extend(
            [
                "## Repair Notes",
                self.repair_notes or "(none)",
                "",
                "## Ratchet Overlay History",
                *([f"- {command}" for command in self.ratchet_overlay_history] or ["(none)"]),
            ]
        )
        return "\n".join(sections)


class ReviewBundleBuilder:
    """Build a read-only bundle for evaluator review."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root)

    def build(
        self,
        task: Task,
        *,
        git_diff: str,
        verification_results: list[VerificationResult],
        repair_notes: str | None = None,
        ratchet_overlay_history: list[str] | None = None,
    ) -> ReviewBundle:
        changed_file_snippets: dict[str, str] = {}
        for relative_path in task.expected_files:
            file_path = self.repo_root / relative_path
            if file_path.exists():
                changed_file_snippets[relative_path] = file_path.read_text(encoding="utf-8").rstrip()
            else:
                changed_file_snippets[relative_path] = "(file does not exist yet)"

        return ReviewBundle(
            task=task,
            git_diff=git_diff,
            changed_file_snippets=changed_file_snippets,
            verification_results=verification_results,
            repair_notes=repair_notes,
            ratchet_overlay_history=ratchet_overlay_history or [],
        )
