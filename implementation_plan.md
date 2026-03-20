# ralph-ng v0.1 — Implementation Plan

## Overview

20 tasks across 5 sprints. TDD throughout: every task starts with tests. Tasks are ordered so each builds on proven foundations. The critical path is: scaffolding → config/plan → state → executor → verification → evaluator → ratchet → git → CLI → progress UI.

**Complexity key:** S = < half day, M = half day to full day, L = 1–2 days.

## Progress Tracker

Status as of 2026-03-20:

- [x] Task 1: Project scaffolding
- [x] Task 2: Configuration loading
- [x] Task 3: Plan loading and schema validation
- [x] Task 4: Plan DAG validation
- [x] Task 5: SQLite state store
- [x] Task 6: Event logger (JSONL)
- [x] Task 7: Task lifecycle FSM
- [x] Task 8: Scheduler
- [x] Task 9: Prompt builder
- [x] Task 10: Executor adapter (Claude CLI)
- [x] Task 11: Verification runner
- [x] Task 12: Review bundle builder
- [x] Task 13: Evaluator adapter (Claude CLI)
- [x] Task 14: Ratchet manager
- [x] Task 15: Git manager
- [x] Task 16: Resume logic
- [x] Task 17: Main orchestration loop
- [x] Task 18: CLI commands (start, resume, status, validate-plan)
- [x] Task 19: Progress renderer (Rich)
- [x] Task 20: End-to-end integration test and documentation

---

## Sprint 1: Foundation (Tasks 1–4)

*Goal: Project skeleton, config loading, plan parsing, plan validation. Everything needed before runtime exists.*

### Task 1: Project scaffolding
- **Complexity:** S
- **Depends on:** nothing
- **Test first:** `tests/test_cli_smoke.py` — verify `ralph --help` exits 0 and shows expected commands.
- **Create:**
  - `pyproject.toml` — project metadata, dependencies (typer, rich, pydantic, pyyaml, pytest, pytest-asyncio), console script entry point `ralph = "ralph_ng.cli:app"`
  - `src/ralph_ng/__init__.py`
  - `src/ralph_ng/cli.py` — Typer app with stub commands: `validate-plan`, `start`, `resume`, `status`
  - `tests/__init__.py`
  - `tests/conftest.py`
  - `tests/test_cli_smoke.py`
- **Done when:** `pip install -e .` succeeds, `ralph --help` shows all four commands, `pytest` passes.

### Task 2: Configuration loading
- **Complexity:** S
- **Depends on:** Task 1
- **Test first:** `tests/test_config.py` — test loading valid TOML, test defaults when fields omitted, test rejection of invalid values (negative timeouts, empty model names).
- **Create:**
  - `src/ralph_ng/config.py` — Pydantic models: `RalphConfig`, `ModelsConfig`, `TimeoutsConfig`, `GitConfig`. Loader from `ralph.toml` with defaults.
  - `tests/fixtures/valid_ralph.toml`
  - `tests/fixtures/minimal_ralph.toml`
  - `tests/fixtures/invalid_ralph.toml`
  - `tests/test_config.py`
- **Done when:** Config loads from TOML, defaults apply, invalid configs raise clear errors, all tests pass.

### Task 3: Plan loading and schema validation
- **Complexity:** M
- **Depends on:** Task 2
- **Test first:** `tests/test_plan_loader.py` — test valid plan loads correctly, test non-string IDs are rejected, test YAML anchors/aliases are rejected, test missing required fields are rejected, test `yaml.safe_load` is used (no arbitrary object instantiation).
- **Create:**
  - `src/ralph_ng/plan.py` — Pydantic models: `Task`, `Plan`. Loader using `yaml.safe_load` with strict string ID enforcement. Anchor/alias detection via pre-parse scan.
  - `tests/fixtures/valid_plan.yaml`
  - `tests/fixtures/plan_with_numeric_ids.yaml`
  - `tests/fixtures/plan_with_anchors.yaml`
  - `tests/fixtures/plan_missing_fields.yaml`
  - `tests/test_plan_loader.py`
