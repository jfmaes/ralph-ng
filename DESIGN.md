# ralph-ng: Design Document

## Problem Statement

Autonomous AI coding loops like [ralph](https://github.com/snarktank/ralph) have proven that a simple bash script piping prompts into an AI CLI can turn a list of requirements into committed, tested code with zero human involvement. The [awesome-ai-workflow](https://github.com/jfmaes/awesome-ai-workflow/tree/v1.1) extended this with tiered complexity, specs, planning, reflection, multi-CLI support, and observability — but both approaches share fundamental limitations:

1. **Single-agent, single-model, sequential execution.** One AI works on one task at a time. There is no way for a reviewer to examine code while a builder is working on the next task, or for two models to discuss a design decision before implementation begins.

2. **No cross-model quality pressure.** Same-model review is like proofreading your own essay. Both ralph and awesome-ai-workflow run the same model for building and (optionally) reviewing. Systematic blind spots of a model are never challenged.

3. **No real agent-to-agent communication.** Context passes between iterations through flat files (progress.txt, prd.json, AGENTS.md). This is serial monologue, not discussion. Agents cannot negotiate, disagree, or converge on a decision.

4. **Existing multi-agent frameworks don't fit.** CrewAI, AutoGen, and OpenAI Swarm all assume programmatic API access with structured function calling. They cannot leverage CLI subscriptions (Claude Code, Codex CLI) which offer effectively unlimited usage at flat rate. These frameworks also treat the model as a stateless function, while CLI tools are stateful interactive sessions.

5. **Poor observability.** You start ralph, alt-tab away, and come back hoping it worked. There is no live view of what's happening, which agent is doing what, or where things went wrong.

6. **Binary human involvement.** Either you're fully hands-off (and hope for the best) or you're babysitting. There's no structured protocol for "interrupt me only when it actually matters."

## What ralph-ng Must Be

An orchestrator that runs multiple AI coding agents (Claude Code, Codex, and future CLIs) concurrently, enables structured discussions between them, assigns different models to different roles based on their strengths, and only involves the human when genuine input is required — all while providing real-time visibility into every agent's activity.

## Core Design Decisions

### 1. CLI-First, Not API-First

**Decision:** ralph-ng orchestrates CLI tools (`claude`, `codex`) as subprocesses, not API calls.

**Rationale:** The user pays for CLI subscriptions at flat rate. API-based frameworks (CrewAI, AutoGen, Swarm) require per-token billing and cannot use these subscriptions. CLI tools also come with batteries included — file access, shell execution, git integration, MCP servers — that would need to be reimplemented from scratch with raw API access.

**Trade-off:** We lose structured `tool_calls` output and fine-grained control over generation. We gain the full power of Claude Code and Codex as autonomous coding agents, not just language models.

**How we compensate:** Both CLIs support streaming JSON output (`claude --output-format stream-json`, `codex` event streams). We parse these streams in real-time. We use structured prompts with sentinel markers to extract decisions, questions, and state changes from agent output. We don't need function-calling control because each agent is a fully autonomous coding agent — we orchestrate at the task level, not the token level.

### 2. Mediated Discussion, Not Direct Messaging

**Decision:** Agents don't talk to each other directly. The orchestrator mediates all communication.

**Rationale:** CLI tools are opaque processes. You cannot inject a message into a running Claude Code session from outside. But you CAN:
- Start a new agent session with context that includes another agent's output
- Run two agents concurrently and route relevant outputs between them
- Structure "discussion rounds" where each agent responds to the other's position

This is actually how human code review works — the reviewer doesn't interrupt the developer mid-keystroke. They review a completed unit of work and provide structured feedback.

**The discussion protocol:**

```
Orchestrator spawns Agent A (e.g., Opus) with: problem statement + "propose an approach"
Orchestrator spawns Agent B (e.g., GPT 5.4) with: problem statement + "propose an approach"
    ↓ both complete concurrently
Orchestrator spawns Agent A with: "Agent B proposed X. Your proposal was Y. Identify risks in both."
Orchestrator spawns Agent B with: "Agent A proposed Y. Your proposal was X. Identify risks in both."
    ↓ both complete concurrently
Orchestrator synthesizes: compare critiques, identify consensus and disagreements
    ↓
If consensus → proceed with agreed approach
If disagreement on critical point → one more round with focused question
If deadlock after N rounds → escalate to human with both positions summarized
```

This gives us genuine multi-model deliberation without needing direct inter-process communication. Each "round" is a fresh CLI invocation with the accumulated discussion as context.

### 3. The Election: Model Self-Assessment for Role Assignment

**Decision:** Before any work begins, both frontier models analyze the problem and self-assess their suitability to drive vs. review.

**Rationale:** Opus and GPT 5.4 are both frontier reasoning models, but they have different strengths. Rather than hard-coding "Opus plans, Codex builds," let the models themselves reason about who's better suited for a given project.

**The election protocol:**

```
Phase 1: Self-Assessment
  Both models receive the same prompt:
    - Problem statement / PRD
    - Tech stack involved
    - "Analyze this project. Assess your strengths and weaknesses for:
       (a) driving implementation, (b) reviewing implementation.
       Be honest about your limitations. Output structured JSON."

Phase 2: Cross-Assessment (optional, for high-stakes projects)
  Each model receives the other's self-assessment:
    - "Do you agree with this assessment? Where do you think
       the other model is overconfident or underconfident?"

Phase 3: Assignment
  The orchestrator compares structured assessments and assigns:
    - Driver: the model with higher implementation confidence AND
      lower reviewer disagreement
    - Reviewer: the other model
    - If assessments are too close to call: use the cheaper/faster
      model as driver (more iterations), frontier model as reviewer
      (fewer but deeper reviews)
```

**Why this matters:** A Python-heavy backend project might favor one model, while a React frontend with complex state management might favor another. The models know their own training data distributions better than any hard-coded heuristic.

### 4. Concurrent Agents with a Message Bus

**Decision:** Multiple agents run simultaneously, coordinated through an event-driven message bus.

**Rationale:** Sequential execution wastes time. While the builder implements US-003, the reviewer should be reviewing US-002, and the scorer should be scoring US-001. This is a pipeline, not a serial loop.

**Implementation:** SQLite-backed event store (no external dependencies). Events are typed:

| Event | Producer | Consumer | Purpose |
|-------|----------|----------|---------|
| `task.assigned` | Orchestrator | Builder | Start work on a task |
| `task.completed` | Builder | Reviewer | Work ready for review |
| `review.verdict` | Reviewer | Orchestrator | Approved / needs-changes / rejected |
| `review.feedback` | Reviewer | Builder | Specific changes requested |
| `score.result` | Scorer | Orchestrator | Quality scores + risk assessment |
| `push.approved` | Orchestrator | Git worker | Safe to push |
| `human.needed` | Any agent | Dashboard | Escalation with context |
| `discussion.round` | Orchestrator | Agents | Discussion prompt for deliberation |
| `election.vote` | Agent | Orchestrator | Self-assessment during election |

The bus is also the observability layer — the dashboard subscribes to all events and renders them in real time.

### 5. Scoring Gates Before Push

**Decision:** No code reaches the remote without passing automated checks, cross-model review, and structured scoring.

**Rationale:** ralph pushes on commit. awesome-ai-workflow pushes per iteration. Neither validates quality beyond "did the tests pass." ralph-ng introduces a multi-stage gate:

```
Builder completes task
    ↓
Gate 1: Automated (lint, typecheck, tests) — fast, free, deterministic
    ↓ pass
Gate 2: Cross-model review — different model than builder examines the diff
    ↓ approve (or loop back to builder with feedback, max N rounds)
Gate 3: Structured scoring against acceptance criteria
    ↓ composite score ≥ threshold AND risk ≠ high
Push to remote
```

**Scoring dimensions:**
- **Spec compliance** (0-10): Does it satisfy acceptance criteria from the spec?
- **Code quality** (0-10): Clean, idiomatic, no obvious issues?
- **Test coverage** (0-10): Are edge cases from the spec tested?
- **Risk** (low/medium/high): Could this break existing functionality?

**Thresholds are configurable.** Aggressive teams can set score ≥ 6; cautious teams can require ≥ 8. Risk = high always escalates to human unless overridden in config.

### 6. Structured Human Escalation

**Decision:** Codify exactly when humans are interrupted, and never otherwise.

**Auto-resolve (no human):**
- Test failures → feed error context back to builder, retry
- Review rejection → feed review feedback back to builder, retry
- Low score → feed scoring feedback back to builder, retry
- Rate limiting → exponential backoff and retry
- Merge conflicts → attempt auto-resolution, retry

**Escalate to human:**
- `OPEN_QUESTION:` markers in specs that agents cannot resolve
- Builder has failed the same task N times (default: 3)
- Reviewer and builder deadlock after M review rounds (default: 3)
- Risk assessment is "high" on code touching critical paths
- Token/cost budget exceeded for a single task
- Security-sensitive operation detected (secrets in code, permission changes)

**When escalating:** ralph-ng pauses ONLY that task, continues working on other independent tasks, and presents the human with:
1. What the question/problem is (one sentence)
2. What the agents tried (summary)
3. The agents' positions if they disagree
4. Suggested options to choose from

### 7. Real-Time Dashboard

**Decision:** A TUI that subscribes to the message bus and shows live agent activity.

```
ralph-ng status

╔═══════════════════════════════════════════════════════════╗
║  ralph-ng: feature/auth-middleware         iter: 3/10     ║
║  driver: opus (elected) │ reviewer: gpt-5.4              ║
╠═══════════════════════════════════════════════════════════╣
║                                                           ║
║  US-001 [✓ pushed]  Setup auth middleware                 ║
║    score: 8.7 │ review: approved (1 round) │ risk: low   ║
║                                                           ║
║  US-002 [⟳ review]  Add JWT validation                   ║
║    builder: done │ reviewer: examining diff...            ║
║    gate: ░░░░░░░░░░ lint ✓ test ✓ review ⟳ score ·       ║
║                                                           ║
║  US-003 [⟳ build]   Add role-based access                ║
║    builder (opus): editing src/middleware/rbac.ts          ║
║                                                           ║
║  US-004 [· queued]   Add audit logging                    ║
║                                                           ║
╠═══════════════════════════════════════════════════════════╣
║  tokens: 84K this iter │ 312K total │ est. $4.20          ║
║  bus: 47 events │ 0 human-needed │ 0 deadlocks            ║
╚═══════════════════════════════════════════════════════════╝
```

## Why Existing Frameworks Can't Do This

| Constraint | CrewAI / AutoGen / Swarm | ralph-ng |
|-----------|--------------------------|----------|
| Model interface | API with structured function calling | CLI subprocesses with streaming JSON |
| Billing | Per-token API costs | Flat-rate CLI subscriptions |
| Agent capabilities | Raw LLM + custom tools | Full coding agents (file access, shell, git, MCP) |
| Context per agent | Shared/chained message history | Fresh context per invocation (by design — avoids drift) |
| Communication | Direct messaging or shared history | Orchestrator-mediated with structured events |
| Concurrency | Mostly sequential (except AutoGen 0.4 actors) | Pipeline parallelism (build/review/score overlap) |

The fundamental mismatch: existing frameworks treat models as **functions** (input → structured output → parse → loop). ralph-ng treats them as **autonomous agents** that happen to communicate through an orchestrator. We don't need token-level control because each agent is a fully capable coding assistant — we coordinate at the task level.

## Architecture Overview

```
                         ┌──────────────┐
                         │   Human      │
                         │  (only when  │
                         │   needed)    │
                         └──────┬───────┘
                                │ escalations only
                         ┌──────▼───────┐
                    ┌────│ Orchestrator  │────┐
                    │    │  (Python,     │    │
                    │    │   asyncio)    │    │
                    │    └──────┬───────┘    │
                    │           │             │
              ┌─────▼──┐  ┌────▼────┐  ┌────▼─────┐
              │Builder  │  │Reviewer │  │ Scorer   │
              │(claude  │  │(codex   │  │(sonnet   │
              │ or      │  │ or      │  │ or       │
              │ codex)  │  │ claude) │  │ haiku)   │
              └────┬────┘  └────┬────┘  └────┬─────┘
                   │            │             │
              ┌────▼────────────▼─────────────▼────┐
              │         Message Bus (SQLite)        │
              │  events · state · discussion logs   │
              └────────────────┬───────────────────┘
                               │
                        ┌──────▼───────┐
                        │  Dashboard   │
                        │  (TUI)       │
                        └──────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12+ | asyncio for concurrency, rich ecosystem for TUI/SQLite, both CLIs have Python-friendly output |
| Message bus | SQLite + WAL mode | Zero dependencies, concurrent reads, single-file persistence, survives crashes |
| TUI | Textual | Modern Python TUI framework, async-native, rich widget library |
| CLI integration | asyncio subprocesses | Stream stdout/stderr in real-time, parse JSON events, manage lifecycle |
| Config | TOML | Human-readable, good Python stdlib support (tomllib) |
| Task format | Compatible with prd.json | Migration path from existing ralph setups |

## Open Questions

- **Session reuse vs. fresh invocation:** Claude Code supports `--resume`. Should the builder maintain a session across review feedback rounds (preserves context, risks drift) or start fresh each time (clean context, costs more tokens)?
- **Parallel task execution:** Should two builders work on independent tasks simultaneously, or does one builder pipeline with one reviewer give better results?
- **Election frequency:** Once per project? Once per major phase? Should roles be re-evaluated if a model is consistently underperforming?
- **Cost tracking:** CLI subscriptions are flat-rate but rate-limited. Should ralph-ng track and optimize for rate limit headroom rather than token cost?
- **Plugin/MCP integration:** Both Claude Code and Codex support tool extensions. Should ralph-ng expose the message bus as an MCP server so agents can explicitly publish events?
