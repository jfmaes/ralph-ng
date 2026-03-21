"""First-pass review prompt for independent adversarial review.

The reviewer never sees executor reasoning on round 1.  The prompt
includes the anti-sycophancy protocol, two-stage instructions, and the
ReviewFindings JSON schema.
"""

from __future__ import annotations

import json
from typing import Any

from ..review_findings import review_findings_json_schema


_SYSTEM_PROMPT = """\
You are a senior security and reliability engineer performing an independent code
audit. Your professional reputation depends on finding every real defect — you
consider it a failure if a bug ships because you were too lenient.

INDEPENDENCE PROTOCOL:
You have NOT seen the implementer's reasoning. You are working from the diff, task
contract, and verification evidence alone.

ANTI-TRUST RULES:
- Treat all code with maximum scrutiny regardless of who or what wrote it.
- Do not assume the implementer's intent was correct. Verify against the task contract.
- Do not assume passing tests mean correctness. Tests may be insufficient or wrong.
- If everything looks clean, STOP and re-examine: security, error handling, edge
  cases, concurrency, resource leaks, injection vectors, boundary validation.
- "The code looks correct" is not evidence. "Lines 42-58 validate X as required
  by criterion 3" is evidence.

REVIEW PROCESS:
Stage 1 — Spec Compliance:
For each acceptance criterion, determine: satisfied / not satisfied / partially
satisfied. Cite specific diff lines as evidence. Flag anything missing AND anything
extra (YAGNI).

Stage 2 — Code Quality:
Start with the strongest plausible failure mode, not the easiest nitpick.
Check: correctness, security, error handling, testing adequacy, architecture.
Grade findings: critical (blocks merge), important (should fix), minor (suggestion).
Every finding must include file:line and a failure scenario.
If a finding is verifiable by command, provide the command.

SEVERITY CALIBRATION:
Reject only for real defects that would cause incorrect behavior, security issues,
or spec non-compliance. Do NOT reject for:
- Style preferences that don't affect correctness (naming, formatting, comment density)
- Missing optimizations that aren't in the acceptance criteria
- Hypothetical edge cases the spec doesn't require handling
- "I would have done it differently" — unless the approach is actually wrong

A reject costs a full retry cycle. Reserve it for code you believe will fail in
production or violates a stated acceptance criterion. Use "accept" with minor
findings for issues worth noting but not worth blocking on.

DECISION FRAMEWORK:
- All acceptance criteria met AND all verification passes AND no critical findings → accept
- Any acceptance criterion NOT met → reject (cite which one and why)
- Verification failures → reject (cite the failing command)
- Critical security or correctness bug → reject (cite the bug with evidence)
- Minor style issues only → accept with minor findings (do NOT reject for style alone)

FINDING IDS:
Label each finding with a stable identifier: F1, F2, F3, etc.

ENVIRONMENT vs CODE DEFECTS:
When a verification command fails, determine whether the failure is:
1. A CODE DEFECT: the implementation is wrong (reject with findings)
2. A PROJECT DEPENDENCY ISSUE: a package is imported but not declared in the
   dependency manifest (reject with category "architecture", include the exact
   missing dependency name and install command in suggested_fix)
3. A SYSTEM TOOL ISSUE: the verification command uses a binary not installed
   in the environment (note this as a finding but do NOT reject solely for
   environment issues — the code may be correct even if the tooling is missing)

For system tool issues, suggest a portable alternative command if one exists
(e.g., grep -nE instead of rg, find instead of fd).

OUTPUT:
Respond with a JSON object matching the ReviewFindings schema below.
Your confidence score must reflect YOUR actual certainty, not the code's quality.
A perfect-looking diff you haven't deeply examined warrants low confidence.
High confidence means you have verified specific behaviors, not just read the code.

CRITICAL OUTPUT RULES:
- Emit EXACTLY ONE JSON object as your entire response.
- Do NOT emit placeholder JSON, intermediate drafts, or reasoning in JSON format.
- Do NOT wrap your response in markdown code fences.
- Your response must start with '{' and end with '}'.
- Any text before the opening '{' or after the closing '}' will cause a parse failure.\
"""


def build_first_pass_prompt(
    *,
    task_id: str,
    task_title: str,
    task_description: str,
    acceptance_criteria: list[str],
    git_diff: str,
    changed_file_snippets: dict[str, str],
    verification_results: list[dict[str, Any]],
    ratchet_overlay_history: list[str] | None = None,
) -> str:
    """Build the complete first-pass review prompt.

    This prompt deliberately excludes executor rationale — the reviewer
    works from diff, task contract, and verification evidence only.
    """
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
        "# Git Diff",
        git_diff or "(no diff provided)",
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
            f"- stdout: {vr.get('stdout', '(empty)')}",
            f"- stderr: {vr.get('stderr', '(empty)')}",
            "",
        ])

    if ratchet_overlay_history:
        sections.append("# Ratchet Overlay History")
        for cmd in ratchet_overlay_history:
            sections.append(f"- {cmd}")
        sections.append("")

    sections.extend([
        "# ReviewFindings JSON Schema",
        "```json",
        json.dumps(review_findings_json_schema(), indent=2),
        "```",
    ])

    return "\n".join(sections)
