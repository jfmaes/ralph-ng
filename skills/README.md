# Skills

This directory holds model-agnostic skill bundles for working with `ralph-ng`.
They are plain Markdown-plus-scripts resources, so you can point Codex, Claude,
or any other agent at a skill folder and ask it to follow the workflow.

## How to use

Tell the agent to read the relevant `SKILL.md` and follow it. Example prompts:

- "Read `skills/ralph-plan-converter/SKILL.md` and generate `ralph-plan.yaml` from `implementation_plan.md` using `python3`."
- "Read `skills/init-ralph/SKILL.md` and prepare this repo to run under `ralph-ng`."
- "Read `skills/ralph-run-triage/SKILL.md` and diagnose this blocked run."
- "Read `skills/ralph-plan-repair/SKILL.md` and fix this invalid `ralph-plan.yaml` until `ralph validate-plan` passes."

## Available skills

- `ralph-plan-converter`: Convert markdown specs and implementation plans into
  a validated `ralph-plan.yaml` plus `specs/` layout.
- `ralph-plan-repair`: Repair an invalid `ralph-plan.yaml` by working from
  validator output back to the source plan and repo layout.
- `init-ralph`: Prepare a repository for `ralph-ng` by checking config, plan,
  spec layout, validation, and preflight readiness.
- `ralph-run-triage`: Diagnose interrupted, blocked, or stale `ralph-ng` runs
  and choose the safest recovery path.
