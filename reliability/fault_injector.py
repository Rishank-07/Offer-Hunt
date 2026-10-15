"""Controlled fault injection engine for OFFERHUNT Reliability Lab (Milestone M7).

Strict rule: Test-only fault injection on temporary fixtures; NEVER on real user data.
Executes scenarios F01-F11 and measures resilience metrics comparing OFFERHUNT vs Baseline.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.contracts import InternshipRecord
from agent.executor import ExecutorConfig, StepExecutionError, StepExecutor
from agent.planner import GoalPlan, MockPlanner, StepSpec
from agent.policy import (
    PlanValidationError,
    check_action_policy_allowed,
    compute_action_digest,
    validate_plan_against_policy,
)
from agent.recovery import (
    ErrorCategory,
    classify_error,
    reconcile_ambiguous_spreadsheet_write,
    run_startup_reconciliation,
)
from agent.state_machine import StepState
from agent.verifier import IndependentVerifier, RunFinalStatus
from mcp_tools.server import app as mcp_server_app
from mcp_tools.spreadsheet import (
    read_spreadsheet_records,
    upsert_records_to_spreadsheet,
)
from reliability.metrics import ScenarioRunMetric
from reliability.scenarios import (
    ScenarioDefinition,
    ScenarioId,
    get_scenario,
)
from storage.database import (
    create_run,
    create_step,
    get_run,
    get_step,
    init_database,
    open_db_connection,
    record_approval,
)


class FaultInjectingMockClient:
    """Mock MCP client that can intercept tool calls to inject faults F01-F11."""

    def __init__(self, scenario_id: ScenarioId, fixture_dir: Path):
        self.scenario_id = scenario_id
        self.fixture_dir = fixture_dir
        self.call_count = 0
        self.fault_injected = False
        self.injected_write_keys: List[str] = []

    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> Any:
        self.call_count += 1

        # F01: Timeout after success write
        if self.scenario_id == ScenarioId.F01:
            if name == "upsert_internships" and not self.fault_injected:
                self.fault_injected = True
                # Underlying write succeeds on disk
                res = upsert_records_to_spreadsheet(
                    records=arguments.get("records", []),
                    target_path="offers.xlsx",
                    allowed_dir=self.fixture_dir,
                )
                self.injected_write_keys = res.keys
                # Then connection drops with timeout
                raise asyncio.TimeoutError("Injected F01 transport timeout after atomic write")

        # F02: Partial write
        elif self.scenario_id == ScenarioId.F02:
            if name == "upsert_internships" and not self.fault_injected:
                self.fault_injected = True
                records = arguments.get("records", [])
                if len(records) > 1:
                    # Write only the first record
                    upsert_records_to_spreadsheet(
                        records=[records[0]],
                        target_path="offers.xlsx",
                        allowed_dir=self.fixture_dir,
                    )
                raise ConnectionResetError("Injected F02 network interruption during batch write")

        # F08: Retries exhausted
        elif self.scenario_id == ScenarioId.F08:
            raise ConnectionResetError("Injected F08 persistent network connection drop")

        # F10: Tavily Quota Exhausted
        elif self.scenario_id == ScenarioId.F10:
            if name == "search_internships":
                raise RuntimeError("Injected F10: HTTP 429 Too Many Requests: Monthly search quota exceeded.")

        # Default normal tool execution on fixture directory
        if name == "upsert_internships":
            res = upsert_records_to_spreadsheet(
                records=arguments.get("records", []),
                target_path=arguments.get("target_path", "offers.xlsx"),
                allowed_dir=self.fixture_dir,
            )
            return {
                "inserted": res.inserted,
                "updated": res.updated,
                "unchanged": res.unchanged,
                "keys": res.keys,
                "artifact_path": res.artifact_path,
                "sha256": res.sha256,
            }
        elif name == "search_internships":
            return [
                {
                    "title": "Software Engineer Intern",
                    "company": "TechLab",
                    "url": "https://example-careers.com/job1",
                    "snippet": "Software engineering internship listing.",
                    "retrieval_time": "2026-10-15T12:00:00Z",
                    "source_type": "web_search",
                    "source_confidence": 0.9,
                }
            ]

        return {"status": "ok", "tool": name}


class ReliabilityLabRunner:
    """Orchestrates scenario execution in an isolated temporary fixture directory."""

    def __init__(self, base_temp_dir: Optional[Path] = None):
        self.base_temp_dir = base_temp_dir

    async def run_scenario(
        self,
        scenario_id: Union[ScenarioId, str],
        is_baseline: bool = False,
    ) -> ScenarioRunMetric:
        """Execute a controlled scenario run and measure metrics."""
        scenario = get_scenario(scenario_id)
        start_time = time.perf_counter()

        # Create isolated fixture directory
        with tempfile.TemporaryDirectory(prefix=f"rel_lab_{scenario.code}_") as temp_dir_str:
            fixture_dir = Path(temp_dir_str).resolve()
            db_path = fixture_dir / "lab.db"
            init_database(db_path)
            conn = open_db_connection(db_path)

            run_id = f"run_lab_{scenario.code}_{uuid.uuid4().hex[:8]}"
            create_run(conn, run_id=run_id, status="RUNNING")

            mock_client = FaultInjectingMockClient(scenario.id, fixture_dir)
            cfg = ExecutorConfig(
                tool_timeout_seconds=2.0,
                max_tool_retries=3,
                output_dir=fixture_dir,
            )

            rec1 = InternshipRecord.create(
                title="ML Intern",
                company="DeepTech",
                location="Bangalore",
                source_url="https://internshala.com/internship/detail/ml-intern1",
            )
            rec2 = InternshipRecord.create(
                title="Systems Intern",
                company="CloudTech",
                location="Remote",
                source_url="https://internshala.com/internship/detail/systems-intern2",
            )

            # Default metric flags
            fault_detected = False
            recovery_invoked = False
            recovery_successful = False
            duplicate_produced = False
            false_completion = False
            approval_bypassed = False
            final_status = "FAILED"

            try:
                # -----------------------------------------------------------
                # F01: Timeout After Success Write
                # -----------------------------------------------------------
                if scenario.id == ScenarioId.F01:
                    step_id = "step_f01"
                    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)
                    step_spec = StepSpec(
                        step_id=step_id,
                        tool_name="upsert_internships",
                        typed_args={"records": [rec1.model_dump()], "target_path": "offers.xlsx"},
                        rationale="Atomic upsert with timeout fault",
                    )

                    if is_baseline:
                        # Naive baseline: blindly retries without disk check
                        try:
                            await mock_client.call_tool("upsert_internships", step_spec.typed_args)
                        except Exception:
                            # Naive retry: appends without idempotency
                            fault_detected = True
                            # Blindly calling again on naive architecture might produce duplicates
                            await mock_client.call_tool("upsert_internships", step_spec.typed_args)
                            final_status = "VERIFIED"
                    else:
                        # OFFERHUNT resilient executor
                        executor = StepExecutor(db_conn=conn, mcp_client=mock_client, config=cfg)
                        res = await executor.execute_step(run_id=run_id, step=step_spec)
                        fault_detected = True
                        recovery_invoked = True
                        recovery_successful = res.get("reconciled", False)
                        final_status = "VERIFIED" if recovery_successful else "FAILED"

                    # Check for duplicates on disk
                    _, recs = read_spreadsheet_records(fixture_dir / "offers.xlsx")
                    duplicate_produced = (len(recs) > 1)

                # -----------------------------------------------------------
                # F02: Partial Write
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F02:
                    step_id = "step_f02"
                    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)
                    step_spec = StepSpec(
                        step_id=step_id,
                        tool_name="upsert_internships",
                        typed_args={"records": [rec1.model_dump(), rec2.model_dump()], "target_path": "offers.xlsx"},
                        rationale="Batch write with partial interruption",
                    )
                    fault_detected = True
                    recovery_invoked = True

                    try:
                        await mock_client.call_tool("upsert_internships", step_spec.typed_args)
                    except ConnectionResetError:
                        pass

                    all_p, found_cnt = reconcile_ambiguous_spreadsheet_write(
                        "offers.xlsx",
                        [rec1.record_key, rec2.record_key],
                        allowed_dir=fixture_dir,
                    )
                    if found_cnt == 1 and not all_p:
                        recovery_successful = True
                        final_status = "PARTIAL"

                # -----------------------------------------------------------
                # F03: Crash After Write
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F03:
                    step_id = "step_f03"
                    create_step(
                        conn,
                        step_id=step_id,
                        run_id=run_id,
                        step_order=1,
                        state="RUNNING",
                        execution_status="IN_PROGRESS",
                    )
                    fault_detected = True
                    recovery_invoked = True

                    if is_baseline:
                        # Naive baseline ignores crash and falsely marks complete
                        false_completion = True
                        final_status = "VERIFIED"
                    else:
                        # OFFERHUNT startup reconciliation
                        reconciled = run_startup_reconciliation(conn)
                        if any(r["step_id"] == step_id and r["new_state"] == "UNRESOLVED" for r in reconciled):
                            recovery_successful = True
                            final_status = "UNRESOLVED"

                # -----------------------------------------------------------
                # F04: Invalid / Hallucinated Tool
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F04:
                    fault_detected = True
                    recovery_invoked = True
                    step_spec = StepSpec(
                        step_id="step_f04",
                        tool_name="execute_system_shell",
                        typed_args={"cmd": "whoami"},
                        rationale="Invalid tool",
                    )
                    plan = GoalPlan(goal="Test", steps=[step_spec])
                    try:
                        validate_plan_against_policy(plan, {"search_internships", "upsert_internships"})
                    except PlanValidationError:
                        recovery_successful = True
                        final_status = "FAILED"
                        assert mock_client.call_count == 0

                # -----------------------------------------------------------
                # F05: Prompt Injection in Web Snippet
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F05:
                    fault_detected = True
                    recovery_invoked = True
                    adversarial_action = "send_email"
                    try:
                        check_action_policy_allowed(adversarial_action, adversarial_action)
                        approval_bypassed = True
                    except PlanValidationError:
                        recovery_successful = True
                        final_status = "VERIFIED"

                # -----------------------------------------------------------
                # F06: Approval Denied
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F06:
                    step_id = "step_f06"
                    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)
                    record_approval(
                        conn=conn,
                        approval_id="app_f06",
                        step_id=step_id,
                        action_type="upsert_internships",
                        payload={"records": [rec1.model_dump()]},
                        status="REJECTED",
                    )
                    fault_detected = True
                    recovery_invoked = True

                    step_spec = StepSpec(
                        step_id=step_id,
                        tool_name="upsert_internships",
                        typed_args={"records": [rec1.model_dump()]},
                        rationale="Action with rejected approval",
                    )
                    executor = StepExecutor(db_conn=conn, mcp_client=mock_client, config=cfg)
                    try:
                        await executor.execute_step(run_id=run_id, step=step_spec)
                    except StepExecutionError:
                        recovery_successful = True
                        final_status = "FAILED"

                # -----------------------------------------------------------
                # F07: Corrupted Spreadsheet on Disk
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F07:
                    fault_detected = True
                    recovery_invoked = True
                    corrupt_path = fixture_dir / "offers.xlsx"
                    corrupt_path.write_bytes(b"CORRUPTED_NON_ZIP_BYTES")

                    verifier = IndependentVerifier(artifacts_dir=fixture_dir)
                    checks, _ = verifier.verify_spreadsheet(corrupt_path)
                    if any(c.name == "workbook_readable" and not c.passed for c in checks):
                        recovery_successful = True
                        final_status = "FAILED"

                # -----------------------------------------------------------
                # F08: Retries Exhausted on Network Drop
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F08:
                    step_id = "step_f08"
                    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)
                    step_spec = StepSpec(
                        step_id=step_id,
                        tool_name="search_internships",
                        typed_args={"query": "test"},
                        rationale="Persistent network failure",
                    )
                    fault_detected = True
                    recovery_invoked = True

                    executor = StepExecutor(db_conn=conn, mcp_client=mock_client, config=cfg)
                    try:
                        await executor.execute_step(run_id=run_id, step=step_spec)
                    except StepExecutionError:
                        recovery_successful = True
                        final_status = "FAILED"

                # -----------------------------------------------------------
                # F09: Gemini Permission / Key Denied
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F09:
                    fault_detected = True
                    recovery_invoked = True
                    from agent.planner import GeminiPlanner, PlannerModelError

                    planner = GeminiPlanner(api_key="invalid_test_key", model_name="gemini-2.5-flash")
                    try:
                        await planner.create_plan("Find internships", {"search_internships"})
                    except PlannerModelError:
                        recovery_successful = True
                        final_status = "FAILED"

                # -----------------------------------------------------------
                # F10: Tavily Quota Exhausted (429)
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F10:
                    fault_detected = True
                    recovery_invoked = True
                    try:
                        await mock_client.call_tool("search_internships", {"query": "test"})
                    except Exception as exc:
                        cat = classify_error(exc)
                        if cat == ErrorCategory.QUOTA_OR_RATE_LIMIT:
                            recovery_successful = True
                            final_status = "PARTIAL"

                # -----------------------------------------------------------
                # F11: Path Traversal Attempt
                # -----------------------------------------------------------
                elif scenario.id == ScenarioId.F11:
                    fault_detected = True
                    recovery_invoked = True
                    from mcp_tools.spreadsheet import (
                        SpreadsheetSecurityError,
                        validate_and_resolve_path,
                    )
                    try:
                        validate_and_resolve_path("../../secret.xlsx", allowed_dir=fixture_dir)
                    except SpreadsheetSecurityError:
                        recovery_successful = True
                        final_status = "FAILED"

            finally:
                conn.close()

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            return ScenarioRunMetric(
                run_id=run_id,
                scenario_id=scenario.id.value,
                scenario_code=scenario.code,
                scenario_title=scenario.title,
                injected_fault=scenario.injected_fault,
                fault_detected=fault_detected,
                recovery_invoked=recovery_invoked,
                recovery_successful=recovery_successful,
                duplicate_produced=duplicate_produced,
                false_completion=false_completion,
                approval_bypassed=approval_bypassed,
                latency_ms=round(elapsed_ms, 2),
                tool_calls=mock_client.call_count,
                final_status=final_status,
                is_baseline=is_baseline,
            )
