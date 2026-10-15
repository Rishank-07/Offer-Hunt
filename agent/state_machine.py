"""State machine and lifecycle orchestration for OFFERHUNT (Milestone M2).

Implements strict state transition validation, separated execution/verification
statuses, and startup reconciliation of interrupted RUNNING steps.
"""

from __future__ import annotations

import sqlite3
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union

from storage.database import (
    db_transaction,
    get_step,
    get_utc_iso,
    open_db_connection,
    record_event,
    update_step_state,
)


class StepState(str, Enum):
    """Lifecycle states for steps."""
    READY = "READY"
    RUNNING = "RUNNING"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    RETRY_WAIT = "RETRY_WAIT"
    UNRESOLVED = "UNRESOLVED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    FAILED = "FAILED"


class ExecutionStatus(str, Enum):
    """Execution status for steps."""
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VerificationStatus(str, Enum):
    """Verification status for steps."""
    UNVERIFIED = "UNVERIFIED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    REQUIRES_RECONCILIATION = "REQUIRES_RECONCILIATION"


class IllegalStateTransitionError(ValueError):
    """Raised when an illegal or unauthorized step state transition is attempted."""
    pass


# Valid legal transitions graph
LEGAL_TRANSITIONS: Dict[StepState, Set[StepState]] = {
    StepState.READY: {
        StepState.RUNNING,
        StepState.FAILED,
    },
    StepState.RUNNING: {
        StepState.EXECUTED,
        StepState.RETRY_WAIT,
        StepState.NEEDS_APPROVAL,
        StepState.UNRESOLVED,
        StepState.FAILED,
    },
    StepState.EXECUTED: {
        StepState.VERIFIED,
        StepState.UNRESOLVED,
        StepState.FAILED,
        StepState.NEEDS_APPROVAL,
        StepState.RETRY_WAIT,
    },
    StepState.VERIFIED: {
        # Verified steps can be transitioned to READY if re-triggered or remain terminal
        StepState.READY,
    },
    StepState.RETRY_WAIT: {
        StepState.READY,
        StepState.RUNNING,
        StepState.FAILED,
    },
    StepState.NEEDS_APPROVAL: {
        StepState.READY,
        StepState.RUNNING,
        StepState.FAILED,
        StepState.UNRESOLVED,
    },
    StepState.UNRESOLVED: {
        StepState.RETRY_WAIT,
        StepState.READY,
        StepState.FAILED,
        StepState.NEEDS_APPROVAL,
    },
    StepState.FAILED: {
        # Failed steps can be reset to READY or RETRY_WAIT if recovery is attempted
        StepState.READY,
        StepState.RETRY_WAIT,
    },
}


class StepStateMachine:
    """Manages state transitions for a single step with invariant checks."""

    def __init__(
        self,
        current_state: Union[StepState, str] = StepState.READY,
        execution_status: Union[ExecutionStatus, str] = ExecutionStatus.NOT_STARTED,
        verification_status: Union[VerificationStatus, str] = VerificationStatus.UNVERIFIED,
    ):
        self.current_state = StepState(current_state)
        self.execution_status = ExecutionStatus(execution_status)
        self.verification_status = VerificationStatus(verification_status)

    def transition_to(
        self,
        target_state: Union[StepState, str],
        execution_status: Optional[Union[ExecutionStatus, str]] = None,
        verification_status: Optional[Union[VerificationStatus, str]] = None,
    ) -> tuple[StepState, ExecutionStatus, VerificationStatus]:
        """Validate and apply state transition.

        Raises:
            IllegalStateTransitionError if transition is not permitted.
        """
        target = StepState(target_state)
        allowed = LEGAL_TRANSITIONS.get(self.current_state, set())

        if target not in allowed:
            raise IllegalStateTransitionError(
                f"Illegal state transition: cannot transition from {self.current_state.value} to {target.value}. "
                f"Allowed transitions: {[s.value for s in allowed]}"
            )

        self.current_state = target

        # Default accompanying status updates based on destination state if not provided
        if execution_status is not None:
            self.execution_status = ExecutionStatus(execution_status)
        else:
            if target == StepState.RUNNING:
                self.execution_status = ExecutionStatus.IN_PROGRESS
            elif target in (StepState.EXECUTED, StepState.VERIFIED):
                self.execution_status = ExecutionStatus.COMPLETED
            elif target == StepState.FAILED:
                self.execution_status = ExecutionStatus.FAILED

        if verification_status is not None:
            self.verification_status = VerificationStatus(verification_status)
        else:
            if target == StepState.VERIFIED:
                self.verification_status = VerificationStatus.PASSED
            elif target == StepState.UNRESOLVED:
                self.verification_status = VerificationStatus.REQUIRES_RECONCILIATION

        return self.current_state, self.execution_status, self.verification_status


