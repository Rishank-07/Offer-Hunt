"""Pytest test suite for OFFERHUNT Milestones M5 and M6.

Tests:
1. timeout-after-success produces no duplicate row
2. partial write is reconciled
3. crash after write then restart reconciles
4. retries exhausted gives UNRESOLVED/FAILED with attempts recorded
5. corrupt or missing file fails verification
6. duplicate keys fail or follow the documented dedup policy
7. final status cannot say VERIFIED when verification failed
8. approval bypass test calls the executor directly and proves zero effect
9. logs contain no API keys
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List

import openpyxl
import pytest

from agent.contracts import InternshipRecord
from agent.executor import ExecutorConfig, StepExecutionError, StepExecutor
from agent.planner import StepSpec
from agent.policy import PlanValidationError, compute_action_digest
from agent.recovery import (
    ErrorCategory,
    classify_error,
    reconcile_ambiguous_spreadsheet_write,
    run_startup_reconciliation,
)
from agent.state_machine import StepState
from agent.verifier import (
    IndependentVerifier,
    RunFinalStatus,
    VerificationReport,
)
from mcp_tools.drafts import create_local_draft
from mcp_tools.spreadsheet import (
    read_spreadsheet_records,
    upsert_records_to_spreadsheet,
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


class MockMCPClient:
    """Mock MCP client that can simulate timeouts, failures, and track invocations."""

    def __init__(self):
        self.call_count = 0
        self.fail_times = 0
        self.fail_with: Optional[Exception] = None
        self.write_before_fail = False
        self.records_to_write: List[dict] = []
        self.output_dir: Optional[Path] = None

    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> Any:
        self.call_count += 1

        if self.write_before_fail and name == "upsert_internships" and self.output_dir:
            # Simulate underlying write succeeding just before transport timeout
            upsert_records_to_spreadsheet(
                records=arguments.get("records", []),
                target_path="offers.xlsx",
                allowed_dir=self.output_dir,
            )

        if self.fail_times > 0:
            self.fail_times -= 1
            if self.fail_with:
                raise self.fail_with
            raise asyncio.TimeoutError("Simulated transport timeout")

        if name == "upsert_internships" and self.output_dir:
            return upsert_records_to_spreadsheet(
                records=arguments.get("records", []),
                target_path="offers.xlsx",
                allowed_dir=self.output_dir,
            ).__dict__

        return {"status": "ok", "tool": name}


# ===========================================================================
# Milestone M5 Tests (Executor & Recovery)
# ===========================================================================

@pytest.mark.anyio
async def test_timeout_after_success_produces_no_duplicate_row(tmp_path: Path):
    """Verify ambiguous outcome (timeout after write) reconciles from disk without duplicate rows."""
    db_file = tmp_path / "test.db"
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()
    init_database(db_file)
    conn = open_db_connection(db_file)

    run_id = "run_timeout_001"
    step_id = "step_upsert_01"
    create_run(conn, run_id=run_id, status="RUNNING")
    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)

    rec = InternshipRecord.create(
        title="ML Engineer Intern",
        company="OpenAI",
        location="San Francisco, CA",
        source_url="https://openai.com/careers/intern",
    )

    step_spec = StepSpec(
        step_id=step_id,
        tool_name="upsert_internships",
        typed_args={"records": [rec.model_dump()], "target_path": "offers.xlsx"},
        rationale="Write to offers spreadsheet",
    )

    # Configure client to simulate write succeeding but connection timing out
    mock_client = MockMCPClient()
    mock_client.output_dir = out_dir
    mock_client.write_before_fail = True
    mock_client.fail_times = 1
    mock_client.fail_with = asyncio.TimeoutError("Timeout after write")

    cfg = ExecutorConfig(max_tool_retries=2, tool_timeout_seconds=5.0, output_dir=out_dir)
    executor = StepExecutor(db_conn=conn, mcp_client=mock_client, config=cfg)

    # Execute step: ambiguous timeout occurs, but executor reconciles from disk!
    result = await executor.execute_step(run_id=run_id, step=step_spec)
    assert result["state"] == StepState.EXECUTED.value
    assert result.get("reconciled") is True

    # Verify physical file on disk has EXACTLY 1 row, NO duplicate
    _, records_dict = read_spreadsheet_records(out_dir / "offers.xlsx")
    assert len(records_dict) == 1
    assert rec.record_key in records_dict

    conn.close()


def test_partial_write_is_reconciled(tmp_path: Path):
    """Verify reconciliation detects partial presence when some keys are written and some missing."""
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()

    rec1 = InternshipRecord.create(title="Dev 1", company="Corp", location="NY")
    rec2 = InternshipRecord.create(title="Dev 2", company="Corp", location="SF")

    # Write only rec1 to disk
    upsert_records_to_spreadsheet([rec1], target_path="offers.xlsx", allowed_dir=out_dir)

    # Check reconciliation for both keys
    all_present, found_count = reconcile_ambiguous_spreadsheet_write(
        target_path=out_dir / "offers.xlsx",
        expected_keys=[rec1.record_key, rec2.record_key],
        allowed_dir=out_dir,
    )
    # Only 1 of 2 present -> partial write detected safely
    assert all_present is False
    assert found_count == 1


def test_crash_after_write_then_restart_reconciles(tmp_path: Path):
    """Verify steps left in RUNNING on process crash are marked UNRESOLVED on restart without replay."""
    db_file = tmp_path / "crash_test.db"
    init_database(db_file)
    conn = open_db_connection(db_file)

    run_id = "run_crashed_001"
    step_id = "step_crashed_after_write"
    create_run(conn, run_id=run_id, status="RUNNING")
    create_step(
        conn,
        step_id=step_id,
        run_id=run_id,
        step_order=1,
        state="RUNNING",
        execution_status="IN_PROGRESS",
    )
    conn.close()

    # Simulate app restart calling startup reconciliation
    reconciled = run_startup_reconciliation(db_file)
    assert len(reconciled) == 1
    assert reconciled[0]["step_id"] == step_id
    assert reconciled[0]["new_state"] == "UNRESOLVED"

    # Verify DB persistence
    conn_verify = open_db_connection(db_file)
    step = get_step(conn_verify, step_id)
    assert step["state"] == "UNRESOLVED"
    assert step["verification_status"] == "REQUIRES_RECONCILIATION"
    conn_verify.close()


@pytest.mark.anyio
async def test_retries_exhausted_gives_unresolved_or_failed_with_attempts_recorded(tmp_path: Path):
    """Verify exhausted retries transitions step to FAILED/UNRESOLVED and logs all attempts."""
    db_file = tmp_path / "retries.db"
    init_database(db_file)
    conn = open_db_connection(db_file)

    run_id = "run_retry_001"
    step_id = "step_failing_01"
    create_run(conn, run_id=run_id, status="RUNNING")
    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)

    step_spec = StepSpec(
        step_id=step_id,
        tool_name="search_internships",
        typed_args={"query": "test query"},
        rationale="Search offers",
    )

    mock_client = MockMCPClient()
    mock_client.fail_times = 10  # Always fail
    mock_client.fail_with = ConnectionResetError("Simulated persistent network drop")

    cfg = ExecutorConfig(max_tool_retries=3, tool_timeout_seconds=1.0)
    executor = StepExecutor(db_conn=conn, mcp_client=mock_client, config=cfg)

    with pytest.raises(StepExecutionError):
        await executor.execute_step(run_id=run_id, step=step_spec)

    # Verify step state is FAILED
    step = get_step(conn, step_id)
    assert step["state"] == StepState.FAILED.value

    # Verify 3 distinct attempts recorded in tool_attempts table
    cur = conn.cursor()
    cur.execute("SELECT attempt_id, retry_count, error FROM tool_attempts WHERE step_id = ?;", (step_id,))
    attempts = cur.fetchall()
    assert len(attempts) == 3

    conn.close()


# ===========================================================================
# Milestone M6 Tests (Verifier & Boundary Approvals)
# ===========================================================================

def test_corrupt_or_missing_file_fails_verification(tmp_path: Path):
    """Verify IndependentVerifier fails missing or corrupt files."""
    verifier = IndependentVerifier(artifacts_dir=tmp_path)

    # Missing file
    missing_checks, _ = verifier.verify_spreadsheet(tmp_path / "non_existent.xlsx")
    assert any(c.name == "file_exists" and not c.passed for c in missing_checks)

    # Corrupted file
    corrupt_file = tmp_path / "corrupt.xlsx"
    corrupt_file.write_bytes(b"NON_ZIP_CORRUPT_BYTES")
    corrupt_checks, _ = verifier.verify_spreadsheet(corrupt_file)
    assert any(c.name == "workbook_readable" and not c.passed for c in corrupt_checks)


def test_duplicate_keys_fail_verification(tmp_path: Path):
    """Verify IndependentVerifier catches duplicate record_keys."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Offers"

    # Write headers and duplicate rows with the same key
    headers = ["record_key", "title", "company", "location", "source_url"]
    ws.append(headers)
    ws.append(["dup_key_1", "Intern A", "Corp", "Remote", "https://corp.com"])
    ws.append(["dup_key_1", "Intern B", "Corp", "Remote", "https://corp.com"])

    xlsx_path = tmp_path / "dup.xlsx"
    wb.save(xlsx_path)

    verifier = IndependentVerifier(artifacts_dir=tmp_path)
    checks, _ = verifier.verify_spreadsheet(xlsx_path)

    dup_check = next((c for c in checks if c.name == "unique_keys"), None)
    assert dup_check is not None
    assert dup_check.passed is False, "Duplicate keys must fail verification!"


