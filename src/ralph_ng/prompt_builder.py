"""Prompt construction for executor invocations."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .plan import Task


class PromptBuilder:
    """Assemble the executor prompt from task and repository context."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        spec_paths: Iterable[str | Path] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.spec_paths = [Path(path) for path in (spec_paths or [])]

    def build(
        self,
        task: Task,
        *,
        repair_notes: str | None = None,
        ratchet_overlays: list[str] | None = None,
    ) -> str:
        effective_commands = list(task.verification_commands)
        if ratchet_overlays:
            effective_commands.extend(ratchet_overlays)

        sections = [
            "# Ralph Executor Task",
            "",
            "## Task Contract",
            f"- ID: {task.id}",
            f"- Title: {task.title}",
            "- Description:",
            task.description,
            "- Depends on: "
            + (", ".join(task.depends_on) if task.depends_on else "(none)"),
            "- Acceptance criteria:",
            *[f"  - {criterion}" for criterion in task.acceptance_criteria],
            "- Effective verification commands:",
            *[f"  - {command}" for command in effective_commands],
            "",
            "## Repair Notes",
            repair_notes if repair_notes else "(none)",
            "",
            "## Spec Excerpts",
            self._render_specs(),
            "",
            "## Expected File Contents",
            self._render_expected_files(task),
            "",
            "Implement only what is required by the task contract and keep verification green.",
        ]
        return "\n".join(sections)

    def _render_specs(self) -> str:
        if not self.spec_paths:
            return "(no spec files provided)"

        rendered: list[str] = []
        for spec_path in self.spec_paths:
            content = spec_path.read_text(encoding="utf-8")
            rendered.extend(
                [
                    f"### {spec_path.name}",
                    content.rstrip(),
                    "",
                ]
            )
        return "\n".join(rendered).rstrip()

    def _render_expected_files(self, task: Task) -> str:
        rendered: list[str] = []
        for relative_path in task.expected_files:
            full_path = self.repo_root / relative_path
            rendered.append(f"### {relative_path}")
            if full_path.exists():
                rendered.append(full_path.read_text(encoding="utf-8").rstrip())
            else:
                rendered.append("(file does not exist yet)")
            rendered.append("")
        return "\n".join(rendered).rstrip()
