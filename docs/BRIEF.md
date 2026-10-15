# ralph-ng — project brief

<!-- Canonical cross-project registry source (2026-07-09 plan §4.2).
     The KEY: value lines above the --- are lifted verbatim into the
     generated ~/projects/CLAUDE.md by projects-registry-gen. Keep each
     on ONE line, compact. Update when purpose/provides/claims change
     (retro-skill checklist). Body below --- is free-form. -->

PURPOSE: Autonomous coding orchestrator: executes a validated ralph-plan.yaml task-by-task with Claude as executor and Codex as independent adversarial reviewer, bounded challenge/repair loops, crash-safe state under .git/ralph/, Textual dashboard.
PROVIDES: autonomous plan execution for any repo with ralph.toml; companion skills (init-ralph, plan-converter, plan-repair, run-triage).
CONSUMES: claude + codex CLIs; pattern lineage from crossfire and lectureforge.
CONFIDENTIALITY: personal.
HOOKS: consumers today: ragnaroky-NG, lectureforge, callm, wealthos, bookfactory; three companion skills sit in ~/.claude/skills.disabled while Codex routing still references them (parity decision open); stray venv in repo root.

---

## Audit snapshot (2026-07-09, frozen)

STATUS: active — git repo, last commit 2026-05-09 ("config: add disable_plugins_and_slash_commands switch"); steady commits Mar–May 2026
RAGNAROK: none — no RAGNAROK references found in the repo
BRIEF: ralph-ng (/home/jenkins/projects/AI-WORKFLOWS/ralph-ng) is the developer's autonomous coding orchestrator: a Python CLI ("ralph") that executes a validated ralph-plan.yaml task-by-task with Claude as executor, Codex as independent adversarial reviewer, bounded challenge/repair loops, local verification, crash-safe state under .git/ralph/, and a Textual dashboard. It is the successor to jfmaes/awesome-ai-workflow, deliberately reuses Crossfire's review patterns without its runtime, and borrows its codex subprocess invocation from lectureforge. It ships companion agent skills (init-ralph, plan-converter, plan-repair, run-triage) that other repos use to onboard onto ralph runs — any sibling project containing a ralph.toml is a consumer. Active as of 2026-05-09; released repo-first (no PyPI). Main hygiene issues: a venv accidentally created in the repo root and no in-repo CLAUDE.md/AGENTS.md.
PAIN: Python venv created in the repo root (untracked bin/, lib64, pyvenv.cfg per git status) instead of .venv/ as the README instructs — pollutes the working tree and risks accidental commits | No CLAUDE.md/AGENTS.md in the repo itself, ironic for the workflow-orchestrator project; agents landing here get no repo-specific guidance | Skill duplication risk: skills/init-ralph in-repo vs the globally installed init-ralph skill can drift | lectureforge coupling is a fragile cross-repo reference living only in a code comment (cli_codex_transport.py:553)
CONNECTIONS: docs/plans/2026-03-20-claude-executor-codex-reviewer-spec.md:52,84 — explicitly scopes against sibling 'Crossfire' (reuses its patterns, not its runtime; multi-turn consensus out of scope) ;; src/ralph_ng/adapters/cli_codex_transport.py:553 — code comment citing lectureforge's scripts/ralph-codex-bare.sh as the reference for the codex subprocess invocation pattern ;; README.md:11 / DESIGN.md:5 / spec.md:7 — lineage from jfmaes/awesome-ai-workflow (GitHub), which ralph-ng supersedes ;; Global init-ralph skill and ultimate-agentic-workflow skill reference ralph.toml/ralph-plan.yaml, making ralph-ng the shared autonomous-run engine other projects can adopt
