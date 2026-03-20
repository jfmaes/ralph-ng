"""SQLite-backed persistent state for ralph-ng runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class RunRecord:
    id: str
    plan_path: str
    status: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class TaskStateRecord:
    run_id: str
    task_id: str
    state: str
    attempt_count: int
    last_error: str | None
    updated_at: str


@dataclass(slots=True)
class AttemptRecord:
    id: int
    run_id: str
    task_id: str
    attempt_number: int
    patch_path: str | None
    prompt_path: str | None
    review_path: str | None
    created_at: str


@dataclass(slots=True)
class InvocationRecord:
    id: int
    run_id: str
    task_id: str
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    exit_status: int
    created_at: str


class StateStore:
    """Persist runs, task states, attempts, and model invocations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._initialize_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                plan_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_states (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, task_id),
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                patch_path TEXT,
                prompt_path TEXT,
                review_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL DEFAULT 0,
                exit_status INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            """
        )
        self.connection.commit()

    def create_run(
        self,
        plan_path: str,
        *,
        run_id: str,
        status: str = "created",
        task_ids: list[str] | None = None,
    ) -> RunRecord:
        timestamp = utc_now()
        self.connection.execute(
            """
            INSERT INTO runs (id, plan_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, plan_path, status, timestamp, timestamp),
        )
        if task_ids:
            self.initialize_tasks(run_id, task_ids)
        self.connection.commit()
        return RunRecord(
            id=run_id,
            plan_path=plan_path,
            status=status,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def initialize_tasks(self, run_id: str, task_ids: list[str], state: str = "pending") -> None:
        timestamp = utc_now()
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO task_states
            (run_id, task_id, state, attempt_count, last_error, updated_at)
            VALUES (?, ?, ?, 0, NULL, ?)
            """,
            [(run_id, task_id, state, timestamp) for task_id in task_ids],
        )
        self.connection.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.connection.execute(
            "SELECT id, plan_path, status, created_at, updated_at FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(**dict(row))

    def update_run_status(self, run_id: str, status: str) -> RunRecord:
        timestamp = utc_now()
        self.connection.execute(
            """
            UPDATE runs
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, timestamp, run_id),
        )
        self.connection.commit()
        record = self.get_run(run_id)
        if record is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        return record

    def get_task_state(self, run_id: str, task_id: str) -> TaskStateRecord | None:
        row = self.connection.execute(
            """
            SELECT run_id, task_id, state, attempt_count, last_error, updated_at
            FROM task_states
            WHERE run_id = ? AND task_id = ?
            """,
            (run_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return TaskStateRecord(**dict(row))

    def list_task_states(self, run_id: str) -> dict[str, TaskStateRecord]:
        rows = self.connection.execute(
            """
            SELECT run_id, task_id, state, attempt_count, last_error, updated_at
            FROM task_states
            WHERE run_id = ?
            ORDER BY task_id
            """,
            (run_id,),
        ).fetchall()
        return {row["task_id"]: TaskStateRecord(**dict(row)) for row in rows}

    def transition_task(
        self,
        run_id: str,
        task_id: str,
        state: str,
        *,
        attempt_count: int | None = None,
        last_error: str | None = None,
    ) -> TaskStateRecord:
        existing = self.get_task_state(run_id, task_id)
        timestamp = utc_now()
        if existing is None:
            resolved_attempt_count = 0 if attempt_count is None else attempt_count
            self.connection.execute(
                """
                INSERT INTO task_states
                (run_id, task_id, state, attempt_count, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, state, resolved_attempt_count, last_error, timestamp),
            )
        else:
            resolved_attempt_count = (
                existing.attempt_count if attempt_count is None else attempt_count
            )
            self.connection.execute(
                """
                UPDATE task_states
                SET state = ?, attempt_count = ?, last_error = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (
                    state,
                    resolved_attempt_count,
                    last_error,
                    timestamp,
                    run_id,
                    task_id,
                ),
            )
        self.connection.execute(
            "UPDATE runs SET updated_at = ? WHERE id = ?",
            (timestamp, run_id),
        )
        self.connection.commit()
        record = self.get_task_state(run_id, task_id)
        assert record is not None
        return record

    def record_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt_number: int,
        *,
        patch_path: str | None = None,
        prompt_path: str | None = None,
        review_path: str | None = None,
    ) -> AttemptRecord:
        timestamp = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO attempts
            (run_id, task_id, attempt_number, patch_path, prompt_path, review_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, attempt_number, patch_path, prompt_path, review_path, timestamp),
        )
        self.connection.execute(
            "UPDATE runs SET updated_at = ? WHERE id = ?",
            (timestamp, run_id),
        )
        self.connection.commit()
        return AttemptRecord(
            id=cursor.lastrowid,
            run_id=run_id,
            task_id=task_id,
            attempt_number=attempt_number,
            patch_path=patch_path,
            prompt_path=prompt_path,
            review_path=review_path,
            created_at=timestamp,
        )

    def record_invocation(
        self,
        run_id: str,
        task_id: str,
        *,
        role: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_seconds: float,
        exit_status: int,
    ) -> InvocationRecord:
        timestamp = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO invocations
            (run_id, task_id, role, model, input_tokens, output_tokens, duration_seconds, exit_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                role,
                model,
                input_tokens,
                output_tokens,
                duration_seconds,
                exit_status,
                timestamp,
            ),
        )
        self.connection.execute(
            "UPDATE runs SET updated_at = ? WHERE id = ?",
            (timestamp, run_id),
        )
        self.connection.commit()
        return InvocationRecord(
            id=cursor.lastrowid,
            run_id=run_id,
            task_id=task_id,
            role=role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration_seconds,
            exit_status=exit_status,
            created_at=timestamp,
        )

    def get_latest_attempt(self, run_id: str, task_id: str) -> AttemptRecord | None:
        row = self.connection.execute(
            """
            SELECT id, run_id, task_id, attempt_number, patch_path, prompt_path, review_path, created_at
            FROM attempts
            WHERE run_id = ? AND task_id = ?
            ORDER BY attempt_number DESC, id DESC
            LIMIT 1
            """,
            (run_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return AttemptRecord(**dict(row))

    def update_attempt_artifacts(
        self,
        attempt_id: int,
        *,
        patch_path: str | None = None,
        prompt_path: str | None = None,
        review_path: str | None = None,
    ) -> AttemptRecord:
        row = self.connection.execute(
            """
            SELECT id, run_id, task_id, attempt_number, patch_path, prompt_path, review_path, created_at
            FROM attempts
            WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown attempt_id: {attempt_id}")

        current = AttemptRecord(**dict(row))
        resolved_patch_path = current.patch_path if patch_path is None else patch_path
        resolved_prompt_path = current.prompt_path if prompt_path is None else prompt_path
        resolved_review_path = current.review_path if review_path is None else review_path
        self.connection.execute(
            """
            UPDATE attempts
            SET patch_path = ?, prompt_path = ?, review_path = ?
            WHERE id = ?
            """,
            (resolved_patch_path, resolved_prompt_path, resolved_review_path, attempt_id),
        )
        self.connection.commit()
        return AttemptRecord(
            id=current.id,
            run_id=current.run_id,
            task_id=current.task_id,
            attempt_number=current.attempt_number,
            patch_path=resolved_patch_path,
            prompt_path=resolved_prompt_path,
            review_path=resolved_review_path,
            created_at=current.created_at,
        )

    def get_run_summary(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")

        task_states = self.connection.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM task_states
            WHERE run_id = ?
            GROUP BY state
            """,
            (run_id,),
        ).fetchall()
        attempts = self.connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        invocation_totals = self.connection.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(duration_seconds), 0) AS duration_seconds
            FROM invocations
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        return {
            "run": run,
            "task_states": {row["state"]: row["count"] for row in task_states},
            "attempt_count": attempts["count"],
            "invocations": dict(invocation_totals),
        }
