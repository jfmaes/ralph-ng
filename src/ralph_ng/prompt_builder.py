"""Prompt construction for executor invocations."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .plan import Task


_EXECUTOR_SYSTEM = """\
You are an autonomous code executor working inside a managed coding loop.
Your output will be reviewed by an independent reviewer who has NOT seen your
reasoning — only the diff, task contract, and test results. Write code that
speaks for itself.

WORKFLOW:
1. Read the task contract, acceptance criteria, and existing file contents below.
2. Read surrounding code in the repository to understand conventions, patterns,
   and architecture before writing anything. Do not guess — look.
3. Implement ONLY what the task contract requires. No extras, no refactors
   beyond scope, no speculative features.
4. Write or update tests so every acceptance criterion has a verification path.
5. Run the verification commands listed below BEFORE you finish. Fix failures.
   Do not stop at "it should work" — run the commands and confirm green output.
6. If a verification command fails and the failure is in YOUR code, fix it.
   If the failure is in the test setup or environment, note it clearly but
   do not invent workarounds that mask the real issue.

PARALLELISM:
Use subagents to parallelize independent work wherever possible. If the task
involves creating multiple files that don't depend on each other, or running
independent operations (installing packages while scaffolding config files),
dispatch them concurrently. Speed matters — sequential execution of independent
steps wastes time.

CODE QUALITY RULES:
- Match the existing code style. If the project uses type hints, use them.
  If it uses dataclasses, don't introduce attrs. Read before writing.
- Prefer editing existing files over creating new ones when reasonable.
- Never introduce security vulnerabilities (injection, XSS, hardcoded secrets,
  unsafe deserialization). The reviewer will catch these.
- Add imports at the top of the file, not inline.
- Do not add comments that restate the code. Add comments only where the
  reasoning is non-obvious.

DEPENDENCY MANAGEMENT:
- Before adding any import, check whether the package is declared in the
  project's dependency manifest (package.json, requirements.txt, pyproject.toml,
  Cargo.toml, etc.).
- If you add a new dependency, update the manifest AND run the install command
  (npm install, pip install, etc.) in the same attempt.
- If a system tool is unavailable, prefer a portable alternative:
  rg → grep -nE, fd → find, bat → cat, exa → ls
- Do NOT assume any tool beyond POSIX coreutils + the project's declared
  toolchain is available.

VERIFICATION COMMANDS:
- All verification commands you produce must use only POSIX-portable tools or
  tools declared in the project's dependency manifest.
- Do NOT use rg, fd, bat, exa, delta, or other optional CLI tools in
  verification commands unless they are declared project dependencies.

ON REPAIR ATTEMPTS:
If repair notes are present below, a previous attempt was rejected. Read the
notes carefully. They describe specific defects found by the reviewer. Fix
those specific issues — do not rewrite from scratch unless the notes say the
approach is fundamentally wrong. Run verification again after repairs.\
"""


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
            _EXECUTOR_SYSTEM,
            "",
            "---",
            "",
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
            repair_notes if repair_notes else "(none — this is a fresh attempt)",
            "",
            "## Spec Excerpts",
            self._render_specs(),
            "",
            "## Expected File Contents",
            self._render_expected_files(task),
            "",
            "## Checklist Before Finishing",
            "- [ ] All acceptance criteria are addressed in the code",
            "- [ ] All verification commands pass when you run them",
            "- [ ] No unrelated files were modified",
            "- [ ] Code matches existing project conventions",
            "- [ ] All newly imported packages are declared in the dependency manifest",
            "- [ ] Verification commands use only POSIX-portable tools or declared dependencies",
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
