"""Tests for reliability hardening changes across multiple modules.

Covers: SelfHealingConfig, RunStatus.PAUSED, StateStore controller/heartbeat/
phase_checkpoint/task_failure/recovery_attempts, prompt single-output rules,
prompt dependency management, and orchestrator shutdown event.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

from ralph_ng.config import RalphConfig, SelfHealingConfig, load_config
from ralph_ng.orchestrator import RunStatus
from ralph_ng.prompt_builder import PromptBuilder
from ralph_ng.prompts.challenge_executor import build_challenge_executor_prompt
from ralph_ng.prompts.challenge_reviewer import build_challenge_reviewer_prompt
from ralph_ng.prompts.reviewer_first_pass import build_first_pass_prompt
from ralph_ng.state import StateStore


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestSelfHealingConfig:
    def test_self_healing_config_defaults(self):
        cfg = SelfHealingConfig()
        assert cfg.enabled is True
        assert cfg.diagnosis_model == "opus"

    def test_self_healing_config_custom(self):
        cfg = SelfHealingConfig(enabled=False, diagnosis_model="sonnet")
        assert cfg.enabled is False
        assert cfg.diagnosis_model == "sonnet"

    def test_ralph_config_has_self_healing(self):
        rc = RalphConfig()
        assert isinstance(rc.self_healing, SelfHealingConfig)

    def test_self_healing_config_from_toml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                '[ralph]\nbranch_prefix = "r/"\n\n'
                '[self_healing]\nenabled = false\n'
                'diagnosis_model = "custom-model"\n'
            )
            f.flush()
            tmp_path = Path(f.name)

        try:
            config = load_config(tmp_path)
            assert config.self_healing.enabled is False
            assert config.self_healing.diagnosis_model == "custom-model"
        finally:
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# RunStatus tests
# ---------------------------------------------------------------------------


class TestRunStatus:
    def test_paused_status_exists(self):
        assert RunStatus.PAUSED == "paused"


# ---------------------------------------------------------------------------
# StateStore tests
# ---------------------------------------------------------------------------


class TestStateStoreController:
    def test_acquire_and_release_controller(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            store.acquire_controller("run-1", os.getpid())
            row = store.connection.execute(
                "SELECT controller_pid FROM runs WHERE id = 'run-1'"
            ).fetchone()
            assert row["controller_pid"] == os.getpid()

            store.release_controller("run-1")
            row = store.connection.execute(
                "SELECT controller_pid FROM runs WHERE id = 'run-1'"
            ).fetchone()
            assert row["controller_pid"] is None

    def test_update_heartbeat(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            store.acquire_controller("run-1", os.getpid())

            row_before = store.connection.execute(
                "SELECT last_heartbeat_at FROM runs WHERE id = 'run-1'"
            ).fetchone()
            hb_before = row_before["last_heartbeat_at"]

            store.update_heartbeat("run-1")

            row_after = store.connection.execute(
                "SELECT last_heartbeat_at FROM runs WHERE id = 'run-1'"
            ).fetchone()
            hb_after = row_after["last_heartbeat_at"]

            # Heartbeat timestamp should have been set (possibly same if very
            # fast, but must not be None).
            assert hb_after is not None
            assert hb_after >= hb_before

    def test_update_phase_checkpoint(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            store.update_phase_checkpoint("run-1", "execute:T1:attempt:2")

            row = store.connection.execute(
                "SELECT phase_checkpoint FROM runs WHERE id = 'run-1'"
            ).fetchone()
            assert row["phase_checkpoint"] == "execute:T1:attempt:2"

    def test_update_task_failure(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            store.update_task_failure("run-1", "T1", "missing_dependency")

            row = store.connection.execute(
                "SELECT failure_class FROM task_states WHERE run_id = 'run-1' AND task_id = 'T1'"
            ).fetchone()
            assert row["failure_class"] == "missing_dependency"

    def test_is_controller_alive_dead_pid(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            # PID 999999 is very unlikely to be a running process
            store.acquire_controller("run-1", 999999)
            assert store.is_controller_alive("run-1") is False

    def test_is_controller_alive_no_controller(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            assert store.is_controller_alive("run-1") is False

    def test_recovery_attempts_table_exists(self, tmp_path):
        with StateStore(tmp_path / "state.db") as store:
            store.create_run("plan.yaml", run_id="run-1", task_ids=["T1"])
            # Verify the table exists by inserting a row directly
            from ralph_ng.state import utc_now

            store.connection.execute(
                """
                INSERT INTO recovery_attempts
                (run_id, task_id, attempt_number, failure_class, recovery_kind, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("run-1", "T1", 1, "missing_dependency", "auto_fix", "success", utc_now()),
            )
            store.connection.commit()

            row = store.connection.execute(
                "SELECT COUNT(*) AS cnt FROM recovery_attempts WHERE run_id = 'run-1'"
            ).fetchone()
            assert row["cnt"] == 1


