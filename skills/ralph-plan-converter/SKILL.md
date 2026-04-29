---
name: ralph-plan-converter
description: "Convert markdown spec and implementation plan documents into a validated ralph-plan.yaml and specs/ layout for ralph-ng. Use when a repo has spec.md, implementation plans, or task markdown that must become a ralph plan before running validate-plan or start."
---

# Ralph Plan Converter

Convert human-readable markdown specs and implementation plans into a
`ralph-plan.yaml` that `ralph-ng` can execute.

## Ralph CLI setup

Before running the bundled converter or any `ralph ...` command, prefer an
installed `ralph` on `PATH`. If you need the local checkout, set
`RALPH_NG_ROOT=/path/to/ralph-ng` and activate its venv in the same shell.
Activation does not persist across separate tool calls.

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

- The repo has `spec.md`, `implementation_plan.md`, or similar planning docs
- The user asks to "generate a ralph plan", "make this ralphable", or "convert
  this plan to ralph-plan.yaml"
- The current repo needs `ralph-plan.yaml` plus a `specs/` layout before
  running `ralph validate-plan` or `ralph start`

## Workflow

### Step 1: Inspect the planning docs

Read the markdown spec and implementation plan first. Identify:

- task boundaries
- explicit dependencies
- test commands or test-file references
- files each task creates or modifies

### Step 2: Generate the draft YAML

Preferred path when the markdown uses `### Task N: Title` headings:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
python "${RALPH_NG_ROOT:-.}/skills/ralph-plan-converter/scripts/markdown_to_ralph_plan.py" \
  implementation_plan.md \
  --output ralph-plan.yaml \
  --repo-root .
```

If the repo also has a root spec file, copy it into `specs/` at the same time:

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
python "${RALPH_NG_ROOT:-.}/skills/ralph-plan-converter/scripts/markdown_to_ralph_plan.py" \
  implementation_plan.md \
  --output ralph-plan.yaml \
  --repo-root . \
  --spec spec.md
```

Manual fallback:

1. Read the whole plan.
2. Create `ralph-plan.yaml` task by task.
3. Preserve task wording inside `description`; do not compress away context.
4. Validate and repair until the YAML passes.

### Step 3: Review the draft critically

Treat script output as a structured draft, not ground truth. Check:

- `verification_commands`: use the narrowest real command justified by source
- `expected_files`: list every file the task creates or modifies
- `depends_on`: valid `TASK-NNN` references only
- `acceptance_criteria`: concrete, testable statements

### Step 4: Repair weak inferences

If the markdown does not justify a real verification command or touched-file
list, stop and tell the user what is missing. Do not invent filler commands or
placeholder paths.

### Step 5: Validate

```bash
if [ -n "${RALPH_NG_ROOT:-}" ]; then source "$RALPH_NG_ROOT/.venv/bin/activate"; fi
ralph validate-plan --plan ralph-plan.yaml --base-dir .
```

Iterate until validation passes.

### Step 6: Fix layout before starting a run

`ralph-ng` expects:

- `ralph-plan.yaml` in the repo root
- spec markdown under `specs/*.md`

Only move to `ralph start` after both are correct.

## Field mapping

See [references/field-mapping.md](references/field-mapping.md) for the concise
mapping rules.

## Resources

- `scripts/markdown_to_ralph_plan.py`: deterministic draft generator for the
  common `### Task N:` markdown format
- `references/field-mapping.md`: quick reference for task-field mapping and
  validation constraints
