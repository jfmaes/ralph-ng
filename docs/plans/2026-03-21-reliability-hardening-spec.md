# Ralph-ng Reliability Hardening Spec

Date: 2026-03-21
Status: Draft (v2 — reviewed and revised)
Scope: Prompt changes, TUI changes, orchestration/runtime changes, resume logic, dependency handling, self-healing, and operator updates

---

## Revision notes

This is a revised version of the original draft. Changes are backed by evidence
from the codebase and maiden-voyage run artifacts, not hand-waving. Key
revisions:

1. **Data model simplified**: original proposed 5 new tables; revised uses column
   additions to existing tables plus 1 new table. Evidence: the `runs` and
   `task_states` tables already carry most of the required state — adding columns
   avoids join overhead, migration complexity, and orphan-row risks.

2. **Phase order changed**: prompt hardening moved from Phase 4 to Phase 1b.
   Evidence: 3 of 13 maiden-voyage failures (runs `991f`, `84a1`, `3198`) were
   caused by multi-JSON reviewer output. The fix is a one-line prompt change
   ("emit exactly one JSON object") that costs zero runtime work. Deferring it
   to Phase 4 means every test of Phases 1-3 still hits the same parser failures.

3. **Missing failure modes added**: original spec covered 5 failure classes from
   10 runs. Revised adds 5 additional failures found in the data.

4. **Concrete bugs identified**: `_rejection_counts` memory-only loss on resume,
   challenge round not updating `patch_path`, TUI quit orphaning orchestrator
   thread and child processes, race condition in `start_command`.

5. **Self-healing scoped more tightly**: diagnosis role is valuable but expensive.
   Moved to Phase 4 (was Phase 3) and made opt-in via config.

6. **Runtime dependency detection deferred**: import-to-package heuristics (§6.2
   original) are fragile and ecosystem-specific. Moved to a future phase.
   Preflight checks (concrete, testable) remain in Phase 2.

---

## Why this spec exists

The current codebase has the right core pieces for a reliable autonomous loop:

- Durable SQLite state (WAL mode, immediate commits)
- Resumable task FSM with per-attempt artifact persistence
- Persisted prompts, patches, reviews, and provider event streams
- Live Textual dashboard with executor/reviewer activity streaming
- Adversarial two-round review with bounded challenge and escalation

The maiden-voyage runs in `/home/jenkins/projects/timezone-test/.git/ralph`
(13 runs, 83 invocations, 2026-03-20 through 2026-03-21) show that the
remaining reliability issues are operational hardening, not missing architecture:

- Abrupt process death leaves runs stuck in `running` (4 runs)
- Provider/parser failures stop the loop even when automated recovery is possible (3 runs)
- Dependency/tooling failures are discovered late and handled inconsistently (2 runs)
- The UI shows state but does not give operators control or clear next actions
- Human escalation happens before the system has exhausted bounded self-healing options
- Rapid provider crashes go undiagnosed (1 run)
- Signal-killed processes produce ambiguous exit codes (1 run)

This document turns those observations into a concrete implementation plan.

---

## Evidence from the codebase and maiden-voyage runs

### Observed failure classes

#### Class 1: Reviewer schema/parser failures stop the run

Evidence:

- `run-20260321-000723-617246-991f`: Codex rejected the response_format schema
  with `invalid_json_schema` — "Missing 'suggested_fix' in required array".
  The `ReviewFinding` Pydantic model has `suggested_fix: str = ""` (default
  empty string, not required), but `review_findings_json_schema()` may emit it
  as required depending on how the schema is built for the provider.
  File: `src/ralph_ng/review_findings.py:41`

- `run-20260321-005811-595197-84a1`: Challenge reviewer (round 2) produced
  malformed output. The raw output contains a placeholder JSON followed by
  3-5 iteratively refined JSON objects concatenated without delimiters.
  The multi-object fallback parser in `codex_reviewer.py:165-195` found
  candidates but none validated against ReviewFindings after retries exhausted.

- `run-20260321-011708-139470-3198`: Same pattern — 4 separate JSON objects
  in reviewer output. First is a placeholder, last contains the actual findings.
  The parser tried `reversed(candidates)` but Pydantic validation failed.

Current code paths:
- Schema generation: `src/ralph_ng/review_findings.py` — `review_findings_json_schema()`
- Parse retry loop: `src/ralph_ng/adapters/codex_reviewer.py:37-142` — 2 retries,
  re-prompts with "single valid JSON object" reminder
- Multi-object fallback: `src/ralph_ng/adapters/codex_reviewer.py:165-195`
- Terminal handling: `orchestrator.py:571-577` — any non-COMPLETED reviewer
  status → `_handle_blocked_system()` → run stops

Root cause: The reviewer prompt (`prompts/reviewer_first_pass.py:70`) says
"Respond with a JSON object matching the ReviewFindings schema" but does NOT
say "emit exactly one JSON object" or "do not emit intermediate JSON". Codex's
reasoning mode produces multiple refinement passes, each shaped like JSON.

#### Class 2: Runs remain `running` long after the controller is gone

Evidence from SQLite state and event history:

- `run-20260321-014539-773160-958d`: status=`running`, task=`review`,
  last event is `reviewer.started` with no subsequent events. ~33 minutes stale.
- `run-20260321-020322-556203-aaea`: status=`running`, task=`review`,
  last event is `reviewer.started`. Executor took 309s for a critical-risk
  diff (2682 lines, 23 files).
- `run-20260321-021426-845303-e57b`: status=`running`, task=`in_progress`,
  last event is `executor.started`. 50+ minutes stale.
- `run-20260321-021528-230896-1fde`: status=`running`, task=`in_progress`,
  last event is `executor.started`. 50+ minutes stale.

Current code paths:
- Heartbeat: emitted every 5s by the display thread (`cli.py:184-189`,
  `tui.py:201-208`), NOT by the orchestrator thread
- Resumable-run query: `state.py:195-207` —
  `get_latest_resumable_run()` matches `status IN ('running', 'blocked_human',
  'blocked_system')` with no staleness check
