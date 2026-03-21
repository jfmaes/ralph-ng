# ralph-ng

`ralph-ng` is a local autonomous coding orchestrator that runs against an already-approved implementation plan, persists enough state to resume after interruption, and keeps the operator informed about what the system is doing.

The current implementation covers the v0.1 workflow and v0.2 mixed-provider adversarial review:

**v0.1 — Single-provider autonomous loop:**
- Python package and CLI entrypoint
- `ralph.toml` loading and validation
- safe `ralph-plan.yaml` parsing and semantic validation
- SQLite run/task/attempt/invocation state
- append-only JSONL event logging
- task lifecycle FSM and dependency-aware scheduling
- executor prompt construction from task/spec/file context
- Claude executor adapter with stream-json parsing
- local verification command execution with timeouts
- review bundle assembly for reviewer input
- ratchet overlay persistence and validation
- git branch/patch/commit/push safety helpers
- resume reconciliation against persisted patch snapshots
- sequential orchestration across executor, verification, reviewer, and git commit stages
- working `validate-plan`, `start`, `resume`, and `status` commands
- Rich-based progress snapshots, with live updates during `start`/`resume` in interactive terminals
- end-to-end coverage with a real temporary git repo and mocked model adapters

**v0.2 — Mixed-provider adversarial review (Claude executor + Codex reviewer):**
- Transport ABC: provider-neutral, synchronous `Transport` interface for CLI subprocess invocations (API transport in v0.3)
- CLI transports for both Claude (`claude -p --output-format stream-json --verbose`) and Codex (`codex exec --json`)
- Role-based config: `[roles.executor]` and `[roles.reviewer]` sections with per-role provider/model/CLI settings
- `ReviewFindings` schema: severity-graded, evidence-backed findings with stable IDs for cross-round tracking
- Diff risk classification: size-based tiers, file count multipliers, path-based security/high-risk overrides
- Independent first-pass review: reviewer never sees executor reasoning; anti-sycophancy protocol
- Bounded adversarial challenge rounds: repair-first-then-explain, configurable thresholds
- Human escalation policy: rejection loop exhaustion, low-confidence, critical risk, persistent security findings
- Provider event persistence: JSONL per run/task/attempt for replay and observability
- Provider activity panes in Rich progress renderer with review round indicators
- Adapter factory: `build_executor()`, `build_reviewer()` from config; adding a new transport requires zero adapter changes
- `[review]` config section: thresholds, challenge rounds, escalation, risk path overrides
- Backward compatible: existing `[models]`-only configs still work for executor selection

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
- Example config (minimal): `docs/examples/ralph.toml.example`
- Example config (mixed-provider): `docs/examples/ralph.toml.mixed-provider.example`
- Example spec: `docs/examples/specs/example-spec.md`
- Example ratchet overlay: `docs/examples/ratchet-overlay.example.yaml`
- v0.2 spec: `docs/plans/2026-03-20-claude-executor-codex-reviewer-spec.md`
- v0.2 implementation plan: `docs/plans/2026-03-20-claude-executor-codex-reviewer-implementation-plan.md`
- E2E fixture repo: `tests/e2e/fixtures/`

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
