"""Service orchestration layer for OFFERHUNT (Milestones M7 and M8).

Enforces strict separation of concerns:
- Contains ALL business logic, execution orchestration, and audit recording.
- app.py and other UI layers delegate entirely to this service; NO business logic in the UI.
- All state updates persist directly to SQLite (offerhunt.db).
- Verifies artifacts independently; summary text can never override failed checks.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.contracts import InternshipRecord
from agent.executor import ExecutorConfig, StepExecutionError, StepExecutor
from agent.planner import (
    GeminiPlanner,
    GoalPlan,
    MockPlanner,
    PlannerModelError,
    StepSpec,
    persist_plan_and_catalog,
)
from agent.policy import (
    PlanValidationError,
    check_action_policy_allowed,
    classify_tool_risk,
    validate_dag_acyclic,
    validate_plan_against_policy,
)
from agent.recovery import (
    ErrorCategory,
    classify_error,
    reconcile_ambiguous_spreadsheet_write,
    run_startup_reconciliation,
)
from agent.state_machine import ExecutionStatus, StepState, VerificationStatus
from agent.verifier import (
    ClaimEvidenceLink,
    IndependentVerifier,
    RunFinalStatus,
    VerificationCheck,
    VerificationReport,
)
from mcp_tools.drafts import create_local_draft
from mcp_tools.research import search_internships_tavily
from mcp_tools.server import (
    create_email_draft as mcp_create_email_draft,
    get_run_evidence as mcp_get_run_evidence,
    read_internships as mcp_read_internships,
    search_internships as mcp_search_internships,
    upsert_internships as mcp_upsert_internships,
    verify_task_result as mcp_verify_task_result,
)
from mcp_tools.spreadsheet import (
    DEFAULT_OUTPUT_DIR,
    compute_file_sha256,
    read_spreadsheet_records,
    upsert_records_to_spreadsheet,
    validate_and_resolve_path,
)
from mcp_tools.tool_errors import MCPToolError, ToolErrorCode
from storage.database import (
    DEFAULT_DB_PATH,
    check_database_integrity,
    create_run,
    create_step,
    get_approvals_for_run,
    get_artifacts_for_run,
    get_events_for_run,
    get_evidence_for_run,
    get_run,
    get_step,
    get_steps_for_run,
    get_tool_attempts_for_run,
    get_utc_iso,
    init_database,
    list_runs,
    open_db_connection,
    record_approval,
    record_artifact,
    record_event,
    record_evidence,
    update_approval_status,
    update_run_status,
    update_step_state,
)

DEFAULT_PERSONA = {
    "name": "Alex Chen",
    "education": "3rd year CS Undergrad",
    "skills": "Python, PyTorch, FastAPI, SQL",
    "interests": "Machine Learning, Backend Systems",
    "disclaimer": "Synthetic persona only. OFFERHUNT never infers personal facts the user did not provide.",
}

CATALOG_TOOLS = {
    "search_internships",
    "upsert_internships",
    "read_internships",
    "create_email_draft",
    "verify_task_result",
    "get_run_evidence",
}


class DirectInProcessMCPClient:
    """Fast, in-process MCP client executing registered tools with exact bounds and error formats."""

    def __init__(
        self,
        artifacts_dir: Path,
        db_path: Path,
        demo_fixtures_path: Optional[Path] = None,
    ):
        self.artifacts_dir = Path(artifacts_dir).resolve()
        self.db_path = Path(db_path).resolve()
        self.demo_fixtures_path = demo_fixtures_path
        self.call_count = 0

    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> Any:
        self.call_count += 1

        if name == "search_internships":
            query = arguments.get("query", "")
            max_results = arguments.get("max_results", 5)
            location = arguments.get("location")
            # Call underlying search
            tavily_key = os.getenv("TAVILY_API_KEY")
            if not tavily_key or tavily_key == "your_tavily_api_key_here":
                # Demo fixture mode
                if self.demo_fixtures_path and self.demo_fixtures_path.exists():
                    try:
                        with open(self.demo_fixtures_path, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        return raw[:max_results]
                    except Exception:
                        pass
            return search_internships_tavily(query=query, max_results=max_results, location=location)

        elif name == "upsert_internships":
            records = arguments.get("records", [])
            target_path = arguments.get("target_path", "offers.xlsx")
            cleaned_records = []
            for r in records:
                if not r.get("record_key"):
                    rec_obj = InternshipRecord.create(
                        title=str(r.get("title", "")),
                        company=str(r.get("company", "")),
                        location=str(r.get("location", "Remote")),
                        source_url=r.get("source_url"),
                        eligibility=r.get("eligibility"),
                        stipend=r.get("stipend"),
                        deadline=r.get("deadline"),
                        source_title=r.get("source_title"),
                        snippet=r.get("snippet"),
                        source_type=r.get("source_type", "mcp_tool"),
                        confidence=float(r.get("confidence") or 1.0),
                        status=r.get("status", "VERIFIED"),
                    )
                    cleaned_records.append(rec_obj.model_dump())
                else:
                    cleaned_records.append(r)

            res = upsert_records_to_spreadsheet(
                records=cleaned_records,
                target_path=target_path,
                allowed_dir=self.artifacts_dir,
            )
            return {
                "inserted": res.inserted,
                "updated": res.updated,
                "unchanged": res.unchanged,
                "keys": res.keys,
                "artifact_path": res.artifact_path,
                "sha256": res.sha256,
            }

        elif name == "read_internships":
            target_path = arguments.get("target_path", "offers.xlsx")
            limit = max(1, min(int(arguments.get("limit", 100)), 500))
            resolved = validate_and_resolve_path(target_path, allowed_dir=self.artifacts_dir)
            headers, recs_dict = read_spreadsheet_records(resolved)
            keys = list(recs_dict.keys())[:limit]
            rows = [recs_dict[k][1] for k in keys]
            return {
                "headers": headers,
                "records": rows,
                "total_records": len(recs_dict),
                "returned_records": len(rows),
                "artifact_path": str(resolved),
            }

        elif name == "create_email_draft":
            recipient = arguments.get("recipient", "")
            subject = arguments.get("subject", "")
            body = arguments.get("body", "")
            draft_res = create_local_draft(
                recipient=recipient,
                subject=subject,
                body=body,
                drafts_dir=self.artifacts_dir / "drafts",
            )
            eml_p = Path(draft_res.eml_path)
            sha = compute_file_sha256(eml_p) if eml_p.exists() else ""
            return {
                "draft_id": draft_res.draft_id,
                "eml_path": draft_res.eml_path,
                "txt_path": draft_res.txt_path,
                "recipient": draft_res.recipient,
                "subject": draft_res.subject,
                "state": draft_res.state,
                "sha256_eml": sha,
            }

        elif name == "verify_task_result":
            step_id = arguments.get("step_id", "step_0")
            expected_artifact = arguments.get("expected_artifact") or arguments.get("target_path", "offers.xlsx")
            verification_method = arguments.get("verification_method", "file_exists_and_valid")
            res = mcp_verify_task_result(
                step_id=step_id,
                expected_artifact=expected_artifact,
                verification_method=verification_method,
                db_path=str(self.db_path),
            )
            return res

        elif name == "get_run_evidence":
            res = mcp_get_run_evidence()
            return res

        else:
            raise MCPToolError(
                ToolErrorCode.TOOL_NOT_FOUND,
                f"Unknown tool: '{name}' is not in OFFERHUNT MCP catalog.",
            )


class OfferHuntService:
    """Central service managing OFFERHUNT discovery runs, persistence, approvals, and audits."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        artifacts_dir: Path = DEFAULT_OUTPUT_DIR,
        demo_fixtures_path: Path = Path("data/internships_demo.json"),
    ):
        self.db_path = Path(db_path).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve()
        self.demo_fixtures_path = Path(demo_fixtures_path).resolve()

        # Ensure infrastructure exists
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        init_database(self.db_path)

        # Run startup reconciliation to clean up any orphaned RUNNING steps
        conn = open_db_connection(self.db_path)
        try:
            run_startup_reconciliation(conn)
        finally:
            conn.close()

    def get_diagnostics(self) -> Dict[str, Any]:
        """Inspect environment, API keys, database integrity, and demo status."""
        gemini_key = os.getenv("GEMINI_API_KEY")
        tavily_key = os.getenv("TAVILY_API_KEY")

        gemini_available = bool(gemini_key and gemini_key != "your_gemini_api_key_here")
        tavily_available = bool(tavily_key and tavily_key != "your_tavily_api_key_here")

        conn = open_db_connection(self.db_path)
        try:
            db_ok, _ = check_database_integrity(self.db_path)
        finally:
            conn.close()

        demo_present = self.demo_fixtures_path.exists()
        is_demo_mode = (not gemini_available) or (not tavily_available)

        return {
            "gemini_available": gemini_available,
            "gemini_status": "Ready (gemini-2.5-flash)" if gemini_available else "Not configured (Using MockPlanner)",
            "tavily_available": tavily_available,
            "tavily_status": "Ready (Live Search)" if tavily_available else "Not configured (Using Demo Fixtures)",
            "database_ok": db_ok,
            "demo_fixtures_present": demo_present,
            "is_demo_mode": is_demo_mode,
            "banner_required": is_demo_mode,
            "banner_text": "⚠️ DEMO DATA - NOT LIVE SEARCH (data/internships_demo.json)",
        }

    def list_recent_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List past discovery runs from SQLite."""
        conn = open_db_connection(self.db_path)
        try:
            return list_runs(conn, limit=limit)
        finally:
            conn.close()

    def verify_run(self, run_id: str, target_spreadsheet_path: str = "offers.xlsx") -> VerificationReport:
        """Execute independent disk and claim verification for a run."""
        conn = open_db_connection(self.db_path)
        try:
            steps = get_steps_for_run(conn, run_id)
            step_states = [s["state"] for s in steps]
        finally:
            conn.close()

        verifier = IndependentVerifier(db_path=self.db_path, artifacts_dir=self.artifacts_dir)
        sp_path = self.artifacts_dir / target_spreadsheet_path
        sheet_to_check = sp_path if sp_path.exists() else None

        drafts_dir = self.artifacts_dir / "drafts"
        draft_to_check = None
        if drafts_dir.exists():
            eml_files = list(drafts_dir.glob("*.eml"))
            if eml_files:
                draft_to_check = eml_files[0]

        claims = [
            "Internship listings extracted from verified sources",
            "No duplicate internship keys in spreadsheet",
            "Stipend and eligibility requirements documented",
        ]

        return verifier.generate_verification_report(
            run_id=run_id,
            step_states=step_states,
            spreadsheet_path=sheet_to_check,
            draft_path=draft_to_check,
            claims=claims,
        )

    def get_run_details(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full details for a run from SQLite."""
        conn = open_db_connection(self.db_path)
        try:
            run = get_run(conn, run_id)
            if not run:
                return None

            steps = get_steps_for_run(conn, run_id)
            tool_attempts = get_tool_attempts_for_run(conn, run_id)
            evidence = get_evidence_for_run(conn, run_id)
            approvals = get_approvals_for_run(conn, run_id)
            artifacts = get_artifacts_for_run(conn, run_id)
            events = get_events_for_run(conn, run_id)

            target_path = "offers.xlsx"
            if run.get("metadata") and run["metadata"].get("output_path"):
                target_path = run["metadata"]["output_path"]

            report = self.verify_run(run_id=run_id, target_spreadsheet_path=target_path)

            return {
                "run": run,
                "steps": steps,
                "tool_attempts": tool_attempts,
                "evidence": evidence,
                "approvals": approvals,
                "artifacts": artifacts,
                "events": events,
                "report": report.model_dump(),
            }
        finally:
            conn.close()

    def simulate_send(self, run_id: str, draft_id: Optional[str] = None) -> Dict[str, Any]:
        """Attempt a 'simulate send' action.

        Strict policy requirement: Sending is permanently BLOCKED in MVP.
        This must log a POLICY_BLOCK event and return the exact blocked status.
        """
        conn = open_db_connection(self.db_path)
        try:
            evt_id = f"evt_send_block_{uuid.uuid4().hex[:8]}"
            block_message = (
                "Action 'send_email' is strictly prohibited by OFFERHUNT safety policy. "
                "MVP is confined to local drafting; external transmission is disabled."
            )
            record_event(
                conn=conn,
                event_id=evt_id,
                run_id=run_id,
                step_id=None,
                event_type="POLICY_BLOCK",
                severity="CRITICAL",
                payload={"action": "simulate_send", "draft_id": draft_id, "reason": block_message},
            )
            return {
                "allowed": False,
                "status": "POLICY_BLOCK",
                "message": block_message,
                "event_id": evt_id,
            }
        finally:
            conn.close()

    def approve_step(self, approval_id: str) -> None:
        """Operator approves a pending boundary action."""
        conn = open_db_connection(self.db_path)
        try:
            update_approval_status(conn, approval_id=approval_id, status="APPROVED", response_notes="Approved via UI")
        finally:
            conn.close()

    def reject_step(self, approval_id: str) -> None:
        """Operator rejects a pending boundary action."""
        conn = open_db_connection(self.db_path)
        try:
            update_approval_status(conn, approval_id=approval_id, status="REJECTED", response_notes="Rejected via UI")
        finally:
            conn.close()

    async def execute_discovery_run(
        self,
        goal: str,
        constraints: Dict[str, Any],
        output_path: str = "offers.xlsx",
        run_budget: Optional[Dict[str, Any]] = None,
        persona: Optional[Dict[str, Any]] = None,
        force_demo: bool = False,
    ) -> str:
        """Orchestrate a complete discovery run from goal to verified report.

        Steps:
        1. Validate inputs and output path.
        2. Create run record in SQLite.
        3. Plan steps with GeminiPlanner or MockPlanner.
        4. Validate plan against safety policy.
        5. Persist steps in SQLite.
        6. Execute steps using StepExecutor.
        7. Run IndependentVerifier and transition final run status.
        """
        conn = open_db_connection(self.db_path)
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        try:
            # 1. Validate output path
            resolved_output = validate_and_resolve_path(output_path, allowed_dir=self.artifacts_dir)
            target_rel_path = resolved_output.name

            # 2. Inspect diagnostics to determine demo data flag
            diagnostics = self.get_diagnostics()
            is_demo = force_demo or diagnostics["is_demo_mode"]

            user_persona = persona or DEFAULT_PERSONA
            budget_config = run_budget or {"max_tool_calls": 10, "max_duration_seconds": 120}

            run_metadata = {
                "goal": goal,
                "constraints": constraints,
                "output_path": target_rel_path,
                "persona": user_persona,
                "budget": budget_config,
                "is_demo_data": is_demo,
            }

            create_run(conn, run_id=run_id, status="RUNNING", metadata=run_metadata)
            record_event(
                conn=conn,
                event_id=f"evt_start_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                event_type="RUN_STARTED",
                severity="INFO",
                payload={"goal": goal, "is_demo": is_demo},
            )

            # 3. Generate Plan
            plan: GoalPlan
            if not is_demo and diagnostics["gemini_available"]:
                planner = GeminiPlanner(
                    api_key=os.environ["GEMINI_API_KEY"],
                    model_name="gemini-2.5-flash",
                )
                prompt = (
                    f"Goal: {goal}. Constraints: Role={constraints.get('role')}, "
                    f"Location={constraints.get('location')}, MaxResults={constraints.get('max_results', 5)}. "
                    f"Target file: {target_rel_path}. Persona: {user_persona.get('name')}, {user_persona.get('education')}."
                )
                plan = await planner.create_plan(prompt, CATALOG_TOOLS)
            else:
                # Use MockPlanner with rich deterministic steps
                mock_p = MockPlanner()
                full_prompt = f"{goal} - Role: {constraints.get('role')}, Location: {constraints.get('location')}"
                plan = await mock_p.create_plan(full_prompt, CATALOG_TOOLS)

            # 4. Validate plan against safety policy
            validate_dag_acyclic(plan.steps)
            validate_plan_against_policy(plan, CATALOG_TOOLS)

            # 5. Persist steps to SQLite
            persist_plan_and_catalog(conn, run_id, plan, CATALOG_TOOLS)

            record_event(
                conn=conn,
                event_id=f"evt_plan_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                event_type="PLAN_VALIDATED",
                severity="INFO",
                payload={"step_count": len(plan.steps), "planner": "MockPlanner" if is_demo else "GeminiPlanner"},
            )

            # 6. Execute steps with StepExecutor
            mcp_client = DirectInProcessMCPClient(
                artifacts_dir=self.artifacts_dir,
                db_path=self.db_path,
                demo_fixtures_path=self.demo_fixtures_path if is_demo else None,
            )
            exec_cfg = ExecutorConfig(
                tool_timeout_seconds=float(budget_config.get("tool_timeout_seconds", 30.0)),
                run_timeout_seconds=float(budget_config.get("max_duration_seconds", 120.0)),
                max_tool_calls=int(budget_config.get("max_tool_calls", 10)),
                output_dir=self.artifacts_dir,
            )
            executor = StepExecutor(db_conn=conn, mcp_client=mcp_client, config=exec_cfg)

            for step in plan.steps:
                try:
                    await executor.execute_step(run_id=run_id, step=step)
                except StepExecutionError as step_err:
                    record_event(
                        conn=conn,
                        event_id=f"evt_step_err_{uuid.uuid4().hex[:8]}",
                        run_id=run_id,
                        step_id=step.step_id,
                        event_type="STEP_EXECUTION_ERROR",
                        severity="ERROR",
                        payload={"error": str(step_err)},
                    )
                    break

            # 7. Independent verification & claim validation
            report = self.verify_run(run_id=run_id, target_spreadsheet_path=target_rel_path)
            final_status = report.final_status
            db_status = "COMPLETED" if final_status in ("VERIFIED", "PARTIAL") else ("CANCELLED" if final_status == "CANCELLED" else "FAILED")
            update_run_status(conn, run_id=run_id, status=db_status, completed=True)

            if final_status == "VERIFIED":
                for step in plan.steps:
                    st_row = get_step(conn, step.step_id)
                    if st_row and st_row["state"] == "EXECUTED":
                        update_step_state(
                            conn,
                            step_id=step.step_id,
                            state="VERIFIED",
                            execution_status="COMPLETED",
                            verification_status="PASSED",
                        )

            verified_claims_count = sum(1 for c in report.claims if c.verified)
            unestablished_claims_count = sum(1 for c in report.claims if not c.verified)

            record_event(
                conn=conn,
                event_id=f"evt_verified_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                event_type="RUN_COMPLETED",
                severity="INFO" if final_status == "VERIFIED" else "WARNING",
                payload={
                    "final_status": final_status,
                    "verified_claims_count": verified_claims_count,
                    "unestablished_claims_count": unestablished_claims_count,
                },
            )

            return run_id

        except Exception as exc:
            update_run_status(conn, run_id=run_id, status="FAILED", completed=True)
            record_event(
                conn=conn,
                event_id=f"evt_fatal_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                event_type="RUN_FATAL_ERROR",
                severity="CRITICAL",
                payload={"error": str(exc)},
            )
            raise
        finally:
            conn.close()
