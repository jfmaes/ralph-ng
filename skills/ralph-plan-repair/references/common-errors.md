# Common Errors

Use this reference after running:

```bash
ralph validate-plan --plan ralph-plan.yaml --base-dir .
```

## Structural load errors

### `YAML anchors and aliases are not allowed`

Cause:
- the plan uses YAML anchors (`&name`) or aliases (`*name`)

Fix:
- expand the duplicated content explicitly
- remove all anchors and aliases from the file

### `Invalid YAML in ...`

Cause:
- malformed YAML syntax

Fix:
- repair indentation, list formatting, quoting, or block scalars
- rerun validation before changing task logic

### `Plan ... must deserialize to a mapping`

Cause:
- the YAML root is not an object with `tasks:`

Fix:
- make the root shape:

```yaml
tasks:
  - id: "TASK-001"
    ...
```

### `Invalid plan in ...`

Common causes:
- missing required task fields
- extra unsupported fields
- numeric or unquoted IDs that are not strings

Fix:
- every task must define:
  `id`, `title`, `description`, `depends_on`, `acceptance_criteria`,
  `verification_commands`, `expected_files`
- remove unknown fields instead of leaving extras in place
- quote IDs and dependency values as strings, for example `"TASK-001"`

## Semantic validation errors

### `Duplicate task ID: TASK-...`

Cause:
- two tasks share the same `id`

Fix:
- make task IDs unique
- preserve ordering and update any `depends_on` references that pointed at the
  duplicated ID

### `Task TASK-... depends on unknown task TASK-...`

Cause:
- a dependency points at a task ID that is missing or misspelled

Fix:
- correct the reference to the intended existing task
- or restore the missing upstream task if it exists in the source plan

### `Task dependency graph contains a cycle involving: ...`

Cause:
- two or more tasks depend on each other in a loop

Fix:
- return to the source plan and restore the intended one-way order
- if the tasks truly belong together, merge or split them so the DAG is acyclic

### `Task TASK-... must define at least one verification command`

Cause:
- `verification_commands` is empty

Fix:
- derive the narrowest real command from the task's source material
- prefer explicit commands already named in the markdown
- if the source only references a concrete test file, use
  `pytest path/to/test_file.py -x`
- if the source only says tests should pass and nothing narrower is justified,
  `pytest -q` may be acceptable

### `Task TASK-... references missing file ... that is not produced by a dependency`

Cause:
- an `expected_files` path does not exist in the repo
- and no transitive dependency task lists that path as an output

Fix:
- if the file already exists, correct the path
- if the task creates the file, make sure the file is listed on the task that
  actually produces it
- if the current task consumes the file, add or fix the dependency chain so an
  upstream task produces it

## Repair heuristics

- Structural errors first, semantic errors second.
- Prefer repairing the YAML from the planning markdown rather than hand-waving.
- If many tasks are wrong in the same way, regeneration with
  `skills/ralph-plan-converter/` is often faster and safer than ad hoc edits.
