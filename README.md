# ralph-ng

`ralph-ng` is a local autonomous coding orchestrator that runs against an already-approved implementation plan, persists enough state to resume after interruption, and keeps the operator informed about what the system is doing.

The current implementation covers the full v0.1 workflow:

- Python package and CLI entrypoint
- `ralph.toml` loading and validation
- safe `ralph-plan.yaml` parsing and semantic validation
- SQLite run/task/attempt/invocation state
- append-only JSONL event logging
- task lifecycle FSM and dependency-aware scheduling
- executor prompt construction from task/spec/file context
- Claude executor/evaluator adapter foundations with stream-json parsing
- local verification command execution with timeouts
- review bundle assembly for evaluator input
- ratchet overlay persistence and validation
- git branch/patch/commit/push safety helpers
- resume reconciliation against persisted patch snapshots
- sequential orchestration across executor, verification, evaluator, and git commit stages
- working `validate-plan`, `start`, `resume`, and `status` commands
- Rich-based progress snapshots, with live updates during `start`/`resume` in interactive terminals
- end-to-end coverage with a real temporary git repo and mocked model adapters

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ralph --help
```

Validate a plan:

```bash
.venv/bin/ralph validate-plan --plan docs/examples/ralph-plan.example.yaml --base-dir docs/examples
```

Start a run inside a git repository that contains `ralph-plan.yaml` and `ralph.toml`:

```bash
.venv/bin/ralph start
.venv/bin/ralph status --run-id <run-id>
.venv/bin/ralph resume --run-id <run-id>
```

Runtime state is written under `.git/ralph/`.

## Trust Boundary

Operator-authored verification commands are intentionally shell-executed. That is part of the product contract for v0.1, and the repo/plan are treated as trusted input. `ralph-ng` does not sandbox arbitrary plan commands in v0.1.

Evaluator-added ratchet commands are a narrower trust boundary. They are still shell commands, but they now pass through lightweight dangerous-pattern checks plus executability validation before they are stored as overlays.

## Documentation

- Example plan: `docs/examples/ralph-plan.example.yaml`
- Example config: `docs/examples/ralph.toml.example`
- Example spec: `docs/examples/specs/example-spec.md`
- Example evaluator verdicts: `docs/examples/evaluator-verdict.accept.json`, `docs/examples/evaluator-verdict.reject.json`
- Example ratchet overlay: `docs/examples/ratchet-overlay.example.yaml`
- E2E fixture repo: `tests/e2e/fixtures/`
- Progress tracker: `implementation_plan.md`

The example files are kept in sync with the current validators by automated tests.

## Current Repo Layout

```text
src/ralph_ng/           Application code
tests/                  Unit tests and fixtures
docs/examples/          Example plan, config, spec, and sample project files
spec.md                 Product specification
implementation_plan.md  Build plan plus completion tracker
DESIGN.md               Background design rationale
```

## Notes On Plans

Plans are strict YAML and are loaded with `yaml.safe_load`. Task IDs must be strings, YAML anchors and aliases are rejected, and the validator enforces duplicate/dependency/cycle checks before execution is allowed.

Use the example plan as the template for new plans. It is intentionally small and includes the fields the loader and validator currently expect.