def transition_step(
    conn: sqlite3.Connection,
    step_id: str,
    target_state: Union[StepState, str],
    execution_status: Optional[Union[ExecutionStatus, str]] = None,
    verification_status: Optional[Union[VerificationStatus, str]] = None,
    output_payload: Optional[dict[str, Any]] = None,
    error_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Load step from database, apply validated state transition, and persist update."""
    step = get_step(conn, step_id)
    if not step:
        raise ValueError(f"Step '{step_id}' not found in database.")

    sm = StepStateMachine(
        current_state=step["state"],
        execution_status=step["execution_status"],
        verification_status=step["verification_status"],
    )

    new_state, new_exec, new_ver = sm.transition_to(
        target_state, execution_status, verification_status
    )

    update_step_state(
        conn=conn,
        step_id=step_id,
        state=new_state.value,
        execution_status=new_exec.value,
        verification_status=new_ver.value,
        output_payload=output_payload,
        error_payload=error_payload,
    )

    return {
        "step_id": step_id,
        "state": new_state.value,
        "execution_status": new_exec.value,
        "verification_status": new_ver.value,
    }


def reconcile_interrupted_steps(
    db_conn_or_path: Union[sqlite3.Connection, str]
) -> List[dict[str, Any]]:
    """Startup reconciliation check.

    Finds all steps in state 'RUNNING' or execution_status 'IN_PROGRESS'
    (which indicates an unexpected process kill or abrupt crash),
    transitions them to 'UNRESOLVED' with verification_status 'REQUIRES_RECONCILIATION',
    and logs an audit event for each reconciled step.
    """
    should_close = False
    if not isinstance(db_conn_or_path, sqlite3.Connection):
        conn = open_db_connection(db_conn_or_path)
        should_close = True
    else:
        conn = db_conn_or_path

    reconciled_steps: List[dict[str, Any]] = []

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT step_id, run_id, step_order, state, execution_status, verification_status
            FROM steps
            WHERE state = 'RUNNING' OR execution_status = 'IN_PROGRESS';
            """
        )
        interrupted_steps = [dict(row) for row in cur.fetchall()]

        for step in interrupted_steps:
            step_id = step["step_id"]
            run_id = step["run_id"]
            event_id = f"evt_recon_{uuid.uuid4().hex[:10]}"

            # Transition to UNRESOLVED / REQUIRES_RECONCILIATION
            update_step_state(
                conn=conn,
                step_id=step_id,
                state=StepState.UNRESOLVED.value,
                execution_status=ExecutionStatus.FAILED.value,
                verification_status=VerificationStatus.REQUIRES_RECONCILIATION.value,
                error_payload={
                    "error_code": "INTERRUPTED_AT_STARTUP",
                    "reason": "Process crash or unexpected termination during step execution.",
                    "previous_state": step["state"],
                    "previous_execution_status": step["execution_status"],
                },
            )

            record_event(
                conn=conn,
                event_id=event_id,
                run_id=run_id,
                step_id=step_id,
                event_type="STEP_STARTUP_RECONCILIATION",
                severity="WARNING",
                payload={
                    "reconciled_step_id": step_id,
                    "previous_state": step["state"],
                    "new_state": StepState.UNRESOLVED.value,
                    "reason": "Interrupted RUNNING step reconciled at startup",
                },
            )

            reconciled_steps.append({
                "step_id": step_id,
                "run_id": run_id,
                "previous_state": step["state"],
                "new_state": StepState.UNRESOLVED.value,
                "verification_status": VerificationStatus.REQUIRES_RECONCILIATION.value,
            })

        return reconciled_steps
    finally:
        if should_close:
            conn.close()
