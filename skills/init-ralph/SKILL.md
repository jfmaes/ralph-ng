---
name: init-ralph
description: "Initialize a repository for ralph-ng autonomous coding. Use when a repo needs ralph.toml, a validated ralph-plan.yaml, specs/ layout, and preflight checks before the first run."
---

# Init Ralph

Prepare a repository so `ralph-ng` can run against it safely.

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

- The user says "init ralph", "set up ralph", or "make this repo ralphable"
- The repo does not yet have a complete `ralph.toml`, `ralph-plan.yaml`, or
  `specs/` layout
- The goal is readiness for `ralph validate-plan`, `ralph preflight`, and then
  `ralph start`

## Workflow

### Step 1: Inspect the repo

Check for:

- git repo status and cleanliness
- `ralph.toml`
- `ralph-plan.yaml`
- root `spec.md` or `specs/*.md`
- test tooling referenced by the plan

### Step 2: Fill the missing pieces

- If there is no plan yet but there is markdown planning material, use
  `skills/ralph-plan-converter/SKILL.md`.
- If there is no `specs/` directory but there is a root `spec.md`, copy it into
  `specs/`.
- If `ralph.toml` is missing, create a minimal config that matches the
  available providers and CLI binaries.

### Step 3: Validate before running

Run:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
ralph validate-plan --plan ralph-plan.yaml --base-dir .
ralph preflight
```

Repair any validation or environment failures before proceeding.

### Step 4: Start only when the repo is ready

Before `ralph start`, confirm:

- the plan validates
- preflight is acceptable
- the git worktree is clean
- the configured provider CLIs actually exist

If any of those are false, stop and explain the blocker.

## What not to do

- Do not start a run against an invalid plan.
- Do not start with a dirty tree unless the user explicitly accepts the risk and
  the tool behavior allows it.
- Do not guess provider config; verify the available CLIs and models first.
