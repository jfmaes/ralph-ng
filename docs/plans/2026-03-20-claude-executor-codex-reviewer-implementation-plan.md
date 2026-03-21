# Claude Executor + Codex Reviewer — Implementation Plan

Status: **Implemented** (all 18 tasks complete, unit/e2e tests passing with fake transports; real CLI smoke tests require `claude` and `codex` on PATH)

## Overview

This plan implements the first mixed-provider milestone for `ralph-ng`:

- Claude executor
- Codex reviewer with two-stage evaluation (spec compliance + code quality)
- provider-level live progress
- bounded adversarial review with evidence-backed thresholds

The implementation reuses Crossfire's adapter/transport patterns and Superpowers' review-prompting patterns, but does not port either runtime.

## Delivery Strategy

Build this in four slices:

1. Provider event layer, fake transports, and adapter groundwork
2. Codex review integration with ReviewFindings schema
3. Adversarial review orchestration with threshold-driven challenge policy
4. Rich progress and validation

Each slice should land with tests and should leave the existing Claude-only path working.

---

## Slice 1: Transport Abstraction, Provider Events, Test Infrastructure

Goal: introduce the transport ABC, provider-neutral event stream, and fake transports. This slice establishes the adapter swappability boundary that v0.3 (API transports) will build on.

### Task 1: Transport ABC and TransportResult

- Create: `src/ralph_ng/adapters/transport.py`
- Create: `tests/test_transport_protocol.py`
- Implement:
  - `Transport` ABC with synchronous `invoke()` and `health_check()` methods
  - `TransportResult` model: status, output_text, input_tokens, output_tokens, duration_seconds, error_message, raw_events
  - `TransportResult` is provider-neutral — no exit codes, no stderr (these are CLI transport internals)
  - protocol tests that verify any transport implementation satisfies the contract
- Design constraint:
  - fully synchronous — ralph is turn-based, even v0.3 API transports will be blocking calls with timeout
  - the interface must accommodate both CLI subprocess and HTTP API transports without changes
  - `json_schema` parameter on `invoke()` allows transports to enforce structured output natively (CLI transports write schema to temp file; API transports pass it as `response_format`)
- Done when:
  - the Transport protocol is defined and testable against any implementation

### Task 2: Provider event schema

- Create: `src/ralph_ng/providers/events.py`
- Create: `tests/test_provider_events.py`
- Implement:
  - normalized provider event types (`agent.started`, `agent.commentary`, `agent.stderr`, `agent.progress`, `agent.structured_output`, `agent.finished`, `agent.error`)
  - Pydantic models with fields: `run_id`, `task_id`, `attempt_number`, `provider`, `role`, `review_round`, `timestamp`, payload
  - `review_round` field: 0 for executor, 1 for first review, 2 for challenge, 3 for critical final pass
  - serialization helpers for JSONL persistence
- Done when:
  - provider events can represent Claude and Codex activity without leaking CLI-specific shapes into the orchestrator
  - events carry `review_round` so the UI can distinguish first review from challenge round

### Task 3: CLI Claude transport (refactor from existing adapter)

- Modify: `src/ralph_ng/adapters/claude_executor.py`
- Modify: `src/ralph_ng/adapters/claude_evaluator.py`
- Create: `src/ralph_ng/adapters/cli_claude_transport.py`
- Create: `tests/test_cli_claude_transport.py`
- Implement:
  - extract subprocess spawning and `stream-json` line parsing into `CliClaudeTransport` implementing `Transport`
  - transport emits normalized provider events via callback
  - `CliClaudeTransport.invoke()` returns `TransportResult`
  - adapters refactored to consume `Transport`, not subprocess directly
- Reuse from Crossfire:
  - `packages/adapters/src/claude/claude-cli-process.ts` — subprocess lifecycle and output extraction patterns
- Done when:
  - Claude parsing is transport-driven
  - existing executor and evaluator behavior is unchanged
  - no `subprocess` import in adapter files (only in transport files)

### Task 4: Fake transports for testing

- Create: `src/ralph_ng/adapters/fake_transport.py`
- Create: `tests/test_fake_transport.py`
- Implement:
  - `FakeTransport` implementing `Transport` — replays event sequences from fixtures
  - configurable: latency simulation, error injection, partial output, structured output responses
  - single implementation (not separate FakeClaudeTransport/FakeCodexTransport) — the transport protocol is provider-neutral, so the fake should be too
- Reuse from Crossfire:
  - `packages/adapters/src/testing/fake-provider.ts` — synthetic turn pattern
