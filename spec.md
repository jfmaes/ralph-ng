# ralph-ng v0.1 — Specification

## 1. Goal

Build ralph-ng as a local autonomous coding orchestrator that executes an already-approved implementation plan inside a git repository, shows operator-friendly live progress, persists enough state to resume after any interruption, and only stops on terminal completion or genuine human/system blockers.

The existing ralph loop (awesome-ai-workflow) proved that a bash script piping prompts into an AI CLI can produce committed, tested code autonomously. But unattended execution is not credible without durable state, explicit task contracts, adversarial cross-model review, deterministic stop/resume behavior, and safe output handling. ralph-ng exists to make autonomous coding loops reliable enough to run unattended overnight.

## 2. Explicit Non-Goals (v0.1)

- Generating product specs or implementation plans from scratch.
- Multiple concurrent writers in the same repository.
- Pipeline parallelism (builder on task N+1 while reviewer examines task N). Sequential execution only.
- Multi-user service hosting, web control plane, or cloud tenancy.
- Sandboxing untrusted code, prompts, or repositories. v0.1 assumes a trusted operator and trusted repo.
- Automatic deployment, release management, or CI repair after push.
- Language-specific semantic diff analysis.
- Cross-vendor model support (e.g., Codex executor + Claude evaluator). Same-vendor, different-model only in v0.1.
- The model election protocol from DESIGN.md. Model roles are configured, not negotiated.
- Mediated multi-model discussion rounds. Evaluator produces a verdict; there is no back-and-forth debate.
- Full TUI dashboard (Textual). v0.1 uses Rich for live console output.

## 3. Architecture

### 3.1 Runtime Shape

- **Packaging:** Python 3.12+ CLI application, installed via `pip install -e .` (development) or `pip install ralph-ng` (release). Console entry point: `ralph`.
- **Operating mode:** Local foreground CLI with commands: `validate-plan`, `start`, `resume`, `status`. Detach/reattach via terminal multiplexer (tmux/screen); native daemon mode is phase 2.
- **Workspace model:** One shared git workspace, one writer (the executor) at a time. The evaluator has no write access. Read-only helpers (code search, static analysis) may run in parallel in future phases.
- **Start gate:** Require a clean working tree. If the tree is dirty on `start`, refuse with an explanation. On `resume`, compare dirty state against the last recorded patch snapshot: if it matches, continue from review phase; if it diverges, transition to `blocked_human` with guidance.
- **State root:** `${git_dir}/ralph/` where `${git_dir}` is resolved from `git rev-parse --git-dir`. This is inherently excluded from git tracking when using standard `.git/` layout.

### 3.2 Source Artifacts

| Artifact | Location | Format | Mutability during run |
|----------|----------|--------|-----------------------|
| Project spec | `specs/` directory in repo | Markdown | Immutable |
| Implementation plan | `ralph-plan.yaml` in repo root | YAML (strict) | Immutable |
| Project config | `ralph.toml` in repo root | TOML | Immutable |
| Run state | `${git_dir}/ralph/runs/<run_id>/` | SQLite + JSONL | Append-only |
| Runtime overlays (ratchet checks) | `${git_dir}/ralph/runs/<run_id>/overlays/` | YAML | Append-only |

### 3.3 Task Contract Schema

```yaml
# ralph-plan.yaml
tasks:
  - id: "TASK-001"           # String, unique, never implicit-typed
    title: "Short task title"
    description: |
      Concrete coding objective with enough context
      for the executor to act without guessing.
    depends_on: []             # List of task ID strings
    acceptance_criteria:
      - "Observable behavior that must be true when done"
    verification_commands:
      - "pytest tests/unit/test_feature.py -x"
    expected_files:            # Files this task will create or modify
      - "src/app/feature.py"
      - "tests/unit/test_feature.py"
```

**YAML safety:** The plan loader MUST use `yaml.safe_load`. Task IDs are always parsed as strings (the schema enforces this via Pydantic `str` typing). The loader rejects YAML anchors and aliases.

### 3.4 Configuration (`ralph.toml`)