- **Done when:** Valid plans load into typed Pydantic models. Invalid plans produce clear rejection messages. YAML safety enforced. All tests pass.

### Task 4: Plan DAG validation
- **Complexity:** M
- **Depends on:** Task 3
- **Test first:** `tests/test_plan_validation.py` — test cycle detection, test duplicate IDs, test missing dependency references, test tasks without verification commands, test file reference validation (expected_files exist or are produced by dependency chain).
- **Create:**
  - `src/ralph_ng/validation.py` — DAG cycle detection (Kahn's algorithm), duplicate ID check, dangling dependency check, verification command presence check, file reference plausibility check.
  - `tests/fixtures/plan_with_cycle.yaml`
  - `tests/fixtures/plan_with_duplicate_ids.yaml`
  - `tests/fixtures/plan_with_dangling_deps.yaml`
  - `tests/fixtures/plan_no_verification.yaml`
  - `tests/test_plan_validation.py`
- **Done when:** `ralph validate-plan` correctly rejects all invalid plan variants and accepts valid plans. All tests pass.

---

## Sprint 2: State and Scheduling (Tasks 5–8)

*Goal: Durable state, event logging, task FSM, scheduler. Everything needed to track run progress and survive interruptions.*

### Task 5: SQLite state store
- **Complexity:** L
- **Depends on:** Task 3
- **Test first:** `tests/test_state_store.py` — test run creation, test task state persistence, test attempt recording, test invocation metadata (model, tokens, duration), test state survives process restart (close and reopen DB).
- **Create:**
  - `src/ralph_ng/state.py` — `StateStore` class. Tables: `runs`, `task_states`, `attempts`, `invocations`. Methods: `create_run`, `get_task_state`, `transition_task`, `record_attempt`, `record_invocation`, `get_run_summary`.
  - `tests/test_state_store.py`
- **Done when:** State persists across process restarts. All CRUD operations work. Token counts and durations recorded per invocation. All tests pass.

### Task 6: Event logger (JSONL)
- **Complexity:** S
- **Depends on:** Task 5
- **Test first:** `tests/test_event_logger.py` — test events are appended (not overwritten), test event schema (timestamp, event_type, task_id, data), test file can be tailed while being written.
- **Create:**
  - `src/ralph_ng/events.py` — `EventLogger` class. Append-only JSONL writer. Event types: `run.started`, `task.transition`, `executor.started`, `executor.completed`, `verification.result`, `evaluator.verdict`, `ratchet.added`, `run.stopped`.
  - `tests/test_event_logger.py`
- **Done when:** Events append to JSONL. Schema is consistent. File is readable while being written. All tests pass.

### Task 7: Task lifecycle FSM
- **Complexity:** M
- **Depends on:** Task 5
- **Test first:** `tests/test_fsm.py` — test all valid transitions succeed, test all invalid transitions raise, test terminal states cannot transition, test `blocked_system` and `blocked_human` can resume.
- **Create:**
  - `src/ralph_ng/fsm.py` — `TaskFSM` class. States: `pending`, `ready`, `in_progress`, `review`, `repair`, `done`, `blocked_human`, `blocked_system`, `failed_exhausted`. Transition validation. Integrates with `StateStore` for persistence.
  - `tests/test_fsm.py`
- **Done when:** All valid transitions work. Invalid transitions raise clear errors. State persists through `StateStore`. All tests pass.

### Task 8: Scheduler
- **Complexity:** M
- **Depends on:** Task 4, Task 7
- **Test first:** `tests/test_scheduler.py` — test selects task with all deps done, test skips tasks with unmet deps, test returns None when no tasks are ready, test detects all-done (plan complete), test detects deadlock (no ready tasks but not all done).
- **Create:**
  - `src/ralph_ng/scheduler.py` — `Scheduler` class. Methods: `next_ready_task()`, `is_plan_complete()`, `is_deadlocked()`. Consults task states and dependency graph.
  - `tests/test_scheduler.py`
- **Done when:** Scheduler correctly identifies next task respecting dependencies. Detects completion and deadlock. All tests pass.

---

## Sprint 3: Execution Pipeline (Tasks 9–14)

*Goal: Executor, verification, evaluator, ratchet, git operations. The core loop that does actual work.*

### Task 9: Prompt builder
- **Complexity:** M
- **Depends on:** Task 3
- **Test first:** `tests/test_prompt_builder.py` — test prompt includes task contract, test prompt includes spec excerpts, test prompt includes file contents for expected_files, test prompt includes repair notes on retry, test prompt includes effective verification commands (original + overlays).
- **Create:**
  - `src/ralph_ng/prompt_builder.py` — `PromptBuilder` class. Assembles executor context from task, specs, file contents, repair notes, and ratchet overlays.
  - `tests/fixtures/sample_spec.md`
  - `tests/test_prompt_builder.py`
- **Done when:** Prompts contain all required context. Repair notes included on retries. Ratchet overlays included in verification section. All tests pass.

### Task 10: Executor adapter (Claude CLI)
- **Complexity:** L
- **Depends on:** Task 2, Task 9
- **Test first:** `tests/test_executor_adapter.py` — test parses streaming JSON output correctly, test extracts token counts from stream, test handles timeout (returns structured error), test handles rate limit response, test handles malformed output (retry with error context).
- **Create:**
  - `src/ralph_ng/adapters/__init__.py`
  - `src/ralph_ng/adapters/base.py` — `ExecutorAdapter` abstract base class with typed result model.
  - `src/ralph_ng/adapters/claude_executor.py` — Claude CLI adapter. Invokes `claude -p --model <model> --output-format stream-json`. Parses stream. Extracts tokens.
  - `tests/test_executor_adapter.py` (with mocked subprocess for unit tests)
- **Done when:** Adapter invokes Claude CLI, parses streaming output, extracts token metadata, handles errors gracefully. All tests pass.

### Task 11: Verification runner
- **Complexity:** M
- **Depends on:** Task 6
- **Test first:** `tests/test_verification_runner.py` — test passing command returns success, test failing command returns failure with output, test timeout kills process and returns timeout error, test multiple commands run sequentially and all results collected.
- **Create:**
  - `src/ralph_ng/verification.py` — `VerificationRunner` class. Runs shell commands with configurable timeout. Captures stdout, stderr, exit code, duration. Returns structured `VerificationResult` per command.
  - `tests/test_verification_runner.py`
- **Done when:** Commands run with timeout enforcement. All output captured. Results are structured and typed. All tests pass.

### Task 12: Review bundle builder
- **Complexity:** S
- **Depends on:** Task 11
- **Test first:** `tests/test_review_bundle.py` — test bundle includes task contract, test bundle includes git diff, test bundle includes verification results, test bundle includes repair notes if retry, test bundle includes ratchet overlay history.
- **Create:**
  - `src/ralph_ng/review_bundle.py` — `ReviewBundleBuilder` class. Assembles read-only package for evaluator from task, diff, verification results, and history.
  - `tests/test_review_bundle.py`
- **Done when:** Bundle contains all required review context. No live repo access passed to evaluator. All tests pass.

### Task 13: Evaluator adapter (Claude CLI)
- **Complexity:** L
- **Depends on:** Task 12
- **Test first:** `tests/test_evaluator_adapter.py` — test parses `accept` verdict correctly, test parses `reject` with repair notes and new checks, test rejects malformed output and retries, test enforces verdict schema (must have repair_notes and new_checks on reject), test extracts token counts.
- **Create:**
  - `src/ralph_ng/adapters/claude_evaluator.py` — Claude CLI adapter for evaluation. Sends review bundle. Parses structured JSON verdict via Pydantic: `EvaluatorVerdict` with `decision` (accept/reject), `repair_notes` (str), `new_verification_commands` (list[str]), `summary` (str).
  - `tests/test_evaluator_adapter.py` (with mocked subprocess)
- **Done when:** Evaluator produces typed verdicts. Malformed output triggers retry (max 2). Token metadata extracted. All tests pass.

### Task 14: Ratchet manager
- **Complexity:** M
- **Depends on:** Task 11, Task 13
- **Test first:** `tests/test_ratchet.py` — test valid new check is accepted and stored as overlay, test check with missing binary is rejected, test check with bad syntax is rejected, test per-task cap is enforced (11th check rejected), test overlays persist across process restart, test effective verification set = original + overlays.
- **Create:**
  - `src/ralph_ng/ratchet.py` — `RatchetManager` class. Validates new checks (subprocess executability test — runs command, checks it doesn't fail on missing binary/syntax error; a non-zero exit on correct logic is expected and allowed). Stores as YAML overlays. Enforces cap. Computes effective verification set.
  - `tests/test_ratchet.py`
- **Done when:** Valid checks stored, invalid rejected, cap enforced, overlays persist, effective set computed correctly. All tests pass.

---

## Sprint 4: Git and Core Loop (Tasks 15–18)

*Goal: Git operations, workspace safety, the main orchestration loop, and CLI commands.*

### Task 15: Git manager
- **Complexity:** L
- **Depends on:** Task 5
- **Test first:** `tests/test_git_manager.py` — test creates managed branch with correct prefix, test detects dirty working tree, test snapshots patch after executor run, test commits with structured message, test refuses protected branches, test refuses force-push, test detects workspace drift (unexpected changes between tasks), test push to remote (mocked).
- **Create:**
  - `src/ralph_ng/git_manager.py` — `GitManager` class. Methods: `ensure_managed_branch()`, `is_clean()`, `snapshot_patch()`, `detect_drift()`, `commit_task()`, `push_if_enabled()`. Uses `subprocess` for git commands (no gitpython dependency).
  - `tests/test_git_manager.py` (uses temporary git repos)
- **Done when:** Branch management, drift detection, patch snapshots, commits, and push all work correctly. Protected branch and force-push guards enforced. All tests pass.

### Task 16: Resume logic
- **Complexity:** M
- **Depends on:** Task 5, Task 15
- **Test first:** `tests/test_resume.py` — test resume from `blocked_system` continues from correct task, test resume with clean matching state continues, test resume with diverged dirty state blocks, test resume with clean state after manual fix continues, test resume after `failed_exhausted` skips that task and continues.
- **Create:**
  - `src/ralph_ng/resume.py` — `ResumeManager` class. Loads run state. Compares current workspace against last patch snapshot. Determines safe continuation point. Transitions resumable blocked states back to actionable states.
  - `tests/test_resume.py`
- **Done when:** Resume correctly handles all blocked/interrupted states. Dirty-state comparison works. All tests pass.

### Task 17: Main orchestration loop
- **Complexity:** L
- **Depends on:** Tasks 5–16 (all prior components)
- **Test first:** `tests/test_orchestrator.py` — test happy path: 3-task plan executes to completion with mocked executor/evaluator, test retry: evaluator rejects once then accepts, test failed_exhausted after 3 retries, test blocked_system on rate limit pauses cleanly, test blocked_human on drift pauses cleanly, test all events logged, test all state transitions persisted.
- **Create:**
  - `src/ralph_ng/orchestrator.py` — `Orchestrator` class. The main loop as described in the data flow section. Wires together scheduler, prompt builder, executor, verification, review bundle, evaluator, ratchet, git manager, state store, and event logger.
  - `tests/test_orchestrator.py` (integration test with mocked adapters)
- **Done when:** Full loop executes plans to completion. All error paths handled. State persisted at every transition. Events logged. All tests pass.

### Task 18: CLI commands (start, resume, status, validate-plan)
- **Complexity:** M
- **Depends on:** Task 17
- **Test first:** `tests/test_cli_commands.py` — test `validate-plan` with valid and invalid plans, test `start` creates run and begins execution, test `resume` loads existing run, test `status` displays run info, test `start` refuses dirty tree, test `start` refuses if plan validation fails.
- **Modify:**
  - `src/ralph_ng/cli.py` — Replace stubs with real implementations. Wire commands to orchestrator, resume manager, state store.
- **Done when:** All four CLI commands work end-to-end. Error messages are clear. All tests pass.

---

## Sprint 5: Progress UI and Polish (Tasks 19–20)

*Goal: Live progress display, end-to-end integration testing, documentation.*

### Task 19: Progress renderer (Rich)
- **Complexity:** M
- **Depends on:** Task 6, Task 18
- **Test first:** `tests/test_progress.py` — test renders current task and state, test renders attempt count, test renders verification status per command, test renders token counts and cost estimate, test renders blocker reason when blocked, test renders final summary on completion.
- **Create:**
  - `src/ralph_ng/progress.py` — `ProgressRenderer` class. Rich Live display driven from event stream. Shows: plan name, run ID, current task (title + state + attempt), verification command results, cumulative tokens (in/out per model), estimated cost, blocker reason, next action suggestion.
  - `tests/test_progress.py`
- **Done when:** Live display updates as events arrive. All information panels populated. Clean exit on completion. All tests pass.

### Task 20: End-to-end integration test and documentation
- **Complexity:** L
- **Depends on:** Task 19
- **Test first:** `tests/e2e/test_full_run.py` — test full lifecycle: validate-plan → start → execute 3 tasks (with one retry) → complete. Uses a real temporary git repo with a trivial Python project and real file edits (mocked LLM responses but real git, real filesystem, real verification commands).
- **Create:**
  - `tests/e2e/__init__.py`
  - `tests/e2e/test_full_run.py`
  - `tests/e2e/fixtures/` — sample plan, spec, and project files for e2e test
  - `README.md` — installation, quickstart, configuration reference, plan format reference
- **Modify:**
  - Any bugs or gaps discovered during e2e testing
- **Done when:** E2E test passes. README covers installation and basic usage. `ralph validate-plan`, `start`, `resume`, and `status` work against a real repo with mocked LLM responses. All tests pass.

---

## Dependency Graph

```
Task 1 (scaffolding)
  └─→ Task 2 (config)
       └─→ Task 3 (plan loader)
            ├─→ Task 4 (DAG validation)
            │    └─→ Task 8 (scheduler) ──→ Task 17 (orchestrator)
            └─→ Task 9 (prompt builder) ──→ Task 17
       └─→ Task 10 (executor adapter) ──→ Task 17
  └─→ Task 5 (state store)
       ├─→ Task 6 (event logger) ──→ Task 11 (verification) ──→ Task 14 (ratchet) ──→ Task 17
       ├─→ Task 7 (FSM) ──→ Task 8
       ├─→ Task 15 (git manager) ──→ Task 16 (resume) ──→ Task 17
       └─→ Task 16
  └─→ Task 12 (review bundle) ──→ Task 13 (evaluator adapter) ──→ Task 14 ──→ Task 17

Task 17 (orchestrator) ──→ Task 18 (CLI) ──→ Task 19 (progress) ──→ Task 20 (e2e + docs)
```

## Sprint Summary

| Sprint | Tasks | Focus | Estimated effort |
|--------|-------|-------|------------------|
| 1 | 1–4 | Foundation: scaffolding, config, plan loading, validation | 3–4 days |
| 2 | 5–8 | State: SQLite store, events, FSM, scheduler | 4–5 days |
| 3 | 9–14 | Pipeline: prompt builder, executor, verification, evaluator, ratchet | 6–8 days |
| 4 | 15–18 | Integration: git, resume, orchestrator loop, CLI commands | 5–7 days |
| 5 | 19–20 | Polish: progress UI, e2e tests, documentation | 3–4 days |
| **Total** | **20** | | **21–28 days** |