- Done when:
  - adapter tests can run without real CLI binaries
  - fake transport satisfies the same protocol tests as real transports

### Task 5: Persist provider events

- Modify: `src/ralph_ng/events.py`
- Modify: `src/ralph_ng/orchestrator.py`
- Create: `tests/test_provider_event_logging.py`
- Implement:
  - provider event persistence by run/task/attempt (separate JSONL file per attempt)
  - stored under `.git/ralph/<run_id>/<task_id>/attempt_<n>/provider_events.jsonl`
- Done when:
  - provider activity can be replayed from disk independently of high-level business events

---

## Slice 2: Codex Review Integration

Goal: make Codex a first-class reviewer while leaving Claude as executor.

### Task 6: ReviewFindings and FindingResponse schemas

- Create: `src/ralph_ng/review_findings.py`
- Create: `tests/test_review_findings.py`
- Implement:
  - `FindingSeverity` enum: `critical`, `important`, `minor`
  - `FindingCategory` enum: `spec_compliance`, `correctness`, `security`, `testing`, `architecture`, `style`
  - `ReviewFinding` model: id, severity, category, description, evidence, suggested_fix, machine_checkable, verification_command
  - `ReviewFindings` model: decision, confidence, spec_compliance, summary, findings, repair_notes, new_verification_commands (NO `risk` field — risk is orchestrator-computed)
  - `FindingResponseVerdict` enum: `concede_and_fixed`, `already_correct`
  - `FindingResponse` model: finding_id, verdict, explanation
  - `ReviewResult` model: status, findings, input_tokens, output_tokens, duration_seconds, error_message, raw_events
  - JSON schema export for embedding in prompts (used by transports that support structured output)
  - parsing helpers with graceful degradation (malformed JSON fallback)
- Done when:
  - the review layer can express severity-graded, evidence-backed findings
  - findings have stable IDs for cross-round tracking

### Task 7: Risk classification

- Create: `src/ralph_ng/risk.py`
- Create: `tests/test_risk_classification.py`
- Implement:
  - `classify_diff_risk(diff, changed_files, config)` → `"low"` | `"medium"` | `"high"` | `"critical"`
  - size-based tiers: <50 lines low, 50-200 medium, 200-500 high, >500 critical
  - path-based overrides: security paths force critical, high-risk paths bump +1
  - file count multiplier: >3 files bumps low→medium, >8 bumps medium→high, >15 forces critical
- Done when:
  - risk is computed from the diff before review invocation, not self-reported by the reviewer

### Task 8: CLI Codex transport

- Create: `src/ralph_ng/adapters/cli_codex_transport.py`
- Create: `tests/test_cli_codex_transport.py`
- Implement:
  - `CliCodexTransport` implementing `Transport` ABC
  - `codex exec --json` process runner (NOT `codex exec review` — ralph is non-interactive, `exec` gives full prompt/schema control)
  - JSONL event parsing (line-by-line, tolerant of partial lines)
  - timeout and stderr handling
  - thread or session metadata capture when available
  - schema file injection for structured output (writes ReviewFindings JSON schema to temp file)
  - returns `TransportResult` (no exit codes in the interface)
- Reuse from Crossfire:
  - `packages/adapters/src/codex/codex-cli-transport.ts` — JSONL parsing, timeout, thread ID tracking