```toml
[ralph]
branch_prefix = "ralph/"           # Managed branch naming
auto_push = false                   # Push after each accepted task
max_retries_per_task = 3            # Retry budget
max_ratchet_checks_per_task = 10    # Cap on evaluator-added checks

[models]
executor = "sonnet"                 # Model for code implementation
evaluator = "opus"                  # Model for adversarial review
cli = "claude"                      # CLI tool to use

[timeouts]
executor_call_seconds = 600         # 10 min per executor invocation
verification_command_seconds = 120  # 2 min per verification command
evaluator_call_seconds = 300        # 5 min per evaluator invocation

[git]
protected_branches = ["main", "master"]
force_push = false                  # Always false in v0.1, not configurable
```

### 3.5 State Layout

```
${git_dir}/ralph/
  state.db                          # SQLite: runs, tasks, attempts, invocations
  events.jsonl                      # Append-only event stream
  runs/<run_id>/
    overlays/<task_id>.yaml         # Evaluator-added ratchet checks
    patches/<task_id>_attempt_<n>.patch  # Diff snapshot per attempt
    prompts/<task_id>_attempt_<n>.md     # Executor prompt as sent
    reviews/<task_id>_attempt_<n>.json   # Evaluator verdict
    command_logs/<task_id>_attempt_<n>/  # Verification stdout/stderr
```

### 3.6 Key Components

| Component | Responsibility |
|-----------|----------------|
| **CLI (`ralph` command)** | Parse commands, load `ralph.toml`, resolve repo/branch/plan/provider settings. |
| **Plan loader + validator** | Load `ralph-plan.yaml`, validate via Pydantic: reject duplicate IDs, DAG cycles, missing verification commands, non-string IDs, YAML anchors/aliases, and unreachable file references. |
| **State store** | SQLite + JSONL. Persist runs, task states, attempts, model invocations (with token counts), costs, blockers, patches, and runtime overlays. |
| **Scheduler + FSM** | Select next ready task (all deps satisfied), enforce state transitions. |
| **Executor adapter** | Invoke executor model CLI with constructed prompt. Parse streaming JSON output via strict typed schemas. Capture token counts, duration, exit status. |
| **Prompt builder** | Construct executor context: task contract, relevant spec sections, current file contents for expected_files, prior repair notes, effective verification commands (original + ratchet overlays). |
| **Verification runner** | Execute verification commands locally. Capture stdout, stderr, exit code. Enforce per-command timeout. |
| **Review bundle builder** | Assemble read-only review package: task contract, git diff, changed-file snippets, verification results, prior repair notes, ratchet overlay history. |
| **Evaluator adapter** | Invoke evaluator model with review bundle. Parse structured verdict: `accept` or `reject` with repair notes and new verification commands. Strict schema validation; malformed output triggers retry. |
| **Ratchet manager** | Validate new evaluator-added verification commands (syntactic validity, executable, no missing binaries). Store accepted overlays. Enforce per-task cap. |
| **Git manager** | Create/switch managed branch, snapshot patches, detect external drift, commit accepted tasks (one commit per task), push if enabled. Refuse protected branches and force-push. |
| **Progress renderer** | Rich-based live console: current task, state, attempt count, verification status, token/cost totals, blocker reason, next action. Driven from event stream. |

### 3.7 Data Flow