- No process liveness verification — a run with `updated_at` from 3 hours ago
  is treated identically to one updated 2 seconds ago

Why the heartbeat architecture matters: The orchestrator runs in a background
thread (`cli.py:165-169`, `tui.py:163-168`) and blocks on synchronous
`executor.execute()` / `reviewer.review()` calls. These calls invoke
`stream_subprocess()` which blocks reading stdout line-by-line
(`streaming.py:186-189`). The display thread is the only code that runs on a
timer. Moving heartbeats to the orchestrator thread alone would NOT work because
the orchestrator is blocked in I/O during provider calls. The correct fix is:
heartbeat updates from BOTH the display loop (as now) AND from within
`stream_subprocess()` on each line of output, so that heartbeats continue
even during long provider calls.

#### Class 3: No graceful shutdown — signals produce ambiguous state

Evidence:

- `run-20260321-021903-180320-ea8d`: executor exited with status 143
  (128 + 15 = SIGTERM). The run transitioned to `blocked_system` with error
  "Claude exited with status 143". This is indistinguishable from a provider
  crash vs. an operator Ctrl+C.

- All 4 stale `running` runs in Class 2 are consistent with abrupt process exit
  (Ctrl+C or terminal close) that killed the Python process before any cleanup.

Current code paths:
- `stream_subprocess()` (`streaming.py:157-166`): timeout handler sends SIGKILL
  (not SIGTERM first). No grace period.
- `cli.py`: zero signal handlers. Ctrl+C raises KeyboardInterrupt in the display
  thread; the orchestrator thread has no way to catch it.
- `tui.py:89-91`: only binding is `("q", "quit", "Quit")`. Pressing `q` calls
  `app.exit()` which tears down Textual but does NOT stop the orchestrator worker
  thread or its child processes.

**Bug: TUI quit orphans the orchestrator.** When the user presses `q`,
`app.exit()` is called. The orchestrator worker thread (`tui.py:158-159`)
continues running as a daemon thread. `run_textual_progress()` then raises
`RuntimeError("Textual dashboard exited before the run produced an outcome.")`
at line 331, but the underlying `claude`/`codex` subprocess may still be alive.
The run is left as `running` with no cleanup.

#### Class 4: Dependency/tooling failures discovered late

Evidence:

- `run-20260321-022217-841230-1a62`: Executor ran for 372.7s building a Vite +
  React + Shadcn scaffold. Reviewer correctly identified missing dependencies
  (`class-variance-authority`, `clsx`, `tailwind-merge`) imported by generated
  shadcn components but not declared in `package.json`. Escalated to
  `blocked_human` at confidence 0.93.

- `run-20260321-024539-435354-3943`: TASK-004 consumed 4 attempts because
  verification commands used `rg` (ripgrep), which is not installed. The ratchet
  system correctly rejected `rg` commands (return code 127 detection at
  `ratchet.py:160-171`), but the EXECUTOR kept producing `rg`-based commands,
  and the REVIEWER kept suggesting "install rg or use a portable equivalent".
  Eventually fixed on attempt 4 when the executor used `grep -nE` instead.

Current code paths:
- Verification runner (`verification.py`): runs commands and reports pass/fail.
  Does not classify failure category (missing binary vs. test failure vs. timeout).
  This is correct — verification.py is the wrong layer for classification.
- Ratchet validation (`ratchet.py:124-147`): already rejects missing-binary
  commands (rc=127, "command not found" in stderr). The real problem is upstream:
  prompts don't instruct models to prefer portable commands.
- Executor prompt (`prompt_builder.py`): no instruction about dependency
  management or portable command usage.

#### Class 5: Rapid provider crash (NOT in original spec)

Evidence:

- `run-20260321-021132-538094-eeeb`: Claude CLI exited with status 1 after only
  0.72 seconds. The run went to `blocked_system`. No diagnostic information was
  captured — the error message is just "Claude exited with status 1".