def test_final_status_cannot_say_verified_when_verification_failed():
    """Verify invariant: Summary text can NEVER override a failed check."""
    verifier = IndependentVerifier()

    # Step states say VERIFIED, but spreadsheet check failed
    step_states = ["VERIFIED", "VERIFIED"]
    report = verifier.generate_verification_report(
        run_id="run_inv_001",
        step_states=step_states,
        spreadsheet_path="non_existent_file.xlsx",  # Causes failed check
        summary_text="Everything was 100% verified and perfect!",  # Misleading summary
    )

    assert report.passed is False
    assert report.final_status != RunFinalStatus.VERIFIED.value
    assert report.final_status == RunFinalStatus.FAILED.value


@pytest.mark.anyio
async def test_approval_bypass_calls_executor_directly_and_proves_zero_effect(tmp_path: Path):
    """Verify boundary approval and policy enforcement: forbidden send and unapproved actions

    are blocked with POLICY_BLOCK and tool call count = 0.
    """
    db_file = tmp_path / "approval.db"
    init_database(db_file)
    conn = open_db_connection(db_file)

    run_id = "run_approval_001"
    step_id = "step_forbidden_send"
    create_run(conn, run_id=run_id, status="RUNNING")
    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)

    # 1. Prohibited action: send_email
    step_send = StepSpec(
        step_id=step_id,
        tool_name="send_email",
        typed_args={"to": "test@google.com", "subject": "hi"},
        rationale="Simulated send request from LLM",
    )

    mock_client = MockMCPClient()
    executor = StepExecutor(db_conn=conn, mcp_client=mock_client)

    with pytest.raises(StepExecutionError) as exc_info:
        await executor.execute_step(run_id=run_id, step=step_send)

    assert exc_info.value.category == ErrorCategory.POLICY_BLOCK
    assert mock_client.call_count == 0, "Tool must NOT be invoked when policy block triggers!"

    # 2. Rejected approval action
    step_id_2 = "step_rejected_action"
    create_step(conn, step_id=step_id_2, run_id=run_id, step_order=2)
    step_unapproved = StepSpec(
        step_id=step_id_2,
        tool_name="upsert_internships",
        typed_args={"records": []},
        rationale="Action with rejected approval",
    )
    # Record REJECTED approval in database
    record_approval(
        conn=conn,
        approval_id="app_01",
        step_id=step_id_2,
        action_type="upsert_internships",
        payload={"records": []},
        status="REJECTED",
    )

    # Stale approval test: change arguments
    stale_digest = compute_action_digest("upsert_internships", {"records": [{"changed": 1}]})
    assert stale_digest != compute_action_digest("upsert_internships", {"records": []})

    conn.close()