```
1. Operator: ralph validate-plan
   └─ Load ralph-plan.yaml → Pydantic validation → report pass/fail

2. Operator: ralph start --plan ralph-plan.yaml
   └─ Verify clean tree → load config → validate plan → create run record
   └─ Create managed branch (ralph/<plan>/<run_id>)
   └─ Enter main loop:

   ┌──────────────────────────────────────────────────────────┐
   │  MAIN LOOP                                               │
   │                                                          │
   │  3. Scheduler selects next ready task (deps satisfied)   │
   │     └─ No ready tasks + all done → EXIT: completed       │
   │     └─ No ready tasks + blocked  → EXIT: blocked_human   │
   │                                                          │
   │  4. Transition: ready → in_progress                      │
   │     └─ Detect workspace drift → blocked_human if dirty   │
   │                                                          │
   │  5. Prompt builder constructs executor context            │
   │     └─ Task contract + spec excerpts + file contents     │
   │     └─ + repair notes (if retry) + effective checks      │
   │     └─ Persist prompt artifact                           │
   │                                                          │
   │  6. Executor adapter invokes model CLI                   │
   │     └─ Parse streaming JSON, extract token counts        │
   │     └─ Timeout: executor_call_seconds                    │
   │     └─ On rate limit / provider error → blocked_system   │
   │     └─ Snapshot diff as patch artifact                   │
   │                                                          │
   │  7. Verification runner executes effective commands       │
   │     └─ Original + ratchet overlays                       │
   │     └─ Per-command timeout, capture all output            │
   │                                                          │
   │  8. Review bundle builder assembles read-only package    │
   │                                                          │
   │  9. Evaluator adapter invokes review model               │
   │     └─ Parse structured verdict (Pydantic)               │
   │     └─ Malformed output → retry evaluator (max 2)        │
   │                                                          │
   │  10. If ACCEPT + all verification passed:                │
   │      └─ Transition: review → done                        │
   │      └─ Git commit on managed branch                     │
   │      └─ Push if auto_push enabled                        │
   │      └─ Back to step 3                                   │
   │                                                          │
   │  11. If REJECT:                                          │
   │      └─ Validate new ratchet checks (executability)      │
   │      └─ Store valid overlays, discard invalid ones       │
   │      └─ Check retry budget                               │
   │      └─ Budget remaining: transition review → repair     │
   │        → back to step 5 with repair notes                │
   │      └─ Budget exhausted: → failed_exhausted             │
   │                                                          │
   │  12. On any unrecoverable error:                         │
   │      └─ Persist state + next-action guidance             │
   │      └─ EXIT with appropriate stop condition             │
   └──────────────────────────────────────────────────────────┘

3. Operator: ralph resume --run <run_id>
   └─ Load run state from SQLite
   └─ Compare workspace against last patch snapshot
   └─ If clean or matching: continue from last recorded state
   └─ If diverged: blocked_human with explanation

4. Operator: ralph status --run <run_id>
   └─ Read SQLite + tail events.jsonl
   └─ Display task states, attempt counts, costs, blockers
```

### 3.8 Task Lifecycle FSM

```
pending ──(deps satisfied)──→ ready
  ready ──(scheduler picks)──→ in_progress
  in_progress ──(executor done)──→ review
  review ──(accept + verified)──→ done
  review ──(reject, budget remaining)──→ repair
  repair ──(executor retry done)──→ review
  review ──(reject, budget exhausted)──→ failed_exhausted
  ANY ──(rate limit / provider down)──→ blocked_system
  ANY ──(human input needed)──→ blocked_human
```

Terminal states: `done`, `failed_exhausted`, `blocked_human`, `blocked_system`.
Resumable states: `blocked_system` (retry after backoff), `blocked_human` (after human resolves).

## 4. Key Design Decisions and Rationale

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Machine-readable `tasks.yaml`, not free-form markdown | Enables deterministic validation, automated retries, and reliable resume. Prose descriptions remain human-friendly within the structured format. |
| 2 | Sequential execution, one writer | Shared-directory multi-writer concurrency produces nondeterministic diffs and git contention. Sequential execution is reliable and debuggable. |
| 3 | Cross-model review: fast executor, thorough evaluator | Sonnet builds (fast, cheap, many iterations). Opus reviews (thorough, catches subtle issues). Different model catches different blind spots than same-model review. |
| 4 | Evaluator is read-only by construction | Evaluator reviews a compiled bundle, not the live repo. Eliminates evaluator-as-second-implementer, preserves audit trail, simplifies governance. Write access is a phase 2 optimization. |
| 5 | State under `${git_dir}/ralph/` | Run state is coupled to the target repo. Resume works from any directory. State is inherently gitignored. |
| 6 | Source plan is immutable during a run | Evaluator-added checks are runtime overlays in state, not mutations to the operator's plan. Avoids plan drift and preserves the operator's intent as ground truth. |
| 7 | Verification commands required on every task | No task is `done` without machine-checkable evidence. Prevents subjective-only completion and enables automated gating. |
| 8 | One commit per accepted task on a managed branch | Attributable history. Easy revert of individual tasks. Clean audit trail. No bulk commits mixing unrelated changes. |
| 9 | Workspace drift detection before each task | If external changes appear (human edits, other tools), Ralph blocks instead of producing nondeterministic results. |
| 10 | Typed schema parsing for all model output | Eliminates the `source .ralph_review_score` vulnerability class from the current system. Malformed output is retried, never executed. |
| 11 | Review ratchet with sanity-checked overlays | Evaluator rejections must add machine-checkable conditions, preventing trivial-implementation loops. New checks are validated for executability before enforcement. |
| 12 | Explicit timeouts on all external calls | Prevents indefinite hangs from model calls or verification commands. Configurable in `ralph.toml`. |

