"""Database repository and persistence engine for OFFERHUNT (Milestone M2).

Implements strict parameterized SQL, explicit transactional controls with rollback,
connection lifecycle management with foreign key enforcement, and UTC ISO-8601 timestamps.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, List, Optional, Union


DEFAULT_DB_PATH = Path("artifacts/offerhunt.db").resolve()
SCHEMA_FILE_PATH = Path(__file__).parent / "schema.sql"
CURRENT_SCHEMA_VERSION = 1


def get_utc_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def open_db_connection(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open an SQLite connection configured with foreign keys and WAL journal mode."""
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # Autocommit mode to allow explicit transaction management
    )
    conn.row_factory = sqlite3.Row

    # Enforce foreign keys and modern WAL mode
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def db_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Cursor, None, None]:
    """Context manager executing an explicit SQLite transaction with automatic rollback on error."""
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        yield cursor
        cursor.execute("COMMIT;")
    except Exception:
        cursor.execute("ROLLBACK;")
        raise
    finally:
        cursor.close()


def init_database(
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
    schema_path: Union[str, Path] = SCHEMA_FILE_PATH,
) -> None:
    """Initialize database tables, schema version, and indexes."""
    schema_sql = Path(schema_path).read_text(encoding="utf-8")
    conn = open_db_connection(db_path)
    try:
        # Execute schema definition
        conn.executescript(schema_sql)

        # Record schema version if not already recorded
        with db_transaction(conn) as cur:
            cur.execute(
                "SELECT version FROM schema_version WHERE version = ?;",
                (CURRENT_SCHEMA_VERSION,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    INSERT INTO schema_version (version, applied_at_utc, description)
                    VALUES (?, ?, ?);
                    """,
                    (
                        CURRENT_SCHEMA_VERSION,
                        get_utc_iso(),
                        "Initial section 15 schema setup",
                    ),
                )
    finally:
        conn.close()


def check_database_integrity(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> tuple[bool, List[str]]:
    """Run PRAGMA integrity_check on the database.

    Returns:
        (is_ok, results): is_ok is True if all integrity checks output 'ok'.
    """
    conn = open_db_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        rows = cursor.fetchall()
        results = [str(r[0]) for r in rows]
        is_ok = len(results) == 1 and results[0].lower() == "ok"
        return is_ok, results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Repository Functions (Parameterized SQL Only)
# ---------------------------------------------------------------------------

def create_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str = "INITIALIZING",
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a new run record."""
    now = get_utc_iso()
    meta_json = json.dumps(metadata) if metadata is not None else None
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO runs (run_id, status, started_at_utc, metadata)
            VALUES (?, ?, ?, ?);
            """,
            (run_id, status, now, meta_json),
        )
    return {
        "run_id": run_id,
        "status": status,
        "started_at_utc": now,
        "completed_at_utc": None,
        "metadata": metadata,
    }


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a run record by run_id."""
    cur = conn.cursor()
    cur.execute(
        "SELECT run_id, status, started_at_utc, completed_at_utc, metadata FROM runs WHERE run_id = ?;",
        (run_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "status": row["status"],
        "started_at_utc": row["started_at_utc"],
        "completed_at_utc": row["completed_at_utc"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
    }


def update_run_status(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    completed: bool = False,
) -> None:
    """Update status of a run."""
    now = get_utc_iso() if completed else None
    with db_transaction(conn) as cur:
        if completed:
            cur.execute(
                "UPDATE runs SET status = ?, completed_at_utc = ? WHERE run_id = ?;",
                (status, now, run_id),
            )
        else:
            cur.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?;",
                (status, run_id),
            )


def create_step(
    conn: sqlite3.Connection,
    step_id: str,
    run_id: str,
    step_order: int,
    state: str = "READY",
    execution_status: str = "NOT_STARTED",
    verification_status: str = "UNVERIFIED",
    input_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a new step for a run."""
    now = get_utc_iso()
    inp_json = json.dumps(input_payload) if input_payload is not None else None
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO steps (
                step_id, run_id, step_order, state, execution_status,
                verification_status, input_payload, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                step_id,
                run_id,
                step_order,
                state,
                execution_status,
                verification_status,
                inp_json,
                now,
                now,
            ),
        )
    return {
        "step_id": step_id,
        "run_id": run_id,
        "step_order": step_order,
        "state": state,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "created_at_utc": now,
        "updated_at_utc": now,
    }


def get_step(conn: sqlite3.Connection, step_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a step by step_id."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT step_id, run_id, step_order, state, execution_status,
               verification_status, input_payload, output_payload,
               error_payload, created_at_utc, updated_at_utc
        FROM steps WHERE step_id = ?;
        """,
        (step_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def update_step_state(
    conn: sqlite3.Connection,
    step_id: str,
    state: str,
    execution_status: Optional[str] = None,
    verification_status: Optional[str] = None,
    output_payload: Optional[dict[str, Any]] = None,
    error_payload: Optional[dict[str, Any]] = None,
) -> None:
    """Update step state, execution status, and payloads."""
    now = get_utc_iso()
    out_json = json.dumps(output_payload) if output_payload is not None else None
    err_json = json.dumps(error_payload) if error_payload is not None else None

    with db_transaction(conn) as cur:
        # Dynamic update query using parameterized bindings
        clauses = ["state = ?", "updated_at_utc = ?"]
        params: List[Any] = [state, now]

        if execution_status is not None:
            clauses.append("execution_status = ?")
            params.append(execution_status)
        if verification_status is not None:
            clauses.append("verification_status = ?")
            params.append(verification_status)
        if out_json is not None:
            clauses.append("output_payload = ?")
            params.append(out_json)
        if err_json is not None:
            clauses.append("error_payload = ?")
            params.append(err_json)

        params.append(step_id)
        query = f"UPDATE steps SET {', '.join(clauses)} WHERE step_id = ?;"
        cur.execute(query, params)


def record_tool_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    step_id: str,
    tool_name: str,
    parameters: Optional[dict[str, Any]] = None,
    result: Optional[dict[str, Any]] = None,
    error: Optional[dict[str, Any]] = None,
    retry_count: int = 0,
    started_at_utc: Optional[str] = None,
    completed_at_utc: Optional[str] = None,
) -> None:
    """Record a tool attempt."""
    started = started_at_utc or get_utc_iso()
    completed = completed_at_utc or get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO tool_attempts (
                attempt_id, step_id, tool_name, parameters, result, error,
                retry_count, started_at_utc, completed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                attempt_id,
                step_id,
                tool_name,
                json.dumps(parameters) if parameters else None,
                json.dumps(result) if result else None,
                json.dumps(error) if error else None,
                retry_count,
                started,
                completed,
            ),
        )


def record_evidence(
    conn: sqlite3.Connection,
    evidence_id: str,
    step_id: str,
    evidence_type: str,
    content: Optional[str] = None,
    source_url: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> None:
    """Record evidence for a step."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO evidence (
                evidence_id, step_id, evidence_type, content, source_url, hash, captured_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (evidence_id, step_id, evidence_type, content, source_url, content_hash, now),
        )


def record_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    step_id: str,
    action_type: str,
    payload: Optional[dict[str, Any]] = None,
    status: str = "PENDING",
) -> None:
    """Record an approval request."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO approvals (
                approval_id, step_id, action_type, payload, status, requested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (approval_id, step_id, action_type, json.dumps(payload) if payload else None, status, now),
        )


def record_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    run_id: str,
    artifact_type: str,
    path: str,
    sha256: str,
    step_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Record an artifact."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, step_id, artifact_type, path, sha256, metadata, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                artifact_id,
                run_id,
                step_id,
                artifact_type,
                path,
                sha256,
                json.dumps(metadata) if metadata else None,
                now,
            ),
        )


def record_event(
    conn: sqlite3.Connection,
    event_id: str,
    run_id: str,
    event_type: str,
    severity: str = "INFO",
    step_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Record an audit or lifecycle event."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO events (
                event_id, run_id, step_id, event_type, severity, payload, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event_id,
                run_id,
                step_id,
                event_type,
                severity,
                json.dumps(payload) if payload else None,
                now,
            ),
        )