# ---------------------------------------------------------------------------
# Prompt tests
# ---------------------------------------------------------------------------


class TestPromptHardening:
    def test_reviewer_prompt_has_single_output_rule(self):
        prompt = build_first_pass_prompt(
            task_id="T1",
            task_title="Test",
            task_description="Desc",
            acceptance_criteria=["AC1"],
            git_diff="",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "EXACTLY ONE JSON object" in prompt

    def test_challenge_reviewer_has_single_output_rule(self):
        prompt = build_challenge_reviewer_prompt(
            task_id="T1",
            task_title="Test",
            task_description="Desc",
            acceptance_criteria=["AC1"],
            original_findings=[],
            executor_responses=[],
            new_git_diff="",
            changed_file_snippets={},
            verification_results=[],
        )
        assert "EXACTLY ONE JSON object" in prompt

    def test_challenge_executor_has_environment_fixes(self):
        prompt = build_challenge_executor_prompt(
            task_id="T1",
            task_title="Test",
            findings=[],
            reviewer_summary="All good",
        )
        assert "missing project dependency" in prompt.lower()

    def test_executor_prompt_has_dependency_management(self):
        # PromptBuilder embeds the _EXECUTOR_SYSTEM template which contains
        # the DEPENDENCY MANAGEMENT section.  We can verify it by importing
        # the module-level constant.
        from ralph_ng.prompt_builder import _EXECUTOR_SYSTEM

        assert "DEPENDENCY MANAGEMENT" in _EXECUTOR_SYSTEM


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------


class TestOrchestratorShutdown:
    def test_orchestrator_has_shutdown_event(self):
        """Verify that _shutdown_requested is a threading.Event on the class."""
        # We cannot easily instantiate Orchestrator without all its deps, so
        # just confirm the attribute is set properly by inspecting the
        # constructor source via a minimal mock approach.
        from ralph_ng.orchestrator import Orchestrator

        # Orchestrator.__init__ sets self._shutdown_requested = threading.Event()
        # Verify it's in the constructor body by checking the attribute exists
        # after construction with mocked dependencies.
        from unittest.mock import MagicMock

        orch = Orchestrator(
            repo_root="/tmp",
            plan=MagicMock(),
            plan_path="/tmp/plan.yaml",
            config=RalphConfig(),
            state_root="/tmp/state",
            store=MagicMock(),
            events=MagicMock(),
            git_manager=MagicMock(),
            prompt_builder=MagicMock(),
            executor=MagicMock(),
            reviewer=MagicMock(),
            verification_runner=MagicMock(),
            review_bundle_builder=MagicMock(),
        )
        assert isinstance(orch._shutdown_requested, threading.Event)

    def test_request_shutdown_sets_event(self):
        from ralph_ng.orchestrator import Orchestrator
        from unittest.mock import MagicMock

        orch = Orchestrator(
            repo_root="/tmp",
            plan=MagicMock(),
            plan_path="/tmp/plan.yaml",
            config=RalphConfig(),
            state_root="/tmp/state",
            store=MagicMock(),
            events=MagicMock(),
            git_manager=MagicMock(),
            prompt_builder=MagicMock(),
            executor=MagicMock(),
            reviewer=MagicMock(),
            verification_runner=MagicMock(),
            review_bundle_builder=MagicMock(),
        )
        assert not orch._shutdown_requested.is_set()
        orch.request_shutdown()
        assert orch._shutdown_requested.is_set()
