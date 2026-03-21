"""Challenge round — final reviewer prompt.

Sent to the reviewer after the executor has repaired and responded.
The reviewer produces a final verdict on the new diff.
"""

from __future__ import annotations

import json
from typing import Any

from ..review_findings import FindingResponse, ReviewFinding, review_findings_json_schema


_SYSTEM_PROMPT = """\
You previously reviewed this code and produced findings. The implementer has
responded with repairs and explanations.

Your job: evaluate the NEW diff against the original task contract.

RULES:
1. Review the NEW diff independently. Do not anchor on your previous findings.
2. For each original finding:
   - If fixed: mark resolved
   - If the implementer claims "already_correct": verify their evidence. If their
     evidence is sound, concede. If not, maintain the finding with explanation.
   - If the fix introduced new issues: report them as new findings.
3. Your confidence must reflect the current state, not your prior assessment.
4. A repair that addresses your findings but introduces new problems is still
   a reject.

ACCEPTING CORRECTIONS:
If the implementer's "already_correct" response includes specific file:line
evidence that your original finding was wrong, accept the correction. Do not
re-raise the same finding with different wording. Persistence is warranted
only when you have NEW evidence the implementer's defense is wrong.

DECISION FRAMEWORK (same as first pass):
- All acceptance criteria met AND verification passes AND no critical findings → accept
- Re-reject ONLY for real defects, not style disagreements or findings the
  implementer correctly defended

OUTPUT:
Respond with a JSON object matching the ReviewFindings schema below.
This is the FINAL verdict. Be thorough — there are no more rounds after this.

CRITICAL OUTPUT RULES:
- Emit EXACTLY ONE JSON object as your entire response.
- Do NOT emit placeholder JSON, intermediate drafts, or reasoning in JSON format.
- Do NOT wrap your response in markdown code fences.
- Your response must start with '{' and end with '}'.
- Any text before the opening '{' or after the closing '}' will cause a parse failure.\
"""


def build_challenge_reviewer_prompt(
    *,
    task_id: str,
    task_title: str,
    task_description: str,
    acceptance_criteria: list[str],
    original_findings: list[ReviewFinding | dict[str, Any]],
    executor_responses: list[FindingResponse | dict[str, Any]],
    new_git_diff: str,
    changed_file_snippets: dict[str, str],
    verification_results: list[dict[str, Any]],
) -> str:
    """Build the challenge-round final reviewer prompt."""
    sections: list[str] = [
        _SYSTEM_PROMPT,
        "",
        "---",
        "",
        "# Task Contract",
        f"- ID: {task_id}",
        f"- Title: {task_title}",
        "- Description:",
        task_description,
        "- Acceptance criteria:",
    ]
    for criterion in acceptance_criteria:
        sections.append(f"  - {criterion}")

    sections.extend([
        "",
        "# Original Findings",
    ])
    for finding in original_findings:
        if isinstance(finding, dict):
            fid = finding.get("id", "?")
            severity = finding.get("severity", "?")
            description = finding.get("description", "")
        else:
            fid = finding.id
            severity = finding.severity
            description = finding.description
        sections.append(f"- {fid} [{severity}]: {description}")

    sections.extend([
        "",
        "# Executor Responses",
    ])
    for response in executor_responses:
        if isinstance(response, dict):
            fid = response.get("finding_id", "?")
            verdict = response.get("verdict", "?")
            explanation = response.get("explanation", "")
        else:
            fid = response.finding_id
            verdict = response.verdict
            explanation = response.explanation
        sections.append(f"- {fid}: {verdict} — {explanation}")

    sections.extend([
        "",
        "# New Git Diff",
        new_git_diff or "(no diff provided)",
        "",
    ])

    if changed_file_snippets:
        sections.append("# Changed File Snippets")
        for path, snippet in changed_file_snippets.items():
            sections.extend([f"## {path}", snippet or "(empty)", ""])

    sections.append("# Verification Results")
    for vr in verification_results:
        sections.extend([
            f"## {vr.get('command', 'unknown')}",
            f"- success: {vr.get('success', False)}",
            f"- exit_code: {vr.get('exit_code', -1)}",
            "",
        ])

    sections.extend([
        "# ReviewFindings JSON Schema",
        "```json",
        json.dumps(review_findings_json_schema(), indent=2),
        "```",
    ])

    return "\n".join(sections)
