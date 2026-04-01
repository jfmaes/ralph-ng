# Field Mapping

Quick reference for converting markdown into `ralph-plan.yaml`.

## Required output fields

Every task must have all seven fields:

| Field | Type | Constraints |
|---|---|---|
| `id` | string | `TASK-NNN` format, zero-padded to 3 digits |
| `title` | string | Short, descriptive |
| `description` | string | Full task body; use YAML `|` block style |
| `depends_on` | list[string] | Task IDs or `[]` |
| `acceptance_criteria` | list[string] | At least one entry |
| `verification_commands` | list[string] | At least one entry |
| `expected_files` | list[string] | At least one entry, relative paths |

## Common markdown source mapping

### Format A: bold metadata

- `### Task N: Title` -> `id`, `title`
- `- **Depends on:** ...` -> `depends_on`
- `- **Done when:** ...` -> `acceptance_criteria`
- `- **Test first:** ...` -> primary source for `verification_commands`
- `- **Create:**` / `- **Modify:**` lists -> `expected_files`

### Format B: plain metadata

- `### Task N: Title` -> `id`, `title`
- `- Create: \`path\`` -> `expected_files`
- `- Modify: \`path\`` -> `expected_files`
- `- Done when:` nested bullets -> `acceptance_criteria`
- `- Test first:` command or test-file refs -> `verification_commands`

### Format C: slice-grouped plans

- `## Slice N: Name` groups tasks
- The first task in Slice N+1 implicitly depends on the last task in Slice N
  unless the markdown already states otherwise

## Verification command rules

1. Prefer explicit commands already present in the markdown.
2. If the markdown names a concrete test file like `tests/test_x.py`, turn it
   into `pytest tests/test_x.py -x`.
3. If the task creates a concrete test file, use that test file as the command.
4. If the source only says "tests pass" or "pytest passes", `pytest -q` is a
   reasonable fallback.
5. If you still cannot justify a command, stop and ask for better source
   material instead of inventing one.

## Spec layout rules

`ralph-ng` expects:

- `ralph-plan.yaml` in the repo root
- spec markdown under `specs/*.md`

If the repo only has a root `spec.md`, copy it into `specs/` before starting a
run.

## Validation rules

These are enforced by `ralph validate-plan`:

- no extra fields
- no duplicate task IDs
- all `depends_on` values must reference existing task IDs
- the dependency graph must be acyclic
- `verification_commands` must be non-empty
- `expected_files` must be non-empty
- expected files must already exist or be produced by a transitive dependency
- YAML anchors and aliases are rejected