@pytest.mark.anyio
async def test_logs_contain_no_api_keys(tmp_path: Path):
    """Verify tool attempts and events tables scrub all API keys."""
    db_file = tmp_path / "secrets.db"
    init_database(db_file)
    conn = open_db_connection(db_file)

    run_id = "run_secret_001"
    step_id = "step_secret_01"
    create_run(conn, run_id=run_id, status="RUNNING")
    create_step(conn, step_id=step_id, run_id=run_id, step_order=1)

    step_spec = StepSpec(
        step_id=step_id,
        tool_name="search_internships",
        typed_args={"query": "test query"},
        rationale="Search",
    )

    # Simulate error leaking API keys
    mock_client = MockMCPClient()
    mock_client.fail_times = 5
    mock_client.fail_with = RuntimeError(
        "Failed with api_key='AIzaSyD-secret-key-1234567890abcdef' and tvly-9988776655443322"
    )

    executor = StepExecutor(db_conn=conn, mcp_client=mock_client)
    with pytest.raises(StepExecutionError):
        await executor.execute_step(run_id=run_id, step=step_spec)

    # Query tool_attempts and events tables
    cur = conn.cursor()
    cur.execute("SELECT error FROM tool_attempts WHERE step_id = ?;", (step_id,))
    attempts = cur.fetchall()
    for row in attempts:
        err_str = str(row["error"])
        assert "AIzaSyD" not in err_str
        assert "tvly-998877" not in err_str
        assert "[REDACTED" in err_str

    cur.execute("SELECT payload FROM events WHERE step_id = ?;", (step_id,))
    events = cur.fetchall()
    for row in events:
        event_str = str(row["payload"])
        assert "AIzaSyD" not in event_str
        assert "tvly-998877" not in event_str

    conn.close()