def record_metric(
    conn: sqlite3.Connection,
    metric_id: str,
    run_id: str,
    metric_name: str,
    metric_value: float,
) -> None:
    """Record a run metric."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            INSERT INTO metrics_runs (
                metric_id, run_id, metric_name, metric_value, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (metric_id, run_id, metric_name, metric_value, now),
        )


create_artifact = record_artifact
create_evidence = record_evidence


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> List[dict[str, Any]]:
    """List recent runs ordered by started_at_utc descending."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT run_id, status, started_at_utc, completed_at_utc, metadata
        FROM runs
        ORDER BY started_at_utc DESC
        LIMIT ?;
        """,
        (limit,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "run_id": r["run_id"],
            "status": r["status"],
            "started_at_utc": r["started_at_utc"],
            "completed_at_utc": r["completed_at_utc"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
        })
    return results


def get_steps_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all steps for a run ordered by step_order ascending."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT step_id, run_id, step_order, state, execution_status,
               verification_status, input_payload, output_payload,
               error_payload, created_at_utc, updated_at_utc
        FROM steps
        WHERE run_id = ?
        ORDER BY step_order ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "step_id": r["step_id"],
            "run_id": r["run_id"],
            "step_order": r["step_order"],
            "state": r["state"],
            "execution_status": r["execution_status"],
            "verification_status": r["verification_status"],
            "input_payload": json.loads(r["input_payload"]) if r["input_payload"] else {},
            "output_payload": json.loads(r["output_payload"]) if r["output_payload"] else {},
            "error_payload": json.loads(r["error_payload"]) if r["error_payload"] else None,
            "created_at_utc": r["created_at_utc"],
            "updated_at_utc": r["updated_at_utc"],
        })
    return results


def get_tool_attempts_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all tool attempts for steps in a run."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ta.attempt_id, ta.step_id, ta.tool_name, ta.parameters,
               ta.result, ta.error, ta.retry_count, ta.started_at_utc, ta.completed_at_utc
        FROM tool_attempts ta
        JOIN steps s ON s.step_id = ta.step_id
        WHERE s.run_id = ?
        ORDER BY ta.started_at_utc ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "attempt_id": r["attempt_id"],
            "step_id": r["step_id"],
            "tool_name": r["tool_name"],
            "parameters": json.loads(r["parameters"]) if r["parameters"] else {},
            "result": json.loads(r["result"]) if r["result"] else None,
            "error": json.loads(r["error"]) if r["error"] else None,
            "retry_count": r["retry_count"],
            "started_at_utc": r["started_at_utc"],
            "completed_at_utc": r["completed_at_utc"],
        })
    return results


def get_evidence_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all evidence recorded for steps in a run."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.evidence_id, e.step_id, e.evidence_type, e.content,
               e.source_url, e.hash, e.captured_at_utc
        FROM evidence e
        JOIN steps s ON s.step_id = e.step_id
        WHERE s.run_id = ?
        ORDER BY e.captured_at_utc ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "evidence_id": r["evidence_id"],
            "step_id": r["step_id"],
            "evidence_type": r["evidence_type"],
            "content": r["content"],
            "source_url": r["source_url"],
            "hash": r["hash"],
            "captured_at_utc": r["captured_at_utc"],
        })
    return results


def get_approvals_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all approvals for steps in a run."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.approval_id, a.step_id, a.action_type, a.payload,
               a.status, a.requested_at_utc, a.responded_at_utc, a.response_notes
        FROM approvals a
        JOIN steps s ON s.step_id = a.step_id
        WHERE s.run_id = ?
        ORDER BY a.requested_at_utc ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "approval_id": r["approval_id"],
            "step_id": r["step_id"],
            "action_type": r["action_type"],
            "payload": json.loads(r["payload"]) if r["payload"] else {},
            "status": r["status"],
            "requested_at_utc": r["requested_at_utc"],
            "responded_at_utc": r["responded_at_utc"],
            "response_notes": r["response_notes"],
        })
    return results


def update_approval_status(
    conn: sqlite3.Connection,
    approval_id: str,
    status: str,
    response_notes: Optional[str] = None,
) -> None:
    """Update approval status (APPROVED, REJECTED)."""
    now = get_utc_iso()
    with db_transaction(conn) as cur:
        cur.execute(
            """
            UPDATE approvals
            SET status = ?, responded_at_utc = ?, response_notes = ?
            WHERE approval_id = ?;
            """,
            (status, now, response_notes, approval_id),
        )


def get_artifacts_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all artifacts created for a run."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT artifact_id, run_id, step_id, artifact_type, path, sha256,
               metadata, created_at_utc
        FROM artifacts
        WHERE run_id = ?
        ORDER BY created_at_utc ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "artifact_id": r["artifact_id"],
            "run_id": r["run_id"],
            "step_id": r["step_id"],
            "artifact_type": r["artifact_type"],
            "path": r["path"],
            "sha256": r["sha256"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            "created_at_utc": r["created_at_utc"],
        })
    return results


def get_events_for_run(conn: sqlite3.Connection, run_id: str) -> List[dict[str, Any]]:
    """Retrieve all audit events for a run ordered by created_at_utc ascending."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_id, run_id, step_id, event_type, severity, payload, created_at_utc
        FROM events
        WHERE run_id = ?
        ORDER BY created_at_utc ASC;
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "event_id": r["event_id"],
            "run_id": r["run_id"],
            "step_id": r["step_id"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "payload": json.loads(r["payload"]) if r["payload"] else {},
            "created_at_utc": r["created_at_utc"],
        })
    return results
