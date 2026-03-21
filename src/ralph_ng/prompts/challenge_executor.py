"""Challenge round — executor response prompt.

Sent to the executor (Claude) when the reviewer has produced findings.
The executor must repair first, then explain.
"""

from __future__ import annotations

import json
from typing import Any

from ..review_findings import FindingResponse, ReviewFinding, finding_response_json_schema


_SYSTEM_PROMPT = """\
A code reviewer has examined your implementation and produced findings.
Your job: fix every valid finding, then explain.

RULES:
1. REPAIR FIRST. Make all code changes before responding to findings.
2. For each finding, respond with one of:
   - concede_and_fixed: "I fixed this. Here's what changed: [specific description]"
   - already_correct: "This finding is incorrect because [specific evidence with
     file:line reference]"
3. Do NOT argue without evidence. "I believe my implementation is correct" is not
   a valid response. "Line 45 already handles this case via the guard clause at
   line 42" is valid.
4. Do NOT dismiss findings by restating your original intent. The reviewer doesn't
   care what you intended — they care what the code does.
5. If you concede a finding but the fix requires changing the approach significantly,
   explain the new approach and why it's better.
6. Run the verification commands after all repairs. If they fail, fix the failures
   before responding.

WHEN TO USE already_correct:
- The reviewer misread the code (cite the exact lines they missed)
- The reviewer flagged something the spec explicitly does NOT require
- The reviewer's proposed fix would break something else (explain what)
Do NOT concede findings you genuinely believe are wrong just to be agreeable.
False concessions waste a repair cycle and may introduce unnecessary changes.

ENVIRONMENT FIXES:
If a finding identifies a missing project dependency (package not in manifest):
- You MAY add the dependency to the manifest and run the install command.
- This counts as fixing the finding.
If a finding identifies a missing system tool in a verification command:
- Replace the command with a portable equivalent (e.g., rg → grep -nE, fd → find).
- Do NOT attempt to install system packages.

FORBIDDEN:
- "The reviewer raises a good point" (just fix it)
- "I respectfully disagree" (show evidence or concede)
- Dismissing any finding without specific counter-evidence

OUTPUT:
After making all repairs, respond with a JSON array of FindingResponse objects
(one per finding ID) matching the schema below.\
"""


def build_challenge_executor_prompt(
    *,
    task_id: str,
    task_title: str,
    findings: list[ReviewFinding | dict[str, Any]],
    reviewer_summary: str,
) -> str:
    """Build the challenge-round executor response prompt.

    Includes the reviewer's findings with IDs so the executor can
    reference them in structured responses.
    """
    sections: list[str] = [
        _SYSTEM_PROMPT,
        "",
        "---",
        "",
        f"# Task: {task_id} — {task_title}",
        "",
        "# Reviewer Findings",
        f"Summary: {reviewer_summary}",
        "",
    ]

    for finding in findings:
        if isinstance(finding, dict):
            fid = finding.get("id", "?")
            severity = finding.get("severity", "?")
            category = finding.get("category", "?")
            description = finding.get("description", "")
            evidence = finding.get("evidence", "")
            suggested_fix = finding.get("suggested_fix", "")
        else:
            fid = finding.id
            severity = finding.severity
            category = finding.category
            description = finding.description
            evidence = finding.evidence
            suggested_fix = finding.suggested_fix or ""

        sections.extend([
            f"## {fid} [{severity}] ({category})",
            description,
            f"Evidence: {evidence}",
        ])
        if suggested_fix:
            sections.append(f"Suggested fix: {suggested_fix}")
        sections.append("")

    sections.extend([
        "# FindingResponse JSON Schema",
        "```json",
        json.dumps(finding_response_json_schema(), indent=2),
        "```",
    ])

    return "\n".join(sections)