## 5. Acceptance Criteria

1. `ralph validate-plan` rejects: duplicate task IDs, DAG cycles, tasks with no verification commands, non-string task IDs, YAML anchors/aliases, and file references that don't exist and aren't in expected_files of a dependency.
2. `ralph start` creates a run record, managed branch, SQLite state, and JSONL event stream under `${git_dir}/ralph/`.
3. Tasks execute sequentially. A task never starts unless all `depends_on` tasks are `done`.
4. Every executor attempt persists: the prompt as sent, command logs, model invocation metadata (model, tokens in/out, duration, exit status), and a patch snapshot — all before review begins.
5. No task reaches `done` unless every effective verification command (original + overlays) exits 0 AND the evaluator returns `accept`.
6. Evaluator rejections include targeted repair notes and at least one new machine-checkable condition. Ralph validates the condition's executability before storing it as an overlay. Per-task overlay cap: 10.
7. Retry budget defaults to 3 per task. Exhaustion transitions to `failed_exhausted` with human-readable next steps persisted.
8. Rate limits, provider outages, or credential failures transition to `blocked_system`. State is fully persisted and `resume` can continue without data loss.
9. Unexpected workspace drift transitions to `blocked_human` with explanation.
10. `ralph resume` compares dirty state against last patch snapshot. Matching state continues; diverged state blocks.
11. Accepted tasks produce one commit each on the managed branch. Push occurs only if `auto_push = true` and the branch is not in `protected_branches`.
12. `ralph status` and live progress show: current task, state, attempt count, verification results, token counts, estimated cost, blocker reason (if any), and suggested next action.
13. All model output is parsed via Pydantic schemas. Malformed output triggers a retry with error context (max 2 retries). No model-written content is ever executed as code.
14. When all tasks are `done`, Ralph exits with a final summary and suggests next steps.

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Plans are underspecified or wrong | Executor thrashes on vague tasks | Plan validation gate rejects tasks without verification commands and acceptance criteria. |
| YAML type coercion corrupts task data | Silent data corruption (IDs, booleans) | Pydantic strict typing, safe_load, anchor/alias rejection, string-only IDs. |
| Evaluator becomes second implementer | Lost auditability, nondeterministic diffs | Read-only evaluator by construction: reviews a bundle, never touches the repo. |
| Infinite repair loops | Token waste, no progress | Retry budget (3), ratchet checks prevent same trivial fix from passing twice, failed_exhausted terminal state. |
| Bad evaluator-added verification commands | Valid implementations blocked by bogus checks | Executability sanity check before accepting overlays. Per-task cap (10). |
| CLI output format changes break parsing | Orchestrator loses control of agent state | Strict Pydantic schemas, malformed-output retries, adapter contract tests, version checks. |
| Workspace modified by external process | Nondeterministic state, bad commits | Drift detection before each task and before commit. Block on unexpected changes. |
| Rate limits or crashes lose progress | Must restart from scratch | Every state transition persisted to SQLite + JSONL. Resume reconciles state with git. |
| Auto-push sends bad code upstream | Broken remote branch | Managed branches only, protected branch list, no force-push, one commit per accepted task. |
| Executor prompt too large or too small | Poor implementation quality | Prompt builder with explicit context assembly: task + spec + files + repairs. Token budget awareness in phase 2. |
| Verification command hangs indefinitely | Loop stalls with no recovery | Per-command timeout (configurable, default 120s). Timeout = verification failure. |
| Dirty tree on resume after crash | Silent data corruption or lost work | Compare dirty state against last patch snapshot. Match → continue. Diverge → block. |