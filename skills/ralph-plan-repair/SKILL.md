---
name: ralph-plan-repair
description: "Repair an invalid ralph-plan.yaml for ralph-ng. Use when validate-plan fails, when a generated plan needs cleanup, or when source markdown changed and the YAML must be brought back into a valid executable state."
---

# Ralph Plan Repair

Repair `ralph-plan.yaml` until it passes `ralph validate-plan`, without
inventing task intent that is missing from the source documents.

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

- `ralph validate-plan` fails
- a generated `ralph-plan.yaml` is structurally or semantically invalid
- source planning markdown changed and the YAML drifted out of sync
- the user wants a broken plan repaired rather than regenerated from scratch

## Workflow

### Step 1: Run the validator first

Start with the actual repo command:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
ralph validate-plan --plan ralph-plan.yaml --base-dir .
```

If the repo uses a different filename or root, adjust the paths but keep the
same validation flow.

### Step 2: Separate structural errors from semantic errors

There are two layers:

- structural load errors from YAML parsing and Pydantic schema checks
- semantic validation errors from dependency, verification, and file-reference
  rules

Fix structural issues first, then rerun validation before touching semantic
rules.

### Step 3: Repair from source, not by guesswork

Use the source plan markdown, spec files, and actual repo layout to justify
changes.

- If a task is missing verification detail, derive the narrowest real command
  you can justify.
- If an `expected_files` entry points at a file that does not exist yet, make
  sure an upstream dependency produces it.
- If task intent is unclear, prefer regenerating that part from the source plan
  over inventing filler fields.

### Step 4: Iterate until clean

After each round of fixes:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
ralph validate-plan --plan ralph-plan.yaml --base-dir .
```

Stop only when the validator passes or the remaining blocker is missing source
information that the user must provide.

## Common fixes

See [references/common-errors.md](references/common-errors.md) for the actual
error-to-fix mapping grounded in `ralph-ng`'s loader and validator.

## What not to do

- Do not "fix" the plan by deleting hard tasks unless the source plan no longer
  includes them.
- Do not invent fake verification commands just to satisfy the validator.
- Do not break dependency order to silence missing-file errors.
- Do not use YAML anchors or aliases; `ralph-ng` rejects them.
