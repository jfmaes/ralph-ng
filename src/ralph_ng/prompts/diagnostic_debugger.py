"""Diagnostic debugger prompt for self-healing sessions.

When a task fails with a recoverable failure class, ralph can launch a
bounded diagnosis session to classify the root cause and determine
whether an automatic fix is possible.
"""

from __future__ import annotations

import json
from typing import Any


_SYSTEM_PROMPT = """\
You are a diagnostic debugger for an autonomous coding system. A task has
failed and you must determine the root cause and whether it can be auto-fixed.

YOUR JOB:
1. Analyze the failure evidence (error messages, stderr, verification logs)
2. Classify the root cause
3. Determine if the system can auto-fix it
4. If fixable: provide exact commands to run
5. If not fixable: provide exact human actions needed

FAILURE CATEGORIES:
- missing_project_dependency: A package is imported but not in the manifest.
  Auto-fixable: add to manifest and run install command.
- missing_system_dependency: A system tool (rg, fd, etc.) is not installed.
  Not auto-fixable. Suggest portable alternative or install instructions.
- verification_command_error: A verification command uses unavailable tools.
  Auto-fixable: rewrite with portable equivalent.
- provider_configuration: The AI provider CLI is misconfigured.
  Not auto-fixable. Provide exact configuration steps.
- code_defect: The implementation has a bug that caused test failure.
  Not auto-fixable by diagnosis. Needs another executor attempt.
- environment_issue: PATH, permissions, or other environment problems.
  Not auto-fixable. Provide exact fix steps.

AUTO-FIX RULES:
- You MAY recommend: npm install, pip install, adding deps to package.json,
  rewriting verification commands with portable tools
- You MUST NOT recommend: apt install, brew install, sudo anything,
  destructive git operations, credential setup

OUTPUT:
Respond with EXACTLY ONE JSON object matching this schema:
{
  "failure_class": "string (one of the categories above)",
  "root_cause_summary": "string (one sentence)",
  "confidence": 0.0-1.0,
  "can_auto_fix": true/false,
  "auto_fix_commands": ["command1", "command2"],
  "human_actions": ["action1", "action2"],
  "notes_for_resume": "string (context for the next executor attempt)"
}

CRITICAL OUTPUT RULES:
- Emit EXACTLY ONE JSON object as your entire response.
- Do NOT emit placeholder JSON, intermediate drafts, or reasoning in JSON format.
- Your response must start with '{' and end with '}'. \
"""


def build_diagnosis_prompt(
    *,
    task_id: str,
    task_title: str,
    task_description: str,
    failure_class: str,
    error_message: str,
    stderr_summary: str = "",
    verification_logs: list[dict[str, Any]] | None = None,
    workspace_diff_summary: str = "",
    dependency_manifests: dict[str, str] | None = None,
) -> str:
    """Build the diagnosis prompt for a failed task."""
    sections: list[str] = [
        _SYSTEM_PROMPT,
        "",
        "---",
        "",
        "# Failed Task",
        f"- ID: {task_id}",
        f"- Title: {task_title}",
        "- Description:",
        task_description,
        "",
        "# Failure Information",
        f"- Classification: {failure_class}",
        f"- Error: {error_message}",
    ]

    if stderr_summary:
        sections.extend([
            "",
            "# Provider Stderr (last 50 lines)",
            stderr_summary[-2000:],  # truncate
        ])

    if verification_logs:
        sections.extend(["", "# Verification Results"])
        for vr in verification_logs[:5]:  # limit
            sections.extend([
                f"## {vr.get('command', 'unknown')}",
                f"- success: {vr.get('success', False)}",
                f"- exit_code: {vr.get('exit_code', -1)}",
                f"- stdout: {str(vr.get('stdout', ''))[:500]}",
                f"- stderr: {str(vr.get('stderr', ''))[:500]}",
                "",
            ])

    if workspace_diff_summary:
        sections.extend([
            "",
            "# Workspace Diff Summary",
            workspace_diff_summary[:3000],
        ])

    if dependency_manifests:
        sections.extend(["", "# Dependency Manifests"])
        for name, content in dependency_manifests.items():
            sections.extend([
                f"## {name}",
                content[:2000],
                "",
            ])

    sections.extend([
        "",
        "# Diagnosis JSON Schema",
        "Respond with a JSON object matching the schema described above.",
    ])

    return "\n".join(sections)


DIAGNOSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "failure_class": {"type": "string"},
        "root_cause_summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "can_auto_fix": {"type": "boolean"},
        "auto_fix_commands": {"type": "array", "items": {"type": "string"}},
        "human_actions": {"type": "array", "items": {"type": "string"}},
        "notes_for_resume": {"type": "string"},
    },
    "required": ["failure_class", "root_cause_summary", "confidence", "can_auto_fix"],
}
