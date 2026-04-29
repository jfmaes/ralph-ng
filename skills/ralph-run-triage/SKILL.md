---
name: ralph-run-triage
description: "Diagnose interrupted, blocked, or stale ralph-ng runs. Use when a run is paused, blocked_system, blocked_human, or appears hung, and the next step is to decide whether to stop, resume, repair, or escalate."
---

# Ralph Run Triage

Diagnose a `ralph-ng` run before touching repo state.

## Ralph CLI setup

Before any `ralph ...` command, prefer an installed `ralph` on `PATH`. If you
need the local checkout, set `RALPH_NG_ROOT=/path/to/ralph-ng` and activate its
venv in the same shell. Activation does not persist across separate tool calls.

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then
  test -d "$RALPH_NG_ROOT"
  if [ ! -x "$RALPH_NG_ROOT/.venv/bin/ralph" ]; then
    python3 -m venv "$RALPH_NG_ROOT/.venv"
    "$RALPH_NG_ROOT/.venv/bin/pip" install -e "$RALPH_NG_ROOT[dev]"
  fi
  source "$RALPH_NG_ROOT/.venv/bin/activate"
elif ! command -v ralph >/dev/null 2>&1; then
  echo "Set RALPH_NG_ROOT=/path/to/ralph-ng or install ralph-ng on PATH." >&2
  exit 1
fi
ralph --help
```

If `RALPH_NG_ROOT` is set, `which ralph` should resolve to
`$RALPH_NG_ROOT/.venv/bin/ralph`.

## When to use this skill

- `ralph start` or `ralph resume` failed
- a run looks stale or hung
- the user wants to know whether to `resume`, `stop`, repair the workspace, or
  escalate to a human

## Workflow

### Step 1: Collect current state

Start with the supported commands:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
ralph status --run-id <id>
ralph doctor --run-id <id>
```

If needed, inspect `.git/ralph/` artifacts such as:

- `state.db`
- event JSONL files
- provider event logs
- control files used for pause/stop

### Step 2: Classify the failure mode

Separate these cases:

- clean pause or clean stop
- stale controller / abandoned run
- `blocked_system` due to provider or verification failure
- `blocked_human` due to review or repo-state problems
- dirty workspace drift

Use the recorded state and recent events, not guesswork.

### Step 3: Choose the least risky recovery

- If the run is paused cleanly, prefer `ralph resume`.
- If the controller is stale, confirm the adoption path rather than starting a
  competing run.
- If verification or environment is broken, fix the root cause before resume.
- If the workspace diverged unexpectedly, stop and explain the divergence.

### Step 4: Explain the next action concretely

End with one of:

- safe to resume now
- safe to stop and restart
- blocked pending human intervention
- blocked pending environment or plan repair

## What not to do

- Do not delete `.git/ralph/` state as a first response.
- Do not guess at run health without reading the recorded status and events.
- Do not reset or discard workspace changes unless the user explicitly asks.
