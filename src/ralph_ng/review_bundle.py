"""Read-only review bundle assembly for reviewer invocations."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Iterable

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
            "# Ralph Review Bundle",
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
    """Build a read-only bundle for reviewer review."""

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
        return ReviewBundle(
            task=task,
            git_diff=git_diff,
            changed_file_snippets=collect_changed_file_snippets(self.repo_root, task.expected_files),
            verification_results=verification_results,
            repair_notes=repair_notes,
            ratchet_overlay_history=ratchet_overlay_history or [],
        )


def collect_changed_file_snippets(repo_root: str | Path, expected_files: Iterable[str]) -> dict[str, str]:
    snippets: dict[str, str] = {}
    root = Path(repo_root)
    for relative_path in expected_files:
        file_path = root / relative_path
        if not file_path.exists():
            snippets[relative_path] = "(file does not exist yet)"
            continue
        snippets[relative_path] = _read_review_snippet(file_path)
    return snippets


def _read_review_snippet(file_path: Path) -> str:
    try:
        return file_path.read_text(encoding="utf-8").rstrip()
    except UnicodeDecodeError:
        mime_type, _ = mimetypes.guess_type(file_path.name)
        size = file_path.stat().st_size
        detail = mime_type or "application/octet-stream"
        return f"(binary file: {detail}, {size} bytes)"
    except OSError as exc:
        return f"(unable to read file: {exc})"
