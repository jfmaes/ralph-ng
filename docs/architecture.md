# Ralph-ng Architecture Guide

## Overview

Ralph-ng is an autonomous coding orchestrator that coordinates multiple AI
models (Claude, Codex) to implement, review, and ship code changes. It runs
locally, stores all state in SQLite, and provides a live Textual dashboard.

## Core Loop

```
ralph start → Orchestrator._run_loop()
  │
  ├─ Scheduler.next_ready_task() → picks next task from plan
  │
  ├─ _execute_task()
  │   ├─ PromptBuilder.build() → executor prompt
  │   ├─ executor.execute() → Claude CLI (stream-json)
  │   ├─ GitManager.snapshot_patch() → save diff
  │   └─ if failed → _handle_blocked_system() → classify + self-heal
  │
  ├─ _review_task()
  │   ├─ VerificationRunner.run_commands() → run test/lint commands
  │   ├─ build_first_pass_prompt() → reviewer prompt
  │   ├─ reviewer.review() → Codex CLI (json)
  │   └─ if challenge needed → _challenge_round()
  │       ├─ build_challenge_executor_prompt() → executor repairs
  │       └─ build_challenge_reviewer_prompt() → reviewer re-evaluates
  │
  ├─ _process_review_verdict()
  │   ├─ accept → git commit → next task
  │   ├─ reject → REPAIR state → retry executor
  │   └─ escalate → BLOCKED_HUMAN → operator action needed
  │
  └─ loop continues until plan complete or blocked
```

## Adapter Architecture

```
Transport (ABC)           ← provider-neutral invocation
  ├─ CliClaudeTransport   ← claude -p <prompt> --output-format stream-json
  └─ CliCodexTransport    ← codex exec --json <prompt>

Adapter (ABC)             ← role-specific wrapping
  ├─ ClaudeExecutorAdapter  ← retry on malformed output
  └─ CodexReviewerAdapter   ← JSON schema enforcement, parse retry

Factory                   ← builds adapters from ralph.toml config
  └─ build_executor() / build_reviewer()
```

### Transport Data Flow

Transports return `TransportResult` which carries:
- `status` — InvocationStatus enum (completed, timeout, provider_error, etc.)
- `output_text` — the model's text output
- `exit_status` — real process exit code (for failure classification)
- `stderr_text` — process stderr (for failure classification)
- `raw_events` — full JSONL event stream (for debugging/replay)

## Streaming Pipeline

```
Provider CLI → JSONL stdout
    │
Transport._extract_activity()
    │ Parses events, accumulates text in buffer
    │ Flushes on: newlines, 120-char threshold, block boundaries
    │
    ▼
make_activity(kind, summary, text=full_text, block_id=..., block_state=...)
    │
    ▼
LiveActivityTracker.push()
    │ Bounded deque (maxlen=50), thread-safe
    │
    ├─→ sink callback → Orchestrator → ProviderEventStore (JSONL persistence)
    │   Maps block_id/block_state to AGENT_THOUGHT_STARTED/DELTA/FINISHED
    │
    └─→ TUI reads via ProviderEventStore.read_events()
        │ _collect_role_events() → filters by role + event type
        │ _render_role_events() → collapses thought blocks, formats with Rich Text
        │ _rewrite_role_log() → writes to RichLog widget (last 8 items)
        └─→ Displayed in full-width reasoning panes
```

### Thought Blocks

Reasoning from providers arrives as streaming deltas. The transport groups
them into "thought blocks" with a `block_id`. The TUI collapses updates
to the same thought block into a single display item with a numbered header:

```
Thought 1
  Let me analyze the authentication module...
  The issue is in JWT validation on line 42.

Thought 2
  I'll fix the validation and add a test case.
```

### ActivityEvent Fields

| Field | Purpose |
|-------|---------|
| `kind` | ActivityKind enum (COMMENTARY, PROGRESS, ERROR, RESULT, TOOL_*) |
| `summary` | Short display text (may be truncated to 80 chars) |
| `text` | Full text content (never truncated) |
| `block_id` | Groups related events into a thought block |
| `block_state` | "start", "update", or "finish" within a block |
| `significant` | Whether it appears in activity tracker display |

## State Model

### SQLite Tables

| Table | Purpose |
|-------|---------|
| `runs` | Run metadata, controller PID, heartbeat, stop reason |
| `task_states` | Task FSM state, attempt count, failure classification |
| `attempts` | Per-attempt artifacts (prompt, patch, review paths) |
| `invocations` | Provider call records (tokens, duration, exit status) |
| `recovery_attempts` | Self-healing diagnosis and auto-fix audit trail |

### Task FSM

