# Claude Executor + Codex Reviewer — Adversarial Review Protocol

Status: Refined draft (v4 — decisions applied)

## Goal

Extend `ralph-ng` beyond v0.1 so it can:

- use Claude as the code-writing executor
- use Codex as the adversarial reviewer
- surface real-time provider activity for both tools
- run a bounded adversarial review protocol that improves code quality without turning into open-ended debate

This is a v0.2 feature expansion. It is intentionally not framed as a v0.1 patch, because the current v0.1 spec explicitly excludes both cross-vendor model support and mediated multi-model discussion rounds.

## Problem

The current `ralph-ng` implementation is strong as a single-loop autonomous executor, but it still has three structural limitations:

1. It only has Claude adapters in practice.
2. Its live progress model is task-level, not provider-level, so the operator cannot see what Claude or Codex are doing in real time.
3. Its review loop is single-pass reject-or-accept. It does not support a bounded adversarial challenge protocol where one model tries to falsify the other model's work, then the executor rebuts or repairs.

These gaps matter because:

- cross-model review catches different blind spots than same-model review
- real-time provider observability builds trust and shortens debugging time
- bounded adversarial prompting raises code quality more than a simple independent pass when tasks are subtle, risky, or security-sensitive

## Product Outcome

After this feature lands, a typical run should look like this:

1. `ralph start` validates the repo and plan.
2. Claude executes the selected task.
3. The operator can see live Claude progress while it works.
4. Verification commands run.
5. Codex reviews the resulting diff and verification evidence in two stages: spec compliance, then code quality.
6. If Codex rejects, Ralph records evidence-backed findings with severity and returns the task to Claude for repair.
7. If the review triggers challenge policy (risk, confidence, or ambiguity), Ralph runs one bounded adversarial challenge round: Claude repairs first, then explains; Codex produces a final verdict on the new diff.
8. Ralph commits only after:
   - effective verification passes
   - final reviewer verdict is `accept`

The operator sees both provider streams in real time and is only interrupted for actual blockers or unresolved reviewer/executor disagreement.

## Non-Goals

This feature does not include:

