# Ralph-ng Prompt Reference

All prompts are in `src/ralph_ng/prompts/` and `src/ralph_ng/prompt_builder.py`.

---

## Executor System Prompt

**File:** `src/ralph_ng/prompt_builder.py` — `_EXECUTOR_SYSTEM`

**Used when:** Every executor invocation (task implementation and repair attempts).

**Key instructions:**
- Read surrounding code before writing — understand conventions
- Implement ONLY what the task contract requires
- Run verification commands BEFORE finishing
- Use subagents for parallelism

**Dependency management contract:**
- Check dependency manifest before adding imports
- If adding a new dep, update manifest AND run install in the same attempt
- Prefer portable alternatives: `rg` → `grep -nE`, `fd` → `find`
- Do NOT assume any tool beyond POSIX coreutils is available
- Verification commands must use only portable or declared tools

**Repair notes:** Included when `mode == REPAIR`. Describes specific defects
from the reviewer. Executor must fix those issues, not rewrite from scratch.

**Checklist appended to every prompt:**
- All acceptance criteria addressed
- All verification commands pass
- No unrelated files modified
- Code matches project conventions
- Newly imported packages declared in manifest
- Verification commands use portable tools

---

## Reviewer First-Pass Prompt

**File:** `src/ralph_ng/prompts/reviewer_first_pass.py` — `_SYSTEM_PROMPT`

**Used when:** First independent review of executor output. Reviewer has NOT
seen executor reasoning.

**Key instructions:**
- Independence protocol: work from diff, task contract, and verification only
- Anti-trust rules: maximum scrutiny, no assumptions about implementer intent
- Two-stage review: spec compliance → code quality
- Severity calibration: reject only for real defects, not style preferences

**Environment vs code defect distinction:**
- CODE DEFECT → reject with findings
- PROJECT DEPENDENCY ISSUE → reject with category "architecture", include
  exact package name and install command
- SYSTEM TOOL ISSUE → note as finding, do NOT reject solely for environment

**Critical output rules:**
- EXACTLY ONE JSON object as entire response
- No placeholder JSON, intermediate drafts, or markdown fences
- Response must start with `{` and end with `}`

**Schema:** ReviewFindings JSON schema is appended at the end.

---

## Challenge Executor Prompt

**File:** `src/ralph_ng/prompts/challenge_executor.py` — `_SYSTEM_PROMPT`

**Used when:** Reviewer produced findings and the challenge round is triggered.
Executor must repair code and respond to each finding.

**Key instructions:**
- REPAIR FIRST, then explain
- For each finding: `concede_and_fixed` or `already_correct` (with evidence)
- No arguing without evidence — cite file:line
- Run verification commands after repairs

**Environment fix permission:**
- MAY add missing project dependencies to manifest and install
- MAY replace non-portable verification commands with portable equivalents
- MUST NOT install system packages

**Output:** JSON array of FindingResponse objects (one per finding ID).

---

## Challenge Reviewer Prompt

**File:** `src/ralph_ng/prompts/challenge_reviewer.py` — `_SYSTEM_PROMPT`

**Used when:** After executor has repaired and responded to findings.
Reviewer produces final verdict on the new diff.

**Key instructions:**
- Review NEW diff independently, don't anchor on previous findings
- Accept corrections when evidence is sound
- Persistence only warranted with NEW evidence
- This is the FINAL verdict — no more rounds

**Critical output rules:** Same as first-pass (exactly one JSON object).

---

## Diagnostic Debugger Prompt

**File:** `src/ralph_ng/prompts/diagnostic_debugger.py` — `_SYSTEM_PROMPT`

**Used when:** Self-healing is enabled and a task fails with a recoverable
failure class.

**Key instructions:**
- Analyze failure evidence
- Classify root cause into categories:
  `missing_project_dependency`, `missing_system_dependency`,
  `verification_command_error`, `provider_configuration`,
  `code_defect`, `environment_issue`
- Determine if auto-fixable

**Auto-fix rules:**
- MAY recommend: npm install, pip install, manifest updates, portable rewrites
- MUST NOT recommend: apt/brew install, sudo, destructive git, credential setup

**Output:** Single JSON object with:
```json
{
  "failure_class": "string",
  "root_cause_summary": "string",
  "confidence": 0.0-1.0,
  "can_auto_fix": true/false,
  "auto_fix_commands": ["..."],
  "human_actions": ["..."],
  "notes_for_resume": "string"
}
```

---

## ReviewFindings Schema

**File:** `src/ralph_ng/review_findings.py`

The structured output schema enforced on reviewer responses:

```
ReviewFindings:
  decision: "accept" | "reject"
  confidence: 0.0 - 1.0
  spec_compliance: str
  summary: str
  findings: [ReviewFinding]
  repair_notes: str
  new_verification_commands: [str]

ReviewFinding:
  id: str (F1, F2, ...)
  severity: critical | important | minor
  category: spec_compliance | correctness | security | testing | architecture | style
  description: str
  evidence: str (file:line reference)
  suggested_fix: str
  machine_checkable: bool
  verification_command: str (required if machine_checkable)
```

---

## FindingResponse Schema

**File:** `src/ralph_ng/review_findings.py`

The executor's response to reviewer findings in challenge rounds:

```
FindingResponse:
  finding_id: str (matches ReviewFinding.id)
  verdict: "concede_and_fixed" | "already_correct"
  explanation: str
```

---

## Prompt Construction Flow

```
1. PromptBuilder.build(task, repair_notes, ratchet_overlays)
   → System prompt + task contract + spec excerpts + expected files + checklist

2. build_first_pass_prompt(task, diff, snippets, verification_results)
   → Reviewer system prompt + task contract + diff + verification + schema

3. build_challenge_executor_prompt(task, findings, summary)
   → Challenge system prompt + findings with IDs + response schema

4. build_challenge_reviewer_prompt(task, findings, responses, new_diff, verification)
   → Challenge reviewer prompt + original findings + executor responses + new diff + schema

5. build_diagnosis_prompt(task, failure_class, error, stderr, verification_logs)
   → Diagnostic system prompt + failure evidence + diagnosis schema
```