```
PENDING → READY → IN_PROGRESS → REVIEW → DONE
                       │            │
                       │            ├─→ REPAIR (rejected) → IN_PROGRESS
                       │            ├─→ BLOCKED_HUMAN (escalated)
                       │            └─→ BLOCKED_SYSTEM (provider failure)
                       │
                       └─→ BLOCKED_SYSTEM (executor failure)
```

### Run Status

| Status | Meaning |
|--------|---------|
| `running` | Active controller is executing tasks |
| `completed` | All tasks done |
| `paused` | Gracefully stopped, resumable |
| `blocked_human` | Needs operator intervention |
| `blocked_system` | Provider/environment failure |
| `failed_exhausted` | Retry budget used up |

### Controller Ownership

Each run tracks its controller via:
- `controller_pid` — PID of the owning process
- `last_heartbeat_at` — updated every 5s by display loop + `touch_run()`
- `stop_reason` — JSON: `{"cause": "operator_interrupt"|"stale"|"force_killed"}`

Stale detection: `is_controller_alive()` checks PID liveness + heartbeat age > 30s.

## Failure Classification

When a provider call fails, `classify_failure()` maps the evidence to one of
15 `FailureClass` values. Each class has a recovery policy (retry count, backoff).

| Exit Code | Duration | Stderr | → Class |
|-----------|----------|--------|---------|
| any | any | `timed_out=True` | `PROVIDER_TIMEOUT` |
| 143 | any | any | `PROVIDER_INTERRUPTED` |
| 137 | any | any | `PROVIDER_TIMEOUT` (SIGKILL) |
| 127 | any | any | `VERIFICATION_MISSING_BINARY` |
| 1 | < 2s | any | `PROVIDER_CRASH` |
| any | any | "rate limit" | `PROVIDER_RATE_LIMIT` |
| any | any | "command not found" | `VERIFICATION_MISSING_BINARY` |

## Self-Healing

When enabled (`[self_healing]` in ralph.toml), failed tasks trigger a diagnosis
session using a separate provider (default: Claude Opus). The diagnosis:

1. Receives failure evidence (error, stderr, verification logs)
2. Classifies root cause
3. Decides if auto-fixable
4. Runs bounded fix commands (npm install, grep rewrites — never sudo/apt)
5. On success: task transitions back to READY for retry
6. On failure: enriched error message for human escalation

## Signal Handling

| Signal | Behavior |
|--------|----------|
| First Ctrl+C | Pause at next checkpoint + SIGTERM to subprocess |
| Second Ctrl+C | Force kill children + emergency release controller + exit |
| SIGTERM | Same as second Ctrl+C |
| TUI `p` | Pause at next checkpoint, dashboard stays open |
| TUI `s` | Stop at next checkpoint, dashboard closes |
| TUI `q`/Ctrl+C | Stop + close dashboard |
| `ralph stop` | Write control file, controller pauses at next checkpoint |

### Graceful Subprocess Termination

`stream_subprocess()` uses SIGTERM → 5s grace → SIGKILL, not immediate SIGKILL.
The `cancel_event` (tied to `_shutdown_requested`) interrupts mid-stream.

## Resume Logic

On `ralph resume`:

| Prior State | Workspace | Action |
|-------------|-----------|--------|
| `IN_PROGRESS` | Clean | Restart task from READY (attempt not counted) |
| `IN_PROGRESS` | Dirty | `git reset --hard` + restart from READY |
| `REVIEW` | Any | Resume reviewer |
| `REPAIR` | Any | Resume repair (rejection count restored) |
| `BLOCKED_SYSTEM` | Clean | Transition to READY |
| `BLOCKED_HUMAN` (dirty ws) | Any | Auto-reset to HEAD |
| `paused` | Any | Continue from last checkpoint |

## CLI Commands

| Command | Purpose |
|---------|---------|
| `ralph start` | Start a new run |
| `ralph resume --run-id <id>` | Resume/adopt a paused or stale run |
| `ralph watch --run-id <id>` | Observe a running instance |
| `ralph status --run-id <id>` | Dashboard snapshot |
| `ralph status --run-id <id> --json` | Machine-readable status |
| `ralph stop` | Request pause of latest active run |
| `ralph preflight` | Environment validation |
| `ralph doctor --run-id <id>` | Run health diagnosis |
| `ralph validate-plan` | Validate plan YAML |

## Configuration

See `docs/examples/ralph.toml.mixed-provider.example` for a full annotated config.

Key sections:
- `[ralph]` — branch prefix, auto-push, retry limits
- `[roles.executor]` / `[roles.reviewer]` — provider, model, CLI path per role
- `[review]` — thresholds, challenge rounds, escalation policy
- `[self_healing]` — enabled (default true), diagnosis model (default opus)
- `[timeouts]` — executor (600s), reviewer (300s), verification (120s)
- `[git]` — protected branches, force push disabled
