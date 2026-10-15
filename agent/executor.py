"""Step executor and run orchestrator for OFFERHUNT (Milestone M5).

Implements:
- Persist-before-call: writes step as RUNNING with attempt_number, idempotency_key,
  and input_hash BEFORE calling effectful tools.
- Config-driven execution: TOOL_TIMEOUT_SECONDS, RUN_TIMEOUT_SECONDS, MAX_TOOL_CALLS, MAX_TOOL_RETRIES.
- Error classification and recovery per specification section 16.
- Ambiguous write reconciliation: reads spreadsheet by record_key first; never blind-retries.
- Approval and policy enforcement at executor/tool boundary.
- Comprehensive tool attempt and event logging to SQLite with secrets redacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from agent.mcp_client import MCPClientWrapper
from agent.planner import GoalPlan, StepSpec
from agent.policy import (
    RiskLevel,
    check_action_policy_allowed,
    classify_tool_risk,
    compute_action_digest,
    verify_executor_approval,
)
from agent.recovery import (
    ErrorCategory,
    classify_error,
    compute_backoff_delay,
    reconcile_ambiguous_spreadsheet_write,
)
from agent.state_machine import (
    ExecutionStatus,
    StepState,
    VerificationStatus,
    transition_step,
)
from mcp_tools.tool_errors import MCPToolError, ToolErrorCode, scrub_secrets
from storage.database import (
    db_transaction,
    get_step,
    get_utc_iso,
    record_event,
    record_evidence,
    record_tool_attempt,
    update_step_state,
)


logger = logging.getLogger("offerhunt.executor")


@dataclass
class ExecutorConfig:
    """Configurable execution parameters loaded from environment or custom settings."""
    tool_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("TOOL_TIMEOUT_SECONDS", "30.0"))
    )
    run_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("RUN_TIMEOUT_SECONDS", "300.0"))
    )
    max_tool_calls: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOOL_CALLS", "20"))
    )
    max_tool_retries: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOOL_RETRIES", "3"))
    )
    output_dir: Path = field(
        default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "artifacts")).resolve()
    )


class StepExecutionError(RuntimeError):
    """Raised when step execution ultimately fails after exhaustion or policy block."""

    def __init__(self, category: ErrorCategory, message: str):
        clean_msg = scrub_secrets(message)
        super().__init__(f"[{category.value}] {clean_msg}")
        self.category = category
        self.message = clean_msg


def compute_input_hash(typed_args: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of step arguments."""
    canonical = json.dumps(typed_args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StepExecutor:
    """Executes planned steps with persist-before-call semantics and bounded recovery."""

    def __init__(
        self,
        db_conn: Any,
        mcp_client: MCPClientWrapper,
        config: Optional[ExecutorConfig] = None,
    ):
        self.conn = db_conn
        self.client = mcp_client
        self.config = config or ExecutorConfig()
        self.total_tool_calls = 0

    async def execute_step(
        self,
        run_id: str,
        step: StepSpec,
    ) -> dict[str, Any]:
        """Execute a single step enforcing:

        1. Policy & boundary approvals check.
        2. Persist-before-call state update in DB.
        3. Config-driven timeout and retry loop with jittered backoff.
        4. Safe recovery on ambiguous outcomes (checks spreadsheet before retrying).
        5. Redacted attempt logging to tool_attempts and events tables.
        """
        step_id = step.step_id
        tool_name = step.tool_name
        args = dict(step.typed_args)
        input_hash = compute_input_hash(args)

        # -------------------------------------------------------------------
        # 1. Approval and Policy Enforcement at Executor Boundary
        # -------------------------------------------------------------------
        try:
            check_action_policy_allowed(action_type=tool_name, tool_name=tool_name)
        except Exception as policy_err:
            # Policy block: record event and fail immediately with zero tool invocation
            event_id = f"evt_block_{uuid.uuid4().hex[:8]}"
            record_event(
                conn=self.conn,
                event_id=event_id,
                run_id=run_id,
                step_id=step_id,
                event_type="POLICY_BLOCK",
                severity="CRITICAL",
                payload={"tool_name": tool_name, "reason": str(policy_err)},
            )
            update_step_state(
                conn=self.conn,
                step_id=step_id,
                state=StepState.FAILED.value,
                execution_status=ExecutionStatus.FAILED.value,
                verification_status=VerificationStatus.FAILED.value,
                error_payload={"error_code": "POLICY_BLOCK", "message": str(policy_err)},
            )
            raise StepExecutionError(ErrorCategory.POLICY_BLOCK, str(policy_err)) from None

        # Check approval if tool has external-write or irreversible risk, or approval record exists
        risk = classify_tool_risk(tool_name)
        cur = self.conn.cursor()
        cur.execute("SELECT approval_id FROM approvals WHERE step_id = ?;", (step_id,))
        has_approval = cur.fetchone() is not None

        if risk in (RiskLevel.EXTERNAL_WRITE, RiskLevel.IRREVERSIBLE) or has_approval:
            is_approved, approval_status = verify_executor_approval(
                conn=self.conn,
                step_id=step_id,
                action_type=tool_name,
                payload=args,
            )
            if not is_approved:
                err_msg = f"Execution blocked: approval is {approval_status} for action '{tool_name}'."
                update_step_state(
                    conn=self.conn,
                    step_id=step_id,
                    state=StepState.NEEDS_APPROVAL.value if approval_status == "PENDING" else StepState.FAILED.value,
                    execution_status=ExecutionStatus.CANCELLED.value,
                    error_payload={"error_code": "APPROVAL_REQUIRED", "status": approval_status},
                )
                raise StepExecutionError(ErrorCategory.POLICY_BLOCK, err_msg)

        # -------------------------------------------------------------------
        # 2. Persist-Before-Call
        # -------------------------------------------------------------------
        attempt_number = 1
        idempotency_key = f"idemp_{step_id}_{attempt_number}_{input_hash[:16]}"

        update_step_state(
            conn=self.conn,
            step_id=step_id,
            state=StepState.RUNNING.value,
            execution_status=ExecutionStatus.IN_PROGRESS.value,
            verification_status=VerificationStatus.UNVERIFIED.value,
        )

        record_event(
            conn=self.conn,
            event_id=f"evt_start_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            step_id=step_id,
            event_type="STEP_EXECUTION_STARTING",
            severity="INFO",
            payload={
                "tool_name": tool_name,
                "attempt_number": attempt_number,
                "idempotency_key": idempotency_key,
                "input_hash": input_hash,
            },
        )

        # -------------------------------------------------------------------
        # 3. Execution & Recovery Loop (Section 16 Pseudocode)
        # -------------------------------------------------------------------
        last_exception: Optional[Exception] = None
        has_started_write = (risk == RiskLevel.LOCAL_WRITE)
        output_result: Any = None

        while attempt_number <= self.config.max_tool_retries:
            if self.total_tool_calls >= self.config.max_tool_calls:
                err_msg = f"Exceeded MAX_TOOL_CALLS budget ({self.total_tool_calls} >= {self.config.max_tool_calls})."
                update_step_state(
                    conn=self.conn,
                    step_id=step_id,
                    state=StepState.FAILED.value,
                    execution_status=ExecutionStatus.FAILED.value,
                    error_payload={"error_code": "BUDGET_EXCEEDED", "message": err_msg},
                )
                raise StepExecutionError(ErrorCategory.QUOTA_OR_RATE_LIMIT, err_msg)

            attempt_id = f"att_{step_id}_{attempt_number}_{uuid.uuid4().hex[:6]}"
            started_at = get_utc_iso()
            self.total_tool_calls += 1

            try:
                # Call MCP tool with per-call timeout
                call_coro = self.client.call_tool(
                    name=tool_name,
                    arguments=args,
                    timeout=self.config.tool_timeout_seconds,
                )
                output_result = await call_coro

                # Success: record attempt and transition step
                completed_at = get_utc_iso()
                record_tool_attempt(
                    conn=self.conn,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    tool_name=tool_name,
                    parameters=args,
                    result={"output": output_result} if isinstance(output_result, (dict, list, str)) else {"status": "ok"},
                    error=None,
                    retry_count=attempt_number - 1,
                    started_at_utc=started_at,
                    completed_at_utc=completed_at,
                )

                update_step_state(
                    conn=self.conn,
                    step_id=step_id,
                    state=StepState.EXECUTED.value,
                    execution_status=ExecutionStatus.COMPLETED.value,
                    output_payload={"result": output_result} if isinstance(output_result, dict) else {"output": output_result},
                )

                # Capture evidence if relevant
                if tool_name == "search_internships" and isinstance(output_result, list):
                    for item in output_result[:5]:
                        if isinstance(item, dict) and item.get("url"):
                            record_evidence(
                                conn=self.conn,
                                evidence_id=f"evi_{uuid.uuid4().hex[:10]}",
                                step_id=step_id,
                                evidence_type="web_search_result",
                                content=f"{item.get('title')} at {item.get('company')}: {item.get('snippet')}",
                                source_url=item.get("url"),
                                content_hash=hashlib.sha256(str(item).encode()).hexdigest(),
                            )

                return {
                    "step_id": step_id,
                    "state": StepState.EXECUTED.value,
                    "result": output_result,
                    "attempt_number": attempt_number,
                }

            except Exception as exc:
                last_exception = exc
                completed_at = get_utc_iso()
                clean_err_msg = scrub_secrets(str(exc))
                category = classify_error(exc, tool_name=tool_name, has_started_write=has_started_write)

                # Record attempt failure with secrets redacted
                record_tool_attempt(
                    conn=self.conn,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    tool_name=tool_name,
                    parameters=args,
                    result=None,
                    error={"error_code": category.value, "message": clean_err_msg},
                    retry_count=attempt_number - 1,
                    started_at_utc=started_at,
                    completed_at_utc=completed_at,
                )

                record_event(
                    conn=self.conn,
                    event_id=f"evt_err_{uuid.uuid4().hex[:8]}",
                    run_id=run_id,
                    step_id=step_id,
                    event_type="TOOL_ATTEMPT_FAILED",
                    severity="WARNING",
                    payload={"category": category.value, "attempt": attempt_number, "message": clean_err_msg},
                )

                # -----------------------------------------------------------
                # Recovery Branch: Section 16 Logic
                # -----------------------------------------------------------
                # Non-retryable errors abort immediately
                if category in (
                    ErrorCategory.VALIDATION,
                    ErrorCategory.AUTH_OR_PERMISSION,
                    ErrorCategory.POLICY_BLOCK,
                ):
                    update_step_state(
                        conn=self.conn,
                        step_id=step_id,
                        state=StepState.FAILED.value,
                        execution_status=ExecutionStatus.FAILED.value,
                        error_payload={"category": category.value, "message": clean_err_msg},
                    )
                    raise StepExecutionError(category, clean_err_msg) from exc

                # Ambiguous after-effect (timeout/disconnect after write):
                # READ spreadsheet first to reconcile! Never blind-retry!
                if category == ErrorCategory.AMBIGUOUS_AFTER_EFFECT and tool_name == "upsert_internships":
                    records = args.get("records") or []
                    expected_keys = [str(r.get("record_key") or "") for r in records if r.get("record_key")]
                    target_path = args.get("target_path", "offers.xlsx")

                    all_present, found_count = reconcile_ambiguous_spreadsheet_write(
                        target_path,
                        expected_keys,
                        allowed_dir=self.config.output_dir,
                    )
                    if all_present:
                        # Success verified on disk despite timeout! Reconciled without blind retry.
                        record_event(
                            conn=self.conn,
                            event_id=f"evt_recon_{uuid.uuid4().hex[:8]}",
                            run_id=run_id,
                            step_id=step_id,
                            event_type="AMBIGUOUS_WRITE_RECONCILED",
                            severity="INFO",
                            payload={"reconciled_keys": expected_keys, "reason": "All records confirmed on disk."},
                        )
                        update_step_state(
                            conn=self.conn,
                            step_id=step_id,
                            state=StepState.EXECUTED.value,
                            execution_status=ExecutionStatus.COMPLETED.value,
                            output_payload={"reconciled": True, "keys_found": found_count},
                        )
                        return {
                            "step_id": step_id,
                            "state": StepState.EXECUTED.value,
                            "reconciled": True,
                            "attempt_number": attempt_number,
                        }

                # Retryable transient error: backoff with jitter
                if attempt_number < self.config.max_tool_retries:
                    delay = compute_backoff_delay(attempt_number)
                    update_step_state(
                        conn=self.conn,
                        step_id=step_id,
                        state=StepState.RETRY_WAIT.value,
                        execution_status=ExecutionStatus.IN_PROGRESS.value,
                    )
                    await asyncio.sleep(delay)
                    attempt_number += 1
                else:
                    break

        # Retries exhausted -> UNRESOLVED / FAILED
        final_category = classify_error(last_exception, tool_name=tool_name, has_started_write=has_started_write) if last_exception else ErrorCategory.TRANSIENT_TRANSPORT
        final_state = StepState.UNRESOLVED.value if final_category in (ErrorCategory.AMBIGUOUS_AFTER_EFFECT, ErrorCategory.PARTIAL_RESULT) else StepState.FAILED.value

        update_step_state(
            conn=self.conn,
            step_id=step_id,
            state=final_state,
            execution_status=ExecutionStatus.FAILED.value,
            error_payload={
                "category": final_category.value,
                "message": f"Retries exhausted ({attempt_number} attempts). Last error: {scrub_secrets(str(last_exception))}",
            },
        )
        raise StepExecutionError(final_category, f"Step execution failed after {attempt_number} attempts.")