- Note: the JSONL parsing is reusable from Crossfire; the output schema and prompt envelope are net-new (Crossfire's Codex transport was built for debate turns, not review findings)
- Done when:
  - Codex CLI output is normalized into `TransportResult` plus provider events

### Task 9: Reviewer prompt templates

- Create: `src/ralph_ng/prompts/reviewer_first_pass.py`
- Create: `src/ralph_ng/prompts/challenge_executor.py`
- Create: `src/ralph_ng/prompts/challenge_reviewer.py`
- Create: `tests/test_reviewer_prompts.py`
- Implement:
  - first-pass review prompt with anti-sycophancy protocol, two-stage instructions, and ReviewFindings JSON schema
  - challenge-round executor prompt with repair-first rules and FindingResponse schema
  - challenge-round final reviewer prompt with re-review rules
  - all prompts are Python functions that accept structured context and return rendered prompt strings
  - prompts embed the JSON schemas for structured output enforcement
  - first-pass prompt includes finding ID instructions ("label each finding F1, F2, ...")
- Sources:
  - Crossfire's anti-sycophancy protocol (adapted from debate to review)
  - Superpowers' anti-trust language and evidence-before-claims rules
  - See spec section "Reviewer Prompt Templates" for exact text
- Done when:
  - prompts are testable (assert on structure, required sections, schema presence)
  - prompts do NOT leak executor rationale in the first-pass template
  - challenge executor prompt references findings by ID

### Task 10: ReviewerAdapter ABC and Codex reviewer adapter

- Create: `src/ralph_ng/adapters/codex_reviewer.py`
- Modify: `src/ralph_ng/adapters/base.py` (add `ReviewerAdapter` ABC)
- Create: `tests/test_codex_reviewer.py`
- Implement:
  - `ReviewerAdapter` ABC with `review()` → `ReviewResult` (distinct from `EvaluatorAdapter`)
  - `CodexReviewerAdapter` implementing `ReviewerAdapter`:
    - accepts a `Transport` (not subprocess directly)
    - uses `codex exec --json` with review prompt and ReviewFindings JSON schema
    - parsing into `ReviewFindings` / `ReviewResult`
    - graceful degradation when structured output is malformed (retry with schema reminder)
- Done when:
  - a Codex review pass produces `ReviewResult` and can be substituted for the current Claude evaluator

### Task 11: Adapter factory and role-based config

- Create: `src/ralph_ng/adapters/factory.py`
- Modify: `src/ralph_ng/config.py`
- Modify: `src/ralph_ng/cli.py`
- Create: `tests/test_adapter_factory.py`
- Create: `tests/test_role_config.py`
- Create: `tests/test_mixed_provider_cli.py`
- Implement:
  - `RoleConfig` model: provider, transport ("cli" now, "api" in v0.3), model, cli_path, plus reserved fields for v0.3 (base_url, api_key_env)
  - `[roles.executor]` and `[roles.reviewer]` config sections
  - `[review]` config section with thresholds, risk classification, challenge, and escalation settings
  - risk classification: built-in default security_paths and high_risk_paths, config entries extend (not replace) the defaults, `!pattern` syntax to remove a built-in
  - `build_executor(config)` → `ExecutorAdapter` factory function
  - `build_reviewer(config)` → `ReviewerAdapter` factory function
  - `_build_transport(config)` → `Transport` internal factory
  - backward compat: if no `[roles]` section, fall back to current `[models]` behavior (Claude for both, using `EvaluatorAdapter`)
- Default target:
  - Claude executor / Codex reviewer
- Done when:
  - the CLI can build different providers for executor and reviewer from config
  - existing configs without `[roles]` still work
  - adding a new transport type requires only a new `Transport` implementation and a config mapping

---

## Slice 3: Adversarial Review Orchestration

Goal: replace single-pass review with bounded independent-review plus challenge workflow. All challenge logic lives in the orchestrator — no separate `adversarial.py` module.

### Task 12: Independent first-pass review

- Modify: `src/ralph_ng/orchestrator.py`
- Modify: `src/ralph_ng/review_bundle.py`
- Create: `tests/test_independent_codex_review.py`
- Implement:
  - `_review_task()` uses `ReviewerAdapter` (Codex by default) instead of `EvaluatorAdapter`
  - review bundle strips executor rationale — only task contract, diff, snippets, verification, ratchet history
  - risk classification runs before review invocation (result passed to `should_challenge`, NOT to the reviewer)
  - `ReviewResult` consumed by orchestrator for verdict
  - existing `EvaluatorAdapter` path remains for Claude-only evaluator config (selected by factory based on config)
- Done when:
  - Codex receives only task, diff, snippets, verification, and prior overlay history
  - orchestrator correctly interprets `ReviewFindings` for accept/reject/repair decisions

### Task 13: Challenge round in orchestrator

- Modify: `src/ralph_ng/orchestrator.py`
- Create: `tests/test_challenge_round.py`
- Implement:
  - `should_challenge(findings, diff_risk, config)` function with configurable thresholds (see spec)
  - `_challenge_round()` method on orchestrator:
    1. send findings to executor (Claude) with repair-first prompt and FindingResponse schema
    2. executor produces new diff + list of `FindingResponse` objects (one per finding ID)
    3. run verification on new diff
    4. run machine-checkable finding verification commands through ratchet validation pipeline
    5. send new diff + finding responses to reviewer (Codex) with final-review prompt
    6. reviewer produces final `ReviewFindings`
  - for critical risk only: optional round 3 (clean-slate verification pass, no prior context)
  - challenge round emits provider events with `review_round=2` (or `3` for critical final pass)
  - skip-challenge fast path: confidence >= 0.95 AND risk == low AND diff < 50 lines AND no security paths AND no critical findings
- Done when:
  - the loop supports at most 2 review rounds (3 for critical) and exits cleanly to repair, accept, or human escalation
  - challenge triggers are configurable and defaulted to evidence-backed values
  - finding IDs are tracked across rounds

### Task 14: Human escalation policy

- Modify: `src/ralph_ng/orchestrator.py`
- Modify: `src/ralph_ng/progress.py`
- Create: `tests/test_review_escalation.py`
- Implement:
  - `should_escalate()` function with configurable thresholds:
    - reviewer disagrees after challenge round
    - all rounds below confidence 0.60
    - critical risk below confidence 0.80
    - security paths below confidence 0.85
    - consecutive rejection count >= `max_rejection_loops` (default 3, counted across attempts, separate from `max_retries_per_task`)
    - critical security/data-loss findings persist after repair
  - `blocked_human` reason includes: reviewer findings (structured), executor finding responses, unresolved question, recommended action
  - all provider event logs for the attempt are referenced in the block reason
- Done when:
  - unresolved reviewer/executor disagreement produces a clear, actionable operator-facing block reason
  - rejection loop counter is distinct from retry counter

---

## Slice 4: Live Provider Visibility + Validation

Goal: make live Claude and Codex activity visible during execution.

### Task 15: Rich progress upgrade

- Modify: `src/ralph_ng/progress.py`
- Create: `tests/test_provider_progress.py`
- Implement:
  - Claude activity pane: current tool/action, latest commentary, elapsed time
  - Codex activity pane: current review stage (spec compliance / code quality), latest commentary, round number
  - review round indicator (sourced from provider event `review_round` field)
  - provider-level elapsed time
- Done when:
  - `ralph start` and `ralph resume` show distinct provider activity rather than only coarse task state

### Task 16: CLI live renderer integration

- Modify: `src/ralph_ng/cli.py`
- Create: `tests/test_live_progress_cli.py`
- Implement:
  - live updates sourced from provider event stream
  - stable fallback when not attached to a terminal (plain log lines)
- Done when:
  - the operator can tell whether Claude is editing or Codex is reviewing in real time

### Task 17: Real CLI smoke tests

- Create: `tests/smoke/test_codex_cli_smoke.py`
- Create: `tests/smoke/test_claude_cli_smoke.py`
- Implement:
  - opt-in real CLI smoke tests for local environments where both tools are installed
  - tiny repo fixture and strict runtime cap (60s per invocation)
  - marked with `@pytest.mark.smoke` for CI skip
  - tests verify `Transport` protocol compliance with real CLIs
- Done when:
  - the adapter layer is validated against real subprocess behavior, not only fake transports

### Task 18: End-to-end mixed-provider run

- Create: `tests/e2e/test_claude_executor_codex_reviewer.py`
- Implement:
  - full run with fake transports (no real CLI needed)
  - scenarios:
    1. happy path: execute → review accepts (fast path, skip challenge) → commit
    2. reject (high confidence) → repair → accept → commit
    3. accept (low confidence) → challenge round → accept → commit
    4. reject (low confidence) → challenge round → reject → repair → accept → commit
    5. escalation: reject → challenge → disagree → human escalation
    6. critical risk: 3-round review path (independent → challenge → clean-slate final)
    7. rejection loop exhaustion: 3 consecutive rejections → human escalation
    8. machine-checkable finding validation: reviewer proposes command → ratchet validates → runs after repair
  - assertions:
    - provider event persistence with correct `review_round` values
    - `ReviewFindings` and `FindingResponse` schema compliance
    - risk classification correctness
    - finding ID tracking across rounds
    - transport swappability: same test runs with both `FakeTransport` and (optionally) real CLI
- Done when:
  - the mixed-provider happy path, retry path, challenge path, escalation path, and rejection loop path are all covered

---

## Prompting Guidelines

The implementation should encode these review prompting rules, derived from research and validated patterns:

1. **Independent first pass.** Reviewer never sees executor reasoning on round 1.
2. **Start with the strongest failure mode.** "What is the most likely way this code breaks in production?" before nitpicks.
3. **Require evidence for every finding.** file:line citations, diff hunks, or concrete failure scenarios. No generic critique.
4. **Two-stage evaluation.** Spec compliance first (did they build the right thing?), then code quality (did they build it well?).
5. **Anti-trust by default.** AI-generated code gets higher scrutiny. Passing tests are not proof of correctness.
6. **Severity grading.** Critical blocks merge. Important should fix. Minor is a suggestion.
7. **Machine-checkable where possible.** If a finding can be verified by running a command, include the command.
8. **Repair first, explain second.** In challenge rounds, the executor fixes before arguing.
9. **Confidence reflects certainty, not quality.** A clean-looking diff the reviewer hasn't deeply examined warrants low confidence.
10. **Bounded rounds.** 2 rounds default, 3 for critical. No open-ended debate.

## Reuse Map

### From Crossfire

| Source | Target | What to port |
|---|---|---|
| `packages/adapters/src/base/provider-adapter.ts` | `adapters/transport.py` | Transport ABC pattern (adapted: ours is synchronous blocking, theirs is AsyncGenerator — ralph is turn-based) |
| `packages/adapters/src/codex/codex-cli-transport.ts` | `adapters/cli_codex_transport.py` | JSONL parsing, timeout, thread ID tracking |
| `packages/adapters/src/claude/claude-cli-process.ts` | `adapters/cli_claude_transport.py` | Subprocess lifecycle, output extraction |
| `apps/daemon/src/services/progress.ts` | `providers/events.py` | Provider event bus pattern |
| `packages/adapters/src/prompts/structured-turn.ts` | `prompts/*.py` | Anti-sycophancy protocol (adapted) |
| `packages/adapters/src/testing/fake-provider.ts` | `adapters/fake_transport.py` | Fake transport pattern |

### From Superpowers

| Source | Target | What to port |
|---|---|---|
| `skills/subagent-driven-development/spec-reviewer-prompt.md` | `prompts/reviewer_first_pass.py` | Anti-trust framing, spec compliance stage |
| `skills/subagent-driven-development/code-quality-reviewer-prompt.md` | `prompts/reviewer_first_pass.py` | Structured quality checklist |
| `skills/receiving-code-review/SKILL.md` | `prompts/challenge_executor.py` | Anti-sycophancy forbidden responses |
| `skills/verification-before-completion/SKILL.md` | `prompts/reviewer_first_pass.py` | Evidence-before-claims gate |

### Do not port

- Crossfire: Fastify server, React frontend, spec-generation phase machine, multi-turn consensus protocol
- Superpowers: skill activation system, hook infrastructure, brainstorming workflow

## Risks

### Risk 1: Codex structured output is less rich than needed

Mitigation:
- use `codex exec --json` with explicit ReviewFindings JSON schema for structured output enforcement
- graceful degradation with retry and schema reminder on malformed output

### Risk 2: Provider resume semantics diverge

Mitigation:
- start with conservative no-resume behavior for Codex across tasks
- only add intra-review resume after proving it works

### Risk 3: Adversarial rounds increase latency too much

Mitigation:
- run challenge rounds only on policy triggers
- skip-challenge fast path for small, low-risk, high-confidence diffs
- default max review rounds to 2 (3 for critical only)

### Risk 4: Confidence scores are poorly calibrated

Mitigation:
- thresholds are configurable, not hardcoded
- defaults are set conservatively (challenge is the default for non-trivial diffs)
- the spec recommends a calibration validation step before tuning thresholds in production

### Risk 5: The progress UI becomes noisy

Mitigation:
- show only the latest provider commentary and a short rolling history
- keep raw provider events persisted on disk, not all rendered live

### Risk 6: Transport abstraction leaks CLI assumptions

Mitigation:
- `TransportResult` has no `exit_status` — CLI transports can include it in `raw_events` for debugging
- `Transport.invoke()` takes `json_schema` param that CLI transports can ignore but API transports need
- protocol tests verify any transport implementation satisfies the contract
- Slice 1 Task 1 explicitly tests the protocol, not just one implementation

## Acceptance Gate

This feature is complete when:

1. `ralph` can run Claude executor plus Codex reviewer from config.
2. Provider-level live activity is visible during execution.
3. The first review pass is independent and two-staged (spec compliance + code quality).
4. Risk classification is computed from the diff.
5. A bounded challenge round exists and is threshold-driven.
6. Challenge uses repair-first-then-explain with structured FindingResponse tracking.
7. Human escalation triggers on persistent disagreement, low confidence, or critical risk.
8. Mixed-provider artifacts persist cleanly.
9. Fake transports enable full test coverage without real CLIs.
10. The full mixed-provider test suite passes (unit + e2e).
11. Adapters depend on Transport ABC — no subprocess imports in adapter code.
12. Adding a new transport type requires only a new Transport implementation and config mapping — zero adapter or orchestrator changes.