- multiple concurrent writers in the same repository
- Crossfire's full web UI, Fastify daemon, or session browser
- automatic model election
- open-ended debate loops (Crossfire's multi-turn consensus protocol is explicitly out of scope)
- parallel task execution
- replacing the current SQLite plus JSONL durability model
- OpenAI-compatible API transport (v0.3 — but the adapter layer MUST be designed to accommodate it; see "Adapter Swappability" section)

## Key Decisions

### 1. Claude remains the executor

Claude is already integrated, already tested, and is currently the stable path for repository-writing work inside `ralph-ng`.

Default:

- executor provider: `claude`
- executor model: `sonnet`

### 2. Codex becomes the default reviewer

Codex is better treated as a review-side model first, not as a second writer.

Default:

- reviewer provider: `codex`
- reviewer model: `gpt-5.3-codex`

The model choice stays configurable, but the default topology should be:

- Claude executor
- Codex reviewer

### 3. Reuse Crossfire and Superpowers patterns, not their runtimes

Crossfire already solved the hardest provider-integration problem: it normalizes very different Claude and Codex subprocess outputs into a common event stream. Superpowers solved the hardest review-prompting problem: how to make an AI reviewer actually distrust the implementer's output.

**From Crossfire, port conceptually:**

- `packages/adapters/src/base/provider-adapter.ts` — event normalization
- `packages/adapters/src/codex/codex-cli-transport.ts` — Codex JSONL parsing
- `packages/adapters/src/claude/claude-cli-process.ts` — Claude subprocess normalization
- `apps/daemon/src/services/progress.ts` — provider event bus
- `packages/adapters/src/prompts/structured-turn.ts` — anti-sycophancy protocol (adapted for review, not debate)

**From Superpowers, port conceptually:**

- `skills/subagent-driven-development/spec-reviewer-prompt.md` — anti-trust reviewer framing
- `skills/subagent-driven-development/code-quality-reviewer-prompt.md` — structured quality checklist
- `skills/receiving-code-review/SKILL.md` — anti-sycophancy forbidden responses
- `skills/verification-before-completion/SKILL.md` — evidence-before-claims gate function

**Do not port:**

- Crossfire's Fastify routes, React web UI, session machine, or spec-workshop phase orchestration
- Superpowers' skill activation system or hook infrastructure

The right move is to port the transport, event-normalization, and review-prompting patterns into Python and plug them into `ralph-ng`'s existing orchestrator, state store, and CLI.

### 4. Two-stage review: spec compliance, then code quality

Adapted from Superpowers' dual-stage pattern. The reviewer performs two distinct evaluation passes within a single invocation:

**Stage 1 — Spec compliance:** Does the diff satisfy the task contract? Are all acceptance criteria met? Is anything missing? Is anything extra that was not requested (YAGNI)?

**Stage 2 — Code quality:** Is the implementation correct, safe, maintainable? Severity-categorized findings (critical/important/minor).

Why two stages:

- spec compliance catches "built the wrong thing" before wasting time on quality review
- quality review catches "built it badly" only after confirming the right thing was built
- structured severity lets the orchestrator distinguish blocking issues from suggestions

### 5. Independent review comes before adversarial challenge

The reviewer should not initially see the executor's rationale. The first review pass should be independent and evidence-driven.

Why:

- it reduces confirmation bias (research shows LLMs exhibit systematic anchoring when exposed to prior reasoning)
- it preserves the value of an external falsification pass
- it is simpler to reason about and test

Only after the first Codex pass should Claude see the findings and repair.

### 6. Repair-first, then explain — not argue-first

After Codex rejects, Claude must **repair the code first**, then explain what changed and why the finding is now addressed (or was already fine). This differs from Crossfire's debate model where models argue over prose.

Why:

- repair produces a new diff that Codex can objectively re-evaluate
- pure rebuttal creates an incentive for Claude to argue its way out of work
- a new diff is machine-checkable; a prose argument is not
- this matches Superpowers' "evidence-before-claims" principle

### 7. Bounded adversarial review, not free-form debate

The review protocol caps at:

- 2 rounds for low/medium/high risk
- 3 rounds for critical risk (the third round is a final verification-only pass)

No additional rounds without explicit future design.

This avoids:

- runtime explosion
- rhetorical loops
- operator confusion about which verdict matters

### 8. Adapter layer must be transport-agnostic (v0.3 readiness)

v0.2 uses CLI transports (subprocess spawning). v0.3 will add OpenAI-compatible API transports so users can plug in any model via OpenRouter or direct API. The adapter layer must be designed now so that v0.3 is an additive change (new transport, new config fields), not a rewrite.

Concretely:

- adapters depend on a `Transport` ABC, not on subprocess directly
- the `Transport` interface is prompt-in/result-out — no CLI-specific concepts (exit codes, stderr) in the interface
- CLI-specific details live inside the CLI transport implementations
- the config schema accepts `transport = "cli"` now and will accept `transport = "api"` in v0.3 without breaking changes
- the adapter factory wires transport → adapter based on config

This is a design constraint, not a build task. We do NOT build the API transport in v0.2. We build the abstraction that makes it a clean addition later.

### 9. Provider activity must be first-class runtime data

The current task-level JSONL event log is not enough for mixed-provider visibility.

We need a provider-neutral event model that can represent:

- provider start/stop
- commentary or progress messages
- stderr
- tool use or action summaries
- structured review findings
- final verdicts

This event stream should drive:

- Rich terminal progress
- persisted artifacts
- possible future daemon or SSE mode

## Architecture Changes

### Provider-neutral runtime events

Add a new internal event layer for provider activity. Normalized event types:

- `agent.started`
- `agent.commentary`
- `agent.stderr`
- `agent.progress`
- `agent.structured_output`
- `agent.finished`
- `agent.error`

Each event should carry:

- `run_id`
- `task_id`
- `attempt_number`
- `provider`
- `role`
- `review_round` (0 for executor, 1 for first review, 2 for challenge, 3 for critical final pass)
- `timestamp`
- provider-specific payload

This layer is different from the existing high-level business events like `task.transition` or `verification.result`.

### Adapter stack — designed for swappability

The adapter layer must be designed so that v0.3 can add OpenAI-compatible API transports (OpenRouter, direct API, etc.) without changing the orchestrator, prompts, or review logic. Users should be able to swap any model into either role provided they have an API key.

**Three-layer architecture:**

```
Orchestrator
    ↓ talks to
Adapter (role semantics: ExecutorAdapter / ReviewerAdapter)
    ↓ talks to
Transport (I/O mechanism: CLI subprocess / HTTP API)
```

The orchestrator never touches transports directly. Adapters are role-specific (executor, reviewer). Transports are I/O-specific (CLI, API). A single transport type can serve multiple roles.

**Transport protocol (abstract, synchronous):**

The transport layer is synchronous. Ralph is turn-based — the orchestrator waits for each invocation to complete before deciding what to do next. Even the v0.3 API transport will be synchronous (blocking HTTP call with timeout). There is no benefit to async here and it would infect the entire call stack for no reason.

```python
class Transport(ABC):
    @abstractmethod
    def invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        cwd: str | None = None,
        json_schema: dict | None = None,
    ) -> TransportResult:
        """Send a prompt and block until complete. Emits provider events via callback."""

    @abstractmethod
    def health_check(self) -> bool:
        """Verify the transport can reach its provider."""
```

v0.2 transports:

- `CliClaudeTransport` — subprocess + stream-json parsing
- `CliCodexTransport` — subprocess + JSONL parsing
- `FakeTransport` — test double

v0.3 transports (not built now, but the interface must accommodate):

- `OpenAIApiTransport` — synchronous HTTP to any OpenAI-compatible endpoint (OpenRouter, direct OpenAI, local vLLM, etc.)

**Transport result (provider-neutral):**

```python
class TransportResult(BaseModel):
    status: InvocationStatus
    output_text: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    error_message: str | None
    raw_events: list[dict]
```

Note: `TransportResult` drops `exit_status` from `AdapterResult` — HTTP transports don't have exit codes. The CLI transports can include it in `raw_events` if needed for debugging, but it must not be part of the interface the adapter depends on.

**Adapter protocol (abstract):**

```python
class ExecutorAdapter(ABC):
    @abstractmethod
    def execute(self, prompt: str, *, timeout_seconds: float, cwd: str | None = None) -> ExecutorResult:
        ...

class ReviewerAdapter(ABC):
    @abstractmethod
    def review(self, review_prompt: str, *, timeout_seconds: float, cwd: str | None = None) -> ReviewResult:
        ...
```

`ReviewerAdapter` is new and distinct from `EvaluatorAdapter`. It returns `ReviewResult` (which wraps `ReviewFindings`), not `EvaluatorResult`. The existing `EvaluatorAdapter` remains for the Claude-only backward-compat path.

**Adapter factory:**

The factory reads the config and wires transport → adapter:

```python
def build_executor(config: RoleConfig) -> ExecutorAdapter:
    transport = _build_transport(config)
    if config.provider in ("claude",):
        return ClaudeExecutorAdapter(transport=transport, model=config.model)
    # v0.3: generic executor via API transport
    raise ConfigLoadError(f"No executor adapter for provider: {config.provider}")

def build_reviewer(config: RoleConfig) -> ReviewerAdapter:
    transport = _build_transport(config)
    if config.provider in ("codex",):
        return CodexReviewerAdapter(transport=transport, model=config.model)
    if config.provider in ("claude",):
        return ClaudeReviewerAdapter(transport=transport, model=config.model)
    # v0.3: generic reviewer via API transport
    raise ConfigLoadError(f"No reviewer adapter for provider: {config.provider}")

def _build_transport(config: RoleConfig) -> Transport:
    if config.transport == "cli":
        return _CLI_TRANSPORT_MAP[config.provider](cli_path=config.cli_path, ...)
    if config.transport == "api":
        return OpenAIApiTransport(base_url=config.base_url, api_key=config.api_key, ...)
    raise ConfigLoadError(f"Unknown transport: {config.transport}")
```

**Python modules:**

- `src/ralph_ng/adapters/transport.py` — `Transport` ABC + `TransportResult`
- `src/ralph_ng/adapters/cli_claude_transport.py`
- `src/ralph_ng/adapters/cli_codex_transport.py`
- `src/ralph_ng/adapters/fake_transport.py`
- `src/ralph_ng/adapters/base.py` — `ExecutorAdapter`, `ReviewerAdapter` ABCs
- `src/ralph_ng/adapters/claude_executor.py` — refactored to use transport
- `src/ralph_ng/adapters/codex_reviewer.py`
- `src/ralph_ng/adapters/factory.py` — `build_executor()`, `build_reviewer()`
- `src/ralph_ng/providers/events.py` — provider event bus

### ReviewFindings schema (new, replaces EvaluatorVerdict for reviewer path)

The existing `EvaluatorVerdict` (`decision`, `repair_notes`, `new_verification_commands`, `summary`) is too thin for structured adversarial review. Introduce a new `ReviewFindings` schema:

```python
class FindingSeverity(str, Enum):
    CRITICAL = "critical"    # blocks merge: bugs, security, data loss
    IMPORTANT = "important"  # should fix: architecture, missing tests, poor error handling
    MINOR = "minor"          # nice to have: style, naming, minor optimization

class FindingCategory(str, Enum):
    SPEC_COMPLIANCE = "spec_compliance"
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTING = "testing"
    ARCHITECTURE = "architecture"
    STYLE = "style"

class ReviewFinding(BaseModel):
    id: str                  # stable identifier for cross-round references, e.g. "F1", "F2"
    severity: FindingSeverity
    category: FindingCategory
    description: str         # what is wrong
    evidence: str            # file:line reference, diff hunk, or test case
    suggested_fix: str | None
    machine_checkable: bool  # can this be verified by running a command?
    verification_command: str | None  # if machine_checkable, the command

class ReviewFindings(BaseModel):
    decision: Literal["accept", "reject"]
    confidence: float        # 0.0 - 1.0, reviewer's certainty in their OWN verdict
    spec_compliance: Literal["pass", "fail", "partial"]
    summary: str
    findings: list[ReviewFinding]
    repair_notes: str | None
    new_verification_commands: list[str]

class FindingResponseVerdict(str, Enum):
    CONCEDE_AND_FIXED = "concede_and_fixed"
    ALREADY_CORRECT = "already_correct"

class FindingResponse(BaseModel):
    finding_id: str          # references ReviewFinding.id
    verdict: FindingResponseVerdict
    explanation: str         # what changed, or why the finding is wrong (with evidence)
```

Note: `ReviewFindings` does NOT include a `risk` field. Risk is computed by the orchestrator from the diff (see "Risk classification" section) — not self-reported by the reviewer. Letting the reviewer self-report risk creates a conflict with the orchestrator's objective measurement and allows gaming (a sycophantic reviewer could downplay risk to justify an accept).

The `FindingResponse` schema structures the executor's challenge-round output. Each finding from the reviewer gets a tracked response with a stable `finding_id`, so the final reviewer pass can verify each was addressed.

The existing `EvaluatorVerdict` remains for backward compatibility with the Claude-only evaluator path. The orchestrator consumes whichever schema the active reviewer produces.

**ReviewResult** wraps the transport result and the parsed findings:

```python
class ReviewResult(BaseModel):
    status: InvocationStatus
    findings: ReviewFindings | None
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    error_message: str | None
    raw_events: list[dict]
```

### Role-based configuration

The current config is model-centric and assumes Claude for both roles in practice.

Add explicit role/provider config:

```toml
[roles.executor]
provider = "claude"
transport = "cli"        # "cli" for v0.2, "api" for v0.3
model = "sonnet"
cli_path = "claude"      # only for transport = "cli"
# v0.3 fields (not used yet, but the schema accepts them):
# base_url = "https://openrouter.ai/api/v1"
# api_key_env = "OPENROUTER_API_KEY"

[roles.reviewer]
provider = "codex"
transport = "cli"
model = "gpt-5.3-codex"
cli_path = "codex"

[review]
enabled = true
stages = ["spec_compliance", "code_quality"]

[review.thresholds]
# Confidence thresholds — see "Challenge Round Trigger Policy" section
auto_accept_min_confidence = 0.95
challenge_below_confidence = 0.80
auto_reject_min_confidence = 0.90
challenge_reject_below = 0.70
escalate_below_confidence = 0.60

[review.risk_classification]
low_max_lines = 50
medium_max_lines = 200
high_max_lines = 500
# above 500 = critical

# security_paths and high_risk_paths have sensible built-in defaults.
# Config entries EXTEND the built-ins (they do not replace them).
# To remove a built-in path, prefix with "!" (e.g., "!**/migrations/**").
#
# Built-in security_paths:
#   **/auth/**, **/crypto/**, **/security/**, **/migrations/**, **/schema/**,
#   **/.env*, **/secrets/**, **/credentials/**, **/Dockerfile, **/*.tf,
#   **/ci/**, **/.github/workflows/**
#
# Built-in high_risk_paths:
#   **/api/**, **/routes/**, **/handlers/**, **/middleware/**,
#   **/config/**, **/payments/**, **/billing/**
#
# Example: add project-specific paths, remove one built-in
# extra_security_paths = ["**/keys/**"]
# extra_high_risk_paths = ["**/webhooks/**"]
# remove_security_paths = ["**/migrations/**"]

[review.challenge]
max_rounds_low = 1
max_rounds_medium = 2
max_rounds_high = 2
max_rounds_critical = 3

[review.escalation]
on_reviewer_disagreement = true
on_all_rounds_below_confidence = 0.60
on_critical_below_confidence = 0.80
on_security_path_below_confidence = 0.85
max_rejection_loops = 3
```

### Reviewer execution mode

Use `codex exec --json` with a review-only prompt and the `ReviewFindings` JSON schema for structured output. Ralph is non-interactive and turn-based — `codex exec` is the correct command for this. `codex exec review` is not used.

### Session reuse policy

Claude and Codex resume semantics are different.

Use conservative defaults:

- Claude: reuse where it is already stable in task repair flows
- Codex: no cross-task resume
- Codex challenge rounds: resume only if testing proves it is reliable within the same review phase

Do not assume Codex resume works cleanly across unrelated prompt shapes.

## Adversarial Review Protocol

### Reviewer persona: The Auditor

Crossfire uses debate personas (Dr. Chen the advocate, Dr. Rivera the critic) designed for multi-turn convergence on a shared spec. That framing is wrong for code review, where:

- there is no convergence goal — the reviewer's job is to find problems, not agree
- the reviewer never writes code — it only evaluates
- findings must be evidence-backed and severity-graded, not discursive

The reviewer persona for ralph-ng is **The Auditor**: a senior security and reliability engineer performing an independent code audit. Key differences from Crossfire's debate personas:

| Crossfire debate | Ralph-ng code review |
|---|---|
| Two equal participants converging | One implementer, one auditor |
| Goal: reach consensus | Goal: find every defect |
| Personas argue and rebut | Auditor reports; implementer repairs |
| Multi-turn until convergence | Bounded to 2-3 rounds |
| Open-ended disagreements | Severity-graded findings |

### Anti-sycophancy protocol (adapted for review)

Derived from Crossfire's independence protocol and Superpowers' anti-trust patterns:

```
INDEPENDENCE PROTOCOL:

You are performing an independent code audit. You have NOT seen the implementer's
reasoning, intent, or self-assessment. You are working from the diff, the task
contract, and the verification evidence alone.

ANTI-TRUST RULES:
- Treat all code with maximum scrutiny regardless of who or what wrote it.
- Do NOT assume the implementer's intent was correct. Verify against the task contract.
- Do NOT assume passing tests mean the code is correct. Tests may be insufficient,
  wrong, or testing the wrong thing.
- If the code looks clean and you find nothing wrong, STOP. Re-examine from a
  different angle: security, error handling, edge cases, concurrency, resource leaks,
  injection vectors, data validation at boundaries.
- Agreement must be earned through evidence. "The code looks correct" is not evidence.
  "Lines 42-58 correctly validate input boundaries as specified in acceptance
  criterion 3" is evidence.

FORBIDDEN:
- "The implementation looks good"
- "The code is well-structured"
- "No major issues found" (without specific evidence of what was checked)
- Any positive assessment without citing specific lines or behaviors

REQUIRED:
- At least 3 specific areas examined before any accept verdict
- file:line or diff hunk citations for every finding
- For accept verdicts: explicit statement of what was checked and why it passes
- For reject verdicts: severity-graded findings with suggested fixes
```

### Round 1: Independent review (two-stage)

Codex receives:

- task contract (id, title, description, acceptance criteria)
- verification commands and their results
- unified diff
- changed file snippets (full file content for context)
- prior ratchet overlay history
- effective verification results

Codex does NOT receive:

- Claude's internal reasoning or commentary
- the executor prompt
- previous attempt rationale

Codex performs two evaluation stages in a single invocation:

**Stage 1 — Spec compliance:**
- For each acceptance criterion: does the diff satisfy it? Cite evidence.
- Is anything missing from the requirements?
- Is anything present that was NOT requested? (YAGNI check)
- Are verification commands testing the right things?

**Stage 2 — Code quality:**
- Identify the strongest plausible failure mode first (not the easiest nitpick)
- Check: correctness, security, error handling, testing adequacy, architecture
- Grade each finding by severity (critical/important/minor)
- For each finding: cite file:line, explain the failure scenario, suggest a fix
- If a finding is machine-checkable, provide a verification command

Codex must output a `ReviewFindings` object.

### Challenge round trigger policy

The challenge round runs when the orchestrator determines the first-pass verdict is uncertain or the risk warrants a second look.

```python
def should_challenge(findings: ReviewFindings, diff_risk: str, config: ReviewConfig) -> bool:
    """Determine whether to run a challenge round after first-pass review.

    Returns False (skip challenge) when the verdict is clear enough to act on directly:
    - High-confidence reject → go straight to repair
    - High-confidence accept on low-risk diff → go straight to commit

    Returns True (run challenge) when the verdict is uncertain or the risk warrants it.
    """

    has_critical_findings = any(f.severity == "critical" for f in findings.findings)
    has_security_findings = any(f.category == "security" for f in findings.findings)

    if findings.decision == "reject":
        # High-confidence reject → skip challenge, go straight to repair
        if findings.confidence >= config.auto_reject_min_confidence:
            return False
        # Low-confidence reject → may be overcorrection, let executor respond
        return True

    # From here: decision == "accept"

    # Critical-severity findings with accept verdict — contradictory, always challenge
    if has_critical_findings:
        return True

    # Security findings with accept — always challenge
    if has_security_findings:
        return True

    # Accept on high/critical risk — always double-check
    if diff_risk in ("high", "critical"):
        return True

    # Low confidence accept — may be rubber-stamping
    if findings.confidence < config.challenge_below_confidence:
        return True

    # High confidence accept on low risk with no security/critical findings → skip challenge
    return False
```

Default thresholds:

| Parameter | Default | Rationale |
|---|---|---|
| `auto_accept_min_confidence` | 0.95 | LLMs are overconfident; only skip challenge at very high confidence + low risk |
| `challenge_below_confidence` | 0.80 | Below this, the reviewer itself is uncertain — get a second look |
| `auto_reject_min_confidence` | 0.90 | High-confidence reject goes straight to repair |
| `challenge_reject_below` | 0.70 | Low-confidence reject may be overcorrection (LLMs show systematic overcorrection bias in code review) |
| `escalate_below_confidence` | 0.60 | Below this, neither accept nor reject is trustworthy |

**Skip challenge (fast path) when ALL of:**

- confidence >= 0.95
- risk == "low"
- no security-path files touched
- no critical-severity findings
- diff < 50 lines

This means the challenge round is the **default** for anything non-trivial. Since every diff is AI-authored, the second pass is warranted more often than not.

### Round 2: Repair + challenge

**Step 1 — Claude repairs.** Claude receives:

- the original task contract
- the Codex findings (full structured `ReviewFindings` output)
- the current diff and verification evidence
- instruction: "Fix every valid finding. For findings you believe are incorrect, explain why with specific evidence after making all other repairs."

Claude produces:

- a new diff (the repaired code)
- a list of `FindingResponse` objects, one per finding, each referencing the finding by `finding_id`

**Step 2 — Codex re-reviews.** Codex receives:

- the original task contract
- the original findings (so it can verify each was addressed)
- Claude's `FindingResponse` list
- the NEW diff (not the original)
- new verification results

Codex produces a final `ReviewFindings` verdict on the new diff.

Note: the final reviewer intentionally sees the original findings and the executor's responses. This is not the same as the independence constraint on Round 1. In the challenge round, the reviewer's job is to verify that specific findings were addressed — that requires seeing what was claimed. The prompt explicitly instructs the reviewer to evaluate the NEW diff independently and not anchor on prior severity assessments.

### Round 3 (critical risk only): Final verification pass

Only runs when `risk == "critical"` and `max_rounds_critical >= 3`.

This is a lightweight verification-only pass:

- Codex receives only the final diff, verification results, and acceptance criteria
- no prior findings or responses (clean slate)
- focused solely on "does this diff meet the contract and pass verification?"

### Risk classification

Risk is computed from the diff, not self-reported by the reviewer. The orchestrator calculates risk before invoking the reviewer:

```python
def classify_diff_risk(diff: str, changed_files: list[str], config: RiskConfig) -> str:
    lines_changed = count_diff_lines(diff)
    files_changed = len(changed_files)

    # Path-based overrides (highest priority)
    if any(matches_glob(f, config.security_paths) for f in changed_files):
        return "critical"

    # Size-based classification
    if lines_changed > config.high_max_lines or files_changed > 15:
        return "critical"
    if lines_changed > config.medium_max_lines or files_changed > 8:
        return "high"
    if lines_changed > config.low_max_lines or files_changed > 3:
        return "medium"

    base = "low"

    # Path-based bump
    if any(matches_glob(f, config.high_risk_paths) for f in changed_files):
        base = bump_risk(base)  # low -> medium, medium -> high

    return base
```

### Machine-checkable finding validation

When the reviewer produces findings with `machine_checkable = true` and a `verification_command`, the orchestrator should run those commands automatically after the executor's repair step (same as ratchet overlay commands). This provides objective evidence for the final reviewer pass and prevents the executor from claiming a fix that doesn't actually work.

These reviewer-proposed commands go through the same ratchet validation pipeline (reject dangerous patterns, test-execute for syntax errors, enforce max checks per task).

### Rejection loop accounting

Clarification on `max_rejection_loops` vs `max_retries_per_task`:

- `max_retries_per_task` (from `[ralph]` config) = total attempts for a task across all review rounds. Each full cycle of execute → verify → review counts as one attempt.
- `max_rejection_loops` (from `[review.escalation]` config) = consecutive rejections without an accept across attempts. If the reviewer rejects on attempts 1, 2, and 3, that's 3 rejection loops — escalate to human regardless of remaining retry budget.

These are separate counters. A task can exhaust its rejection loop budget before hitting `max_retries_per_task` (escalate to human), or exhaust retries before hitting rejection loop budget (transition to `FAILED_EXHAUSTED`).

### Escalation to human

Escalate when:

- the reviewer and executor still disagree after the bounded challenge round
- all review rounds have confidence below 0.60
- risk is critical and any round confidence is below 0.80
- security-path files are touched and any round confidence is below 0.85
- the same task has been rejected `max_rejection_loops` consecutive times (default: 3)
- provider outputs cannot be parsed or normalized safely
- critical-severity security or data-loss findings persist after repair

The operator should see:

- Codex findings (structured, with severity)
- Claude's repair response
- final unresolved question
- recommended next action
- all provider event logs for the attempt

## Reviewer Prompt Templates

### First-pass review prompt

```
You are a senior security and reliability engineer performing an independent code
audit. Your professional reputation depends on finding every real defect — you
consider it a failure if a bug ships because you were too lenient.

INDEPENDENCE PROTOCOL:
You have NOT seen the implementer's reasoning. You are working from the diff, task
contract, and verification evidence alone.

ANTI-TRUST RULES:
- Treat all code with maximum scrutiny regardless of who or what wrote it.
- Do not assume the implementer's intent was correct. Verify against the task contract.
- Do not assume passing tests mean correctness. Tests may be insufficient or wrong.
- If everything looks clean, STOP and re-examine: security, error handling, edge
  cases, concurrency, resource leaks, injection vectors, boundary validation.
- "The code looks correct" is not evidence. "Lines 42-58 validate X as required
  by criterion 3" is evidence.

REVIEW PROCESS:
Stage 1 — Spec Compliance:
For each acceptance criterion, determine: satisfied / not satisfied / partially
satisfied. Cite specific diff lines as evidence. Flag anything missing AND anything
extra (YAGNI).

Stage 2 — Code Quality:
Start with the strongest plausible failure mode, not the easiest nitpick.
Check: correctness, security, error handling, testing adequacy, architecture.
Grade findings: critical (blocks merge), important (should fix), minor (suggestion).
Every finding must include file:line and a failure scenario.
If a finding is verifiable by command, provide the command.

OUTPUT:
Respond with a JSON object matching the ReviewFindings schema.
Your confidence score must reflect YOUR actual certainty, not the code's quality.
A perfect-looking diff you haven't deeply examined warrants low confidence.
High confidence means you have verified specific behaviors, not just read the code.
```

### Challenge round — executor response prompt

```
A code reviewer has examined your implementation and produced findings.
Your job: fix every valid finding, then explain.

RULES:
1. REPAIR FIRST. Make all code changes before responding to findings.
2. For each finding, respond with one of:
   - concede_and_fixed: "I fixed this. Here's what changed: [specific description]"
   - already_correct: "This finding is incorrect because [specific evidence with
     file:line reference]"
3. Do NOT argue without evidence. "I believe my implementation is correct" is not
   a valid response. "Line 45 already handles this case via the guard clause at
   line 42" is valid.
4. Do NOT dismiss findings by restating your original intent. The reviewer doesn't
   care what you intended — they care what the code does.
5. If you concede a finding but the fix requires changing the approach significantly,
   explain the new approach and why it's better.

FORBIDDEN:
- "The reviewer raises a good point" (just fix it)
- "I respectfully disagree" (show evidence or concede)
- Dismissing any finding without specific counter-evidence
```

### Challenge round — final reviewer prompt

```
You previously reviewed this code and produced findings. The implementer has
responded with repairs and explanations.

Your job: evaluate the NEW diff against the original task contract.

RULES:
1. Review the NEW diff independently. Do not anchor on your previous findings.
2. For each original finding:
   - If fixed: mark resolved
   - If the implementer claims "already_correct": verify their evidence. If their
     evidence is sound, concede. If not, maintain the finding with explanation.
   - If the fix introduced new issues: report them as new findings.
3. Your confidence must reflect the current state, not your prior assessment.
4. A repair that addresses your findings but introduces new problems is still
   a reject.

OUTPUT:
Respond with a JSON object matching the ReviewFindings schema.
This is the FINAL verdict. Be thorough — there are no more rounds after this.
```

## Real-time Visibility

The terminal renderer should show both providers distinctly.

Suggested layout:

- current task and state
- Claude executor activity:
  - current tool/action summary
  - latest commentary line
  - elapsed time
- Codex reviewer activity:
  - current review stage (spec compliance / code quality)
  - latest commentary line
  - round number (1/2, with challenge indicator)
- verification status
- blocker reason if present

Artifacts should persist provider event logs under each attempt so a run can be replayed or debugged after failure.

## Human-in-the-loop Policy

The human is still not meant to babysit the loop.

The operator remains in control by:

- authoring the approved plan and config
- seeing real-time provider activity
- reviewing persisted prompts, patches, and review artifacts
- only being interrupted for true blockers or unresolved disagreements

The system should continue to be opinionated about not asking the human for routine test failures, parser retries, or first-pass review rejections.

## Resolved Open Questions

### Q1: `codex exec review --json` or `codex exec --json`?

**Answer: `codex exec --json` only.** Ralph is non-interactive and turn-based. `codex exec` is the right command — it gives us full control over the prompt and output schema. `codex exec review` is not used.

### Q2: Fold findings into EvaluatorVerdict or introduce richer schema?

**Answer: introduce ReviewFindings.** `EvaluatorVerdict` is too thin (`decision`, `repair_notes`, `new_verification_commands`, `summary`). Trying to shoehorn severity-graded, evidence-backed findings into that shape creates parsing hacks. `ReviewFindings` is the new schema for the adversarial reviewer path. `EvaluatorVerdict` remains for backward compat with the Claude-only evaluator.

### Q3: Daemon-lite mode in this feature?

**Answer: no.** Keep the first version as foreground CLI plus richer provider events. The event model is designed to support a future daemon/SSE mode, but building it now is premature.

## Acceptance Criteria

1. `ralph` can run with Claude as executor and Codex as reviewer in the same task loop.
2. Provider activity from both CLIs is visible in real time during `start` and `resume`.
3. Codex review is read-only by policy and does not become a second repository writer.
4. The first Codex review pass is independent of Claude's rationale.
5. Review performs two-stage evaluation (spec compliance, then code quality).
6. The review system supports one optional bounded challenge round, policy-driven.
7. Challenge round uses repair-first-then-explain with structured `FindingResponse` tracking.
8. Risk classification is computed from the diff, not self-reported.
9. Challenge trigger thresholds are configurable with evidence-backed defaults.
10. Final commit still requires passing effective verification and a final `accept` verdict.
11. Provider event logs are persisted per attempt with `review_round` disambiguation.
12. Resume preserves enough provider state to continue safely, or blocks with a clear reason.
13. Mixed-provider integration has unit tests with fake transports and at least one real CLI smoke path.
14. Adapters depend on `Transport` ABC — no direct subprocess calls in adapter code.
15. Adding a new transport type (e.g., OpenAI API) requires only a new `Transport` implementation and config, no adapter or orchestrator changes.
16. Machine-checkable reviewer findings are automatically validated via ratchet pipeline.

## References

- Crossfire:
  - `/home/jenkins/projects/crossfire/packages/adapters/src/base/provider-adapter.ts`
  - `/home/jenkins/projects/crossfire/packages/adapters/src/codex/codex-cli-transport.ts`
  - `/home/jenkins/projects/crossfire/packages/adapters/src/claude/claude-cli-process.ts`
  - `/home/jenkins/projects/crossfire/apps/daemon/src/services/progress.ts`
  - `/home/jenkins/projects/crossfire/packages/adapters/src/prompts/structured-turn.ts`
- Superpowers:
  - `/home/jenkins/superpowers/skills/subagent-driven-development/spec-reviewer-prompt.md`
  - `/home/jenkins/superpowers/skills/subagent-driven-development/code-quality-reviewer-prompt.md`
  - `/home/jenkins/superpowers/skills/receiving-code-review/SKILL.md`
  - `/home/jenkins/superpowers/skills/verification-before-completion/SKILL.md`
- Research:
  - LLMs show systematic overcorrection bias in code review (arxiv.org/html/2603.00539)
  - AI-generated code has 1.7x more issues, 2.7x more security issues (CodeRabbit 2025 report)
  - LLMs are overconfident in 84% of scenarios (JMIR 2025)
  - Meta's Diff Risk Score: top 30% risk tier captures 86.4% of defect-inducing commits
  - Iterative review-fix: rounds 1-2 capture 75% of total defect improvement (Yang et al. EMNLP 2025)
  - DORA: elite teams maintain <15% change failure rate
- OpenAI Codex docs:
  - `codex exec --json`
  - GPT-5.3-Codex prompting and phase guidance