This is a distinct failure class: the provider CLI itself crashed or failed to
initialize (bad arguments, missing config, rate limit, auth failure). The current
code treats it identically to a 10-minute timeout, but the appropriate response
is different: retry immediately (it's likely transient) or diagnose the CLI setup.

Current code path: `orchestrator.py:395-401` — all non-COMPLETED executor
results go through `_handle_blocked_system()` with a generic message.

#### Class 6: Pre-spec baseline timeout failures (NOT in original spec)

Evidence:

- `run-20260320-203324-720041-18e6`: First-ever run. TASK-001 attempt 1 timed
  out at 600s. TASK-001 attempt 2 timed out at 1800s (the timeout was increased
  between attempts). TASK-002 also timed out.

This is the "initial calibration" failure class — the timeout config was wrong
for the complexity of the task. The system has no mechanism to suggest timeout
adjustments based on task complexity or to distinguish "the model is making
progress but slowly" from "the model is stuck".

#### Class 7: Hung executor with no progress events (NOT in original spec)

Evidence:

- Runs `e57b` and `1fde` both show `executor.started` with no subsequent
  provider events. These occurred within 1 minute of each other (02:14:26 and
  02:15:28), suggesting a systemic issue (CLI hang, rate limit, auth session
  expiry) rather than isolated incidents.

Current code: `stream_subprocess()` waits for stdout lines (`streaming.py:186-189`).
If the provider CLI hangs without producing output, the only escape is the
timeout timer. But the timeout kills with SIGKILL, which produces no diagnostic
stderr.

#### Class 8: `_rejection_counts` lost on resume (concrete bug, NOT in original spec)

Code evidence: `orchestrator.py:173` — `self._rejection_counts: dict[str, int] = {}`
is initialized to empty in `__init__`. This dict tracks consecutive rejections
for escalation policy (`should_escalate()` checks `rejection_count >= config.escalation.max_rejection_loops`).

When the orchestrator dies and resumes, `_rejection_counts` resets to zero.
This means a task that was rejected 2 times (out of a max of 3) before a crash
can get rejected 3 more times after resume before escalating, for a total of 5
instead of the configured 3. This defeats the escalation safety bound.

Fix: persist `rejection_count` in `task_states.last_error` JSON or in a new column.

#### Class 9: Challenge round doesn't update attempt patch_path (concrete bug, NOT in original spec)

Code evidence: `orchestrator.py:696-701` — after the challenge executor runs,
a new patch is created at `challenge_patch_path` and written to disk, but the
attempt record's `patch_path` (set at line 386-393) still points to the
pre-challenge patch.

When `ResumeManager._resume_blocked_task()` at `resume.py:308-316` uses
`latest_attempt.patch_path` for drift detection, it compares against the
PRE-challenge workspace state. If the challenge executor made changes, the
drift detection will incorrectly flag the workspace as diverged.

Fix: call `store.update_attempt_artifacts(attempt_id, patch_path=str(challenge_patch_path))`
after the challenge patch is written.

#### Class 10: Race condition in `start_command` (concrete bug, NOT in original spec)

Code evidence: `cli.py:299-304` — `get_latest_resumable_run()` checks for
existing resumable runs, then `create_run()` creates a new one. There is no
lock between these operations. If two `ralph start` processes run
simultaneously, both can pass the check and create conflicting runs that
operate on the same git branch.

Fix: use a SQLite `INSERT ... WHERE NOT EXISTS` pattern or a file-based lock
in `.git/ralph/`.

### Summary of what is already working

The current system provides a strong base:

- Persisted artifacts per run and attempt (prompts, patches, reviews, command logs)
- Provider event streams per task/attempt/round (JSONL)
- Patch snapshots for drift detection
- Resumable FSM states with head-rollback recovery
- Challenge rounds with machine-checkable finding verification
- Human escalation payloads with structured findings and suggested actions
- Multi-object JSON fallback parser (works in many cases; just needs the prompt
  to reduce multi-object output in the first place)
- Ratchet system with binary-existence validation and dangerous-pattern blocking

The hardening work extends the current model, not replaces it.

---

## Goals

1. Make `running` mean "a live controller owns this run right now", not "once started".
2. Turn SIGINT/SIGTERM, TUI quit, and controller crashes into explicit, resumable
   states with precise reasons.
3. Recover automatically from bounded, known failure classes before escalating.
4. Detect environment and dependency blockers earlier; classify them when they occur.
5. Give operators clearer live status, control, and recovery context.
6. Preserve single-repo, local-first architecture. Keep state durable and auditable.

## Non-goals

1. Full daemon/service mode in this phase.
2. Arbitrary autonomous system-package installation.
3. Unlimited self-healing loops or open-ended agent recursion.
4. Replacing the current FSM or review architecture wholesale.
5. Runtime import-to-package heuristic scanning (fragile, ecosystem-specific — defer).
6. External notification integrations (webhooks, Slack) — the `--json` output
   enables these without core changes.

## Reliability design principles

1. Prefer explicit classification over generic `blocked_system`.
2. Persist every recovery decision, not just the final outcome.
3. Treat run ownership separately from task lifecycle.
4. Keep recovery bounded by time, count, and scope.
5. Escalate with context only after auto-recovery paths are exhausted.
6. Optimize for resumability after interruption, not just happy-path completion.
7. **Cheapest fix first**: a prompt change that prevents 3 failures costs less
   than a runtime parser that recovers from them.

---

## Proposed design

## 1. Run lifecycle hardening

### 1.1 Controller ownership via column additions (simplified from original)

The original spec proposed a `run_leases` table with 8 columns. This is
over-engineered for the current single-process, single-host model. The same
semantics can be achieved with columns on the existing `runs` table:

Add to `runs`:
- `controller_pid INTEGER` — PID of the owning process
- `controller_started_at TEXT` — when this controller took ownership
- `last_heartbeat_at TEXT` — last heartbeat timestamp (separate from `updated_at`)
- `stop_reason TEXT` — structured JSON: `{"cause": "sigint"|"sigterm"|"timeout"|"error"|"completed"|"paused", "detail": "..."}`
- `phase_checkpoint TEXT` — last completed phase for the active task (e.g., `executor_finished`, `review_round_1_started`)

Why columns, not a table:
- A run has at most one controller at a time. A separate table models a
  one-to-one relationship with extra join cost and orphan-row risk.
- If multi-host or multi-controller support is needed later, a table can be
  added then. YAGNI now.

Staleness detection:
- A controller is stale if `last_heartbeat_at` is older than
  `STALE_THRESHOLD_SECONDS` (default: 30s, configurable) AND the PID is not
  alive (checked via `os.kill(pid, 0)`).
- A run with no `controller_pid` and status `running` is orphaned.

### 1.2 Heartbeat from both display and subprocess layers

The original spec said "move heartbeat ownership from UI thread to controller."
This is partially wrong. The orchestrator thread CANNOT heartbeat during provider
calls because it blocks on `stream_subprocess()` stdout iteration
(`streaming.py:186-189`).

Correct approach — heartbeat from two places:

1. **Display loop** (keep existing): `cli.py:184-189` and `tui.py:201-208`
   continue updating `last_heartbeat_at` every 5 seconds. This covers the
   "controller process is alive" signal.

2. **Subprocess streaming** (add new): `stream_subprocess()` updates
   `last_heartbeat_at` on each stdout line received. This covers the "provider
   is producing output" signal. Requires passing a heartbeat callback into
   `stream_subprocess()`.

3. **Headless mode** (add new): when running without a terminal
   (`cli.py:146-147`), spawn a lightweight heartbeat thread that updates
   `last_heartbeat_at` every 5 seconds. The orchestrator thread is still blocked
   on provider calls, so a separate thread is needed.

Result: `last_heartbeat_at` is always fresh while the controller is alive,
regardless of terminal mode or provider call duration.

### 1.3 Stale-run reconciliation

On `ralph start`:
- Query `get_latest_resumable_run()` as today
- If found and stale (heartbeat expired + PID dead): mark status `paused` with
  `stop_reason = {"cause": "stale", "detail": "controller PID {pid} not alive"}`.
  Allow new run creation.
- If found and live (heartbeat fresh or PID alive): refuse, suggest
  `ralph watch --run-id ...` or `ralph resume --run-id ...`.
- If found with no `controller_pid` (pre-migration run): treat as stale.

On `ralph resume`:
- Same staleness check. If stale, adopt by updating `controller_pid` and
  `controller_started_at`.

CLI additions:
- `ralph doctor --run-id <id>`: inspect state, heartbeat freshness, PID
  liveness, last checkpoint, and print recommended next step.

`ralph adopt` from the original spec is unnecessary — `ralph resume` handles
adoption automatically when the old controller is confirmed dead.

### 1.4 Fix the start_command race condition

Replace the check-then-insert pattern in `cli.py:299-304` with an atomic
approach:

```python
# Instead of:
resumable = store.get_latest_resumable_run()
if resumable is not None:
    raise ValueError(...)
store.create_run(...)

# Use:
try:
    store.create_run_if_no_active(...) # INSERT WHERE NOT EXISTS
except ActiveRunExists as e:
    raise ValueError(...)
```

Or use a file lock: `fcntl.flock()` on `.git/ralph/controller.lock`.

---

## 2. Prompt hardening (moved from Phase 4 to Phase 1b)

**Why this is Phase 1b, not Phase 4:** Three of 13 maiden-voyage failures were
caused by multi-JSON reviewer output. The fix is a prompt-only change. No
runtime code required. Every test of subsequent phases will benefit from fewer
parser failures. Deferring this means debugging new features while the
reviewer still intermittently produces unparseable output.

### 2.1 Reviewer prompt: single-output enforcement

Add to `prompts/reviewer_first_pass.py` system prompt, after the OUTPUT section:

```
CRITICAL OUTPUT RULES:
- Emit EXACTLY ONE JSON object as your entire response.
- Do NOT emit placeholder JSON, intermediate drafts, or reasoning in JSON format.
- Do NOT wrap your response in markdown code fences.
- Your response must start with '{' and end with '}'.
- Any text before the opening '{' or after the closing '}' will cause a parse failure.
```

Add the same rules to `prompts/challenge_reviewer.py`.

Evidence this is needed: runs `84a1` and `3198` both show Codex emitting a
placeholder `{"decision": "reject", ...}` followed by 3-5 refined versions.
The multi-object parser prefers the last valid one, but when none validate
(due to schema differences in intermediate drafts), the run stops.

### 2.2 Reviewer prompt: dependency-vs-code defect distinction

Add to the reviewer system prompt:

```
ENVIRONMENT vs CODE DEFECTS:
When a verification command fails, determine whether the failure is:
1. A CODE DEFECT: the implementation is wrong (reject with findings)
2. A PROJECT DEPENDENCY ISSUE: a package is imported but not declared in the
   dependency manifest (reject with category "architecture", include the exact
   missing dependency name and install command)
3. A SYSTEM TOOL ISSUE: the verification command uses a binary not installed
   in the environment (note this as a finding but do NOT reject solely for
   environment issues — the code may be correct even if the tooling is missing)

For system tool issues, suggest a portable alternative command if one exists
(e.g., grep -nE instead of rg, find instead of fd).
```

Evidence: run `3943` TASK-004 looped because the reviewer rejected for `rg`
absence, which is an environment issue, not a code defect.

### 2.3 Executor prompt: dependency management contract

Add to the executor system prompt (`prompt_builder.py`):

```
DEPENDENCY MANAGEMENT:
- Before adding any import, check whether the package is declared in the
  project's dependency manifest (package.json, requirements.txt, pyproject.toml,
  Cargo.toml, etc.).
- If you add a new dependency, update the manifest AND run the install command
  (npm install, pip install, etc.) in the same attempt.
- If a system tool is unavailable, prefer a portable alternative:
  rg → grep -nE, fd → find, bat → cat, exa → ls
- Do NOT assume any tool beyond POSIX coreutils + the project's declared toolchain
  is available.

VERIFICATION COMMANDS:
- All verification commands you produce must use only POSIX-portable tools or
  tools declared in the project's dependency manifest.
- Do NOT use rg, fd, bat, exa, delta, or other optional CLI tools in verification
  commands unless they are declared project dependencies.
```

Evidence: run `1a62` — executor generated shadcn components that import
`class-variance-authority` without adding it to `package.json`. Run `3943` —
executor produced `rg`-based verification commands on a machine without ripgrep.

### 2.4 Challenge executor prompt: environment fix permission

Add to `prompts/challenge_executor.py`:

```
If a finding identifies a missing project dependency (package not in manifest):
- You MAY add the dependency to the manifest and run the install command.
- This counts as fixing the finding.

If a finding identifies a missing system tool in a verification command:
- Replace the command with a portable equivalent.
- Do NOT attempt to install system packages.
```

---

## 3. Graceful pause and shutdown

### 3.1 Signal handling

Install SIGINT/SIGTERM handlers in `cli.py` before starting the orchestrator.

Behavior:
- **First SIGINT**: set a threading.Event `shutdown_requested`. The orchestrator
  checks this event at safe points (between tasks, before starting a new provider
  call). Persist `stop_reason = {"cause": "operator_interrupt"}` and transition
  run to `paused`.
- **Second SIGINT within 3 seconds**: escalate to forced stop. Send SIGTERM to
  active provider subprocess, wait 5s, then SIGKILL. Persist
  `stop_reason = {"cause": "forced_interrupt"}`.
- **SIGTERM**: same as second SIGINT (immediate graceful stop).

Safe checkpoint locations in the orchestrator loop:
1. `_run_loop()` top of `while True` — before `_next_task()` (line 221)
2. `_execute_task()` — before `executor.execute()` (line 353)
3. `_review_task()` — before `reviewer.review()` (line 532)
4. `_challenge_round()` — before each provider call (lines 659, 764)

At each checkpoint: `if self._shutdown_requested.is_set(): return self._handle_pause(run_id, task)`

### 3.2 TUI quit cleanup

Fix the TUI quit bug. When the user presses `q`:

1. Set `shutdown_requested` event on the orchestrator
2. Wait for the orchestrator worker thread to finish (with timeout)
3. If the worker is still alive after timeout, send SIGTERM to child processes
4. Then exit the Textual app

Implementation: add an `on_shutdown()` method to `RalphDashboardApp` that
coordinates with the orchestrator thread before calling `app.exit()`.

### 3.3 Make subprocess termination graceful

Replace the current SIGKILL-only timeout in `stream_subprocess()`:

```
Current (streaming.py:157-166):
    SIGKILL immediately on timeout

Proposed:
    1. SIGTERM to process group
    2. Wait grace_seconds (default 5s)
    3. If still alive, SIGKILL
    4. Return StreamResult with reason_code:
       - "timeout_graceful" (SIGTERM worked)
       - "timeout_forced" (SIGKILL required)
       - "operator_interrupt" (shutdown_requested)
```

Also add a `cancel()` method to `stream_subprocess` (or wrap it in a class)
so the orchestrator can request cancellation from outside.

### 3.4 Add `paused` run status

Add `paused` to `RunStatus` enum (`orchestrator.py:43-48`). A paused run is
explicitly resumable without any reconciliation — the controller stopped at a
clean checkpoint.

### 3.5 Run control via file-based signal (simplified from original)

The original spec proposed a `run_control_requests` table with 7 columns.
This is over-engineered for a single-process model where the controller is the
only writer.

Simpler approach: use a control file at `.git/ralph/<run_id>.control`. The
orchestrator polls this file at each checkpoint. The TUI or CLI writes to it.

Content: single line, one of `pause`, `stop`, `retry_review`.

After handling, the orchestrator deletes the file.

Why this is better:
- No database contention between the TUI thread and orchestrator thread
- Works from external scripts (`echo pause > .git/ralph/<run_id>.control`)
- Trivial to implement and test
- If database-backed control is needed later, it can be added

---

## 4. Failure classification and structured recovery

### 4.1 Failure class enum

Add `src/ralph_ng/failure.py`:

```python
class FailureClass(StrEnum):
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CRASH = "provider_crash"          # exit code != 0, very short duration
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_SCHEMA_ERROR = "provider_schema_error"
    PROVIDER_MALFORMED_OUTPUT = "provider_malformed_output"
    PROVIDER_INTERRUPTED = "provider_interrupted"  # SIGTERM/SIGINT
    VERIFICATION_TEST_FAILURE = "verification_test_failure"
    VERIFICATION_MISSING_BINARY = "verification_missing_binary"
    VERIFICATION_TIMEOUT = "verification_timeout"
    WORKSPACE_DRIFT = "workspace_drift"
    REVIEW_DISAGREEMENT = "review_disagreement"
    DEPENDENCY_MISSING_PROJECT = "dependency_missing_project"
    DEPENDENCY_MISSING_SYSTEM = "dependency_missing_system"
    OPERATOR_PAUSE = "operator_pause"
    INTERNAL_ERROR = "internal_error"
```

### 4.2 Failure classifier

Add a `classify_failure()` function that examines:
- Exit code (127 = missing binary, 143 = SIGTERM, 1 = generic error)
- Duration (< 2s with exit code 1 = likely CLI crash, not task failure)
- Stderr content ("command not found", "rate limit", "authentication")
- Verification result patterns

Evidence-based rules:
- Exit 143 + timeout → `PROVIDER_TIMEOUT` (run `ea8d`)
- Exit 1 + duration < 2s → `PROVIDER_CRASH` (run `eeeb`, 0.72s)
- Exit 127 in verification → `VERIFICATION_MISSING_BINARY` (run `3943`)
- Malformed output after retries → `PROVIDER_MALFORMED_OUTPUT` (runs `84a1`, `3198`)
- Schema validation error from provider → `PROVIDER_SCHEMA_ERROR` (run `991f`)

### 4.3 Recovery policy

Each failure class maps to an automatic recovery action:

| Failure class | Auto-recovery | Budget | Evidence |
|---|---|---|---|
| `PROVIDER_TIMEOUT` | Retry with same timeout | 1 retry | Transient provider overload |
| `PROVIDER_CRASH` | Retry immediately (provider CLI issue) | 2 retries | run `eeeb`: 0.72s crash |
| `PROVIDER_RATE_LIMIT` | Backoff + retry | 3 retries, exp backoff | Standard API behavior |
| `PROVIDER_SCHEMA_ERROR` | Rebuild schema without optional fields | 1 retry | run `991f`: required/optional mismatch |
| `PROVIDER_MALFORMED_OUTPUT` | Already handled by codex_reviewer retry loop | existing 2 retries | runs `84a1`, `3198` |
| `PROVIDER_INTERRUPTED` | No retry (operator intent) | 0 | run `ea8d` |
| `VERIFICATION_MISSING_BINARY` | Suggest portable alternative in repair notes | 0 (prompt-driven) | run `3943` |
| `VERIFICATION_TEST_FAILURE` | Normal repair loop | existing max_retries | Working as designed |
| `WORKSPACE_DRIFT` | Block human | 0 | Working as designed |
| `OPERATOR_PAUSE` | No retry | 0 | Intentional |

The auto-recovery budget is per-task, per-failure-class. Once exhausted,
escalate with the failure class, all retry history, and the raw evidence.

### 4.4 Persist failure classification

Add a `failure_class` column to `task_states`:

```sql
ALTER TABLE task_states ADD COLUMN failure_class TEXT;
ALTER TABLE task_states ADD COLUMN failure_detail_json TEXT;
```

`failure_detail_json` stores: `{"class": "...", "retries": N, "evidence_paths": [...], "recovery_actions_taken": [...]}`

This replaces the need for separate `task_failures` and `recovery_attempts`
tables proposed in the original spec. The task_states row is already the
canonical record of a task's current condition.

---

## 5. Concrete bug fixes

These are bugs found during code review that should be fixed regardless of
which phase they fall in.

### 5.1 Persist rejection counts across resume

`orchestrator.py:173` — `_rejection_counts` is in-memory only.

Fix: when transitioning to REPAIR state, encode the rejection count in the
`last_error` JSON payload:

```python
# In _process_review_verdict, when transitioning to REPAIR:
repair_payload = json.dumps({
    "repair_notes": repair_notes or findings.summary,
    "rejection_count": rejection_count,
})
self._transition(run_id, task.id, TaskState.REPAIR,
    attempt_count=attempt_number, last_error=repair_payload)
```

On resume, `_resume_active_task` for REPAIR state should parse the rejection
count from `last_error` and restore `_rejection_counts[task_id]`.

### 5.2 Update patch_path after challenge round

`orchestrator.py:696-701` — challenge patch is written but attempt record
is not updated.

Fix: add after line 701:

```python
self.store.update_attempt_artifacts(
    latest_attempt.id,
    patch_path=str(challenge_patch_path),
)
```

### 5.3 Transport health_check before expensive operations

The `Transport` ABC defines `health_check()` but it is never called.

Fix: call `self.executor.transport.health_check()` (if available) before
the first task in `_run_loop()`. If it fails, transition to
`BLOCKED_SYSTEM` with `PROVIDER_CRASH` failure class immediately, before
spending 600s on a timeout.

### 5.4 Provider event persistence gap

Several runs show `executor.started` or `reviewer.started` as the last event,
with no `AGENT_FINISHED` or error event. This happens when the process is killed
externally (SIGKILL from timeout or Ctrl+C).

Fix: wrap each provider call in the orchestrator with a try/finally that emits
an `AGENT_FINISHED` event with `status=interrupted` if no normal completion
event was emitted. This ensures provider event logs are always complete.

---

## 6. Environment preflight

### 6.1 `ralph preflight` command

Before the first task, validate:
- Required CLIs for configured providers (`claude`, `codex`) — check
  `shutil.which()` and `transport.health_check()`
- Package manager presence when the repo has a lockfile or manifest
  (`package.json` → `npm`/`pnpm`/`yarn`/`bun`, `requirements.txt` → `pip`, etc.)
- Binaries referenced in plan verification commands — parse each plan task's
  `verification_commands` and check `shutil.which()` for the first token
- Git cleanliness (existing check)

Output: list of pass/fail checks with actionable messages.

`ralph start` runs preflight automatically. Failures are warnings, not hard
blocks (the operator may know the tool will be installed by a task).

### 6.2 Verification command portability in ratchet

Extend `ratchet.py._validate_command()` to suggest portable alternatives when
a command uses a known non-portable tool:

```python
PORTABLE_ALTERNATIVES = {
    "rg": "grep -rnE",
    "fd": "find . -name",
    "bat": "cat",
    "exa": "ls",
    "delta": "diff",
}
```

When a command's first token is in `PORTABLE_ALTERNATIVES` and `shutil.which()`
confirms it's missing: reject with a message that includes the portable
alternative. This is better than auto-rewriting because the rewrite may not be
semantically equivalent.

---

## 7. Resume and recovery hardening

### 7.1 Expand resume for paused state

Add handling for `status = "paused"` in `ResumeManager`:
- If the task's `phase_checkpoint` is `executor_finished` or later: resume from REVIEW
- If the task's `phase_checkpoint` is before `executor_finished`: resume from READY
  (restart the executor attempt)
- If paused between challenge rounds: resume from the last review checkpoint

### 7.2 Restore rejection counts on resume

When resuming a REPAIR-state task, parse `_rejection_counts` from the
structured `last_error` payload and restore it in the orchestrator.

### 7.3 Stale-run auto-recovery on resume

When `ralph resume` encounters a `running` run with a dead controller:
1. Mark the old controller as stale
2. Update `controller_pid` to current process
3. Classify each task in `in_progress` or `review` state:
   - Has patch artifact → resume from REVIEW
   - No patch artifact → reset to READY (restart attempt)
4. Continue the run loop

---

## 8. TUI and operator experience

### 8.1 Control keybindings (implement with graceful shutdown)

Add Textual bindings:
- `p` — pause run (writes `pause` to control file, sets `shutdown_requested`)
- `s` — graceful stop (same as first Ctrl+C)
- `Ctrl+C` / `q` — graceful stop with TUI cleanup (fix the orphan bug)

### 8.2 Controller status in dashboard

Add to the summary bar:
- Controller PID and uptime
- Heartbeat age ("last heartbeat 2s ago" / "STALE: no heartbeat for 45s")
- Mode indicator: "controlling" vs "watching"

### 8.3 Recovery/failure panel

When a task is in a failure state, show below the task table:
- Failure class (from the new enum)
- Recovery actions taken so far
- Next automatic action (if any remain in budget)
- Exact human actions needed (if escalated)
- Downstream tasks blocked

### 8.4 Stale-run banner

If watching a run with a stale controller heartbeat:
- Red banner: "Controller (PID {pid}) has not heartbeated for {N}s — run may be orphaned"
- Show: `ralph resume --run-id {id}` to adopt

### 8.5 `ralph status --json`

Add `--json` flag to the `status` command that emits machine-readable output:

```json
{
  "run_id": "...",
  "status": "running",
  "controller_pid": 12345,
  "controller_alive": true,
  "last_heartbeat_age_seconds": 3.2,
  "tasks": {...},
  "current_task": "TASK-003",
  "phase": "review",
  "failure_class": null,
  "cost": {...}
}
```

This enables external tooling (monitoring, notifications) without core changes.

---

## 9. Self-healing diagnostic sessions (opt-in, Phase 4)

### 9.1 Design rationale and cost awareness

**Moved from Phase 3 to Phase 4** because:
1. Each diagnosis call costs provider tokens + wall time (30-120s typically).
2. Phases 1-3 (prompt fixes, signal handling, failure classification) address
   the majority of maiden-voyage failures without extra provider calls.
3. Diagnosis is most valuable AFTER failure classification exists, so it can
   act on structured failure data rather than raw stderr.

**Opt-in via config:**

```toml
[self_healing]
enabled = false
diagnosis_provider = "codex"  # or "claude"
max_diagnosis_per_task = 1
max_auto_fix_actions_per_task = 2
diagnosis_timeout_seconds = 120
```

### 9.2 Diagnosis role

When enabled and a task reaches `BLOCKED_SYSTEM` with a recoverable failure
class, launch a diagnosis session:

- Input: task contract, last executor prompt, review artifacts, verification
  logs, provider stderr, workspace diff, failure classification
- Output (structured JSON): `failure_class`, `root_cause_summary`, `confidence`,
  `can_auto_fix`, `recommended_commands[]`, `human_actions[]`

### 9.3 Auto-fix scope

Allowed:
- `npm install` / `pip install` for project dependencies
- Verification command rewrite (portable alternatives)
- Retry a provider call with adjusted parameters

Not allowed:
- System package installation (apt, brew, etc.)
- Credential/auth flows
- Destructive git operations
- Open-ended "keep trying" loops

### 9.4 Provider strategy

Use the opposite provider from the one that failed:
- If executor (Claude) failed → diagnose with Codex
- If reviewer (Codex) failed → diagnose with Claude
- Config override available

---

## 10. Data model changes (consolidated)

All changes are column additions to existing tables. One new optional table.

### Additions to `runs` table:

```sql
ALTER TABLE runs ADD COLUMN controller_pid INTEGER;
ALTER TABLE runs ADD COLUMN controller_started_at TEXT;
ALTER TABLE runs ADD COLUMN last_heartbeat_at TEXT;
ALTER TABLE runs ADD COLUMN stop_reason TEXT;         -- JSON
ALTER TABLE runs ADD COLUMN phase_checkpoint TEXT;
```

### Additions to `task_states` table:

```sql
ALTER TABLE task_states ADD COLUMN failure_class TEXT;
ALTER TABLE task_states ADD COLUMN failure_detail_json TEXT;  -- JSON
```

### New table (Phase 4 only, for self-healing):

```sql
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    failure_class TEXT NOT NULL,
    recovery_kind TEXT NOT NULL,   -- 'retry', 'diagnosis', 'dependency_fix'
    status TEXT NOT NULL,          -- 'attempted', 'succeeded', 'failed'
    summary TEXT,
    artifact_path TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
```

### Why this is simpler than the original

Original proposed 5 new tables (run_leases, run_control_requests,
task_failures, task_checkpoints, recovery_attempts) with ~40 columns total.

Revised uses 7 column additions + 1 optional table (Phase 4 only).

The information that the original stored in `run_leases` is now in `runs`
columns. The information in `task_failures` and `task_checkpoints` is now in
`task_states` columns. The `run_control_requests` table is replaced by a
file-based control channel.

---

## 11. CLI surface changes

### New commands:
- `ralph preflight` — environment validation
- `ralph doctor --run-id <id>` — inspect run health and recommend next step
- `ralph status --json` — machine-readable status

### Behavior changes:
- `ralph start` — runs preflight, checks controller liveness (not just run status)
- `ralph resume` — auto-adopts stale runs, restores rejection counts, handles `paused` state
- `ralph watch` — shows stale-controller banner, remains read-only

### Removed from original:
- `ralph adopt` — merged into `ralph resume` (adoption is automatic for dead controllers)
- `ralph pause` / `ralph stop` — handled via TUI keybindings and file-based control
- `ralph incidents` — deferred to when incident summaries are implemented

---

## 12. Testing plan

### 12.1 Concrete bug fix tests

- `test_rejection_count_survives_resume`: orchestrator with 2 rejections → crash
  → resume → verify count is 2, not 0
- `test_challenge_patch_path_updated`: challenge round → verify attempt record
  points to challenge patch, not pre-challenge patch
- `test_start_race_condition`: two concurrent `start` calls → only one succeeds
- `test_tui_quit_stops_subprocess`: press `q` → verify child process receives
  SIGTERM and run transitions to `paused`

### 12.2 Signal handling tests

- `test_sigint_pauses_run`: send SIGINT during provider call → run status = `paused`,
  stop_reason contains `operator_interrupt`
- `test_double_sigint_forces_stop`: two SIGINTs within 3s → run status = `paused`,
  child process killed
- `test_sigterm_graceful_stop`: SIGTERM → SIGTERM to child, wait 5s, SIGKILL if needed

### 12.3 Failure classification tests

- `test_classify_provider_crash`: exit 1 + 0.72s duration → `PROVIDER_CRASH`
- `test_classify_provider_timeout`: exit 143 + timed_out=True → `PROVIDER_TIMEOUT`
- `test_classify_missing_binary`: exit 127 → `VERIFICATION_MISSING_BINARY`
- `test_classify_rate_limit`: stderr contains "rate limit" → `PROVIDER_RATE_LIMIT`

### 12.4 Resume hardening tests

- `test_resume_paused_run`: paused run with `phase_checkpoint=executor_finished` →
  resumes from REVIEW, not READY
- `test_resume_stale_running`: running run with dead PID → auto-adopted, continues
- `test_resume_with_rejection_count`: REPAIR state with `rejection_count=2` in
  `last_error` → orchestrator starts with `_rejection_counts[task_id] = 2`

### 12.5 Prompt hardening tests

- `test_reviewer_single_json_output`: mock Codex producing single JSON → parses OK
- `test_reviewer_prompt_contains_single_output_rule`: verify prompt text includes
  "EXACTLY ONE JSON object"

### 12.6 Maiden-voyage replay tests

Fixture-driven tests using actual run artifacts:
- Schema error replay (run `991f`)
- Multi-JSON reviewer output replay (runs `84a1`, `3198`)
- Rapid provider crash replay (run `eeeb`)
- Orphaned review-started replay (run `958d`)
- Dependency escalation replay (run `1a62`)
- Missing `rg` portability loop replay (run `3943`)

---

## 13. Phased implementation plan

### Phase 1a: Prompt hardening and concrete bug fixes (smallest blast radius, highest ROI)

Deliverables:
- Reviewer prompt: single-output enforcement (§2.1)
- Reviewer prompt: dependency-vs-code distinction (§2.2)
- Executor prompt: dependency management contract (§2.3)
- Challenge prompt: environment fix permission (§2.4)
- Bug fix: rejection count persistence (§5.1)
- Bug fix: challenge patch_path update (§5.2)
- Bug fix: provider event persistence gap (§5.4)
- Tests: §12.1, §12.5

Why first:
- Zero runtime risk — prompt and orchestrator-logic-only changes
- Directly prevents 3 of 13 maiden-voyage failures (multi-JSON)
- Fixes 2 concrete bugs that affect resume reliability
- Can ship and test in isolation before touching signal handling or data model

### Phase 1b: Graceful shutdown and signal handling

Deliverables:
- SIGINT/SIGTERM handlers in cli.py (§3.1)
- TUI quit cleanup — fix orphan bug (§3.2)
- Graceful subprocess termination: SIGTERM → wait → SIGKILL (§3.3)
- `paused` run status (§3.4)
- File-based run control channel (§3.5)
- TUI control keybindings: p, s, q (§8.1)
- Tests: §12.2

Why second:
- Directly prevents the 4 "stuck running" failures from maiden voyage
- Requires data model changes (next) to persist stop reasons properly

### Phase 2: Run ownership and failure classification

Deliverables:
- Column additions to `runs` and `task_states` tables (§10)
- Controller PID/heartbeat tracking (§1.1)
- Heartbeat from subprocess streaming + headless mode (§1.2)
- Stale-run reconciliation (§1.3)
- Start command race fix (§1.4)
- Failure class enum and classifier (§4.1, §4.2)
- Recovery policy with auto-retry for transient failures (§4.3)
- Failure classification persistence (§4.4)
- Transport health_check before first task (§5.3)
- `ralph preflight` command (§6.1)
- Ratchet portability suggestions (§6.2)
- `ralph doctor` command
- Tests: §12.3, §12.4, §12.6

Why third:
- Run ownership requires schema changes; schema changes should be batched
- Failure classification is most useful after prompt fixes reduce noise
- Preflight validates the environment before wasting provider tokens

### Phase 3: Resume hardening and TUI improvements

Deliverables:
- Resume support for `paused` state (§7.1)
- Rejection count restoration on resume (§7.2)
- Stale-run auto-recovery on resume (§7.3)
- Controller status in dashboard (§8.2)
- Recovery/failure panel (§8.3)
- Stale-run banner (§8.4)
- `ralph status --json` (§8.5)
- Tests: §12.4 (remaining)

Why fourth:
- Resume logic depends on the new data model columns from Phase 2
- TUI improvements depend on failure classification from Phase 2

### Phase 4: Self-healing diagnostic sessions (opt-in)

Deliverables:
- Diagnosis role and prompt (§9.2)
- Auto-fix scope enforcement (§9.3)
- Recovery attempts table (§10)
- Config section for self-healing (§9.1)
- Provider strategy for diagnosis (§9.4)

Why last:
- Most expensive feature (extra provider calls per failure)
- Phases 1-3 address the majority of observed failures without diagnosis
- Requires all prior phases to be stable before adding autonomous recovery

---

## 14. Acceptance criteria

1. A run whose controller dies is detectable as stale/orphaned within
   `STALE_THRESHOLD_SECONDS` and resumable via `ralph resume`.
2. SIGINT/SIGTERM produce a persisted `paused` state with structured stop reason.
   Resume from paused skips reconciliation and continues from the last checkpoint.
3. TUI `q` cleanly stops the orchestrator and child processes before exiting.
4. Reviewer multi-JSON output rate drops by >80% after prompt hardening
   (measurable via maiden-voyage replay tests).
5. Rejection counts survive resume. A task that was rejected 2/3 times before
   crash only gets 1 more retry after resume, not 3.
6. Challenge round patch_path correctly reflects post-challenge state.
7. Missing project dependencies are identified in reviewer findings with
   specific package names and install commands.
8. `ralph preflight` catches missing provider CLIs and verification command
   binaries before the first task starts.
9. `ralph doctor --run-id <id>` prints actionable diagnosis for any run state.
10. All maiden-voyage failure cases have regression test coverage.

---

## Appendix: Maiden-voyage run index

| Run ID | Status | Failure class | Root cause |
|---|---|---|---|
| `18e6` | blocked_system | PROVIDER_TIMEOUT | Timeout config too low for TASK-001 complexity |
| `991f` | running (stale) | PROVIDER_SCHEMA_ERROR | `suggested_fix` required/optional mismatch in schema |
| `ee67` | running (stale) | PROVIDER_TIMEOUT | Reviewer hung on complex diff |
| `84a1` | blocked_system | PROVIDER_MALFORMED_OUTPUT | Multi-JSON reviewer output, challenge round 2 |
| `3198` | blocked_system | PROVIDER_MALFORMED_OUTPUT | Multi-JSON reviewer output, 4 concatenated objects |
| `958d` | running (stale) | PROVIDER_TIMEOUT | Reviewer started, no completion event |
| `aaea` | running (stale) | PROVIDER_TIMEOUT | Critical-risk diff (2682 lines), reviewer hung |
| `eeeb` | blocked_system | PROVIDER_CRASH | Claude CLI exit 1 after 0.72s |
| `e57b` | running (stale) | PROVIDER_TIMEOUT | Executor hung, no output |
| `1fde` | running (stale) | PROVIDER_TIMEOUT | Executor hung, no output (1 min after `e57b`) |
| `ea8d` | blocked_system | PROVIDER_INTERRUPTED | Exit 143 (SIGTERM), likely operator Ctrl+C |
| `1a62` | blocked_human | DEPENDENCY_MISSING_PROJECT | shadcn imports without package.json entries |
| `3943` | blocked_human | VERIFICATION_MISSING_BINARY + REVIEW_DISAGREEMENT | `rg` not installed; TASK-004 looped 4 attempts |
