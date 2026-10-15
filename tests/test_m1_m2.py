"""Pytest test suite for OFFERHUNT Milestones M1 and M2.

All tests utilize pytest tmp_path fixture exclusively.
"""

from __future__ import annotations

import openpyxl
import pytest
from pathlib import Path

from agent.contracts import (
    ErrorModel,
    InternshipRecord,
    canonicalize_url,
    compute_record_key,
    normalize_text,
)
from agent.state_machine import (
    ExecutionStatus,
    IllegalStateTransitionError,
    StepState,
    StepStateMachine,
    VerificationStatus,
    reconcile_interrupted_steps,
    transition_step,
)
from mcp_tools.drafts import (
    DraftSecurityError,
    create_local_draft,
    validate_recipient_email,
)
from mcp_tools.spreadsheet import (
    SpreadsheetCorruptError,
    SpreadsheetSecurityError,
    read_spreadsheet_records,
    upsert_records_to_spreadsheet,
    validate_and_resolve_path,
)
from storage.database import (
    check_database_integrity,
    create_artifact,
    create_run,
    create_step,
    get_run,
    get_step,
    init_database,
    open_db_connection,
    record_evidence,
    record_metric,
    update_step_state,
)


# ===========================================================================
# Milestone M1 Tests
# ===========================================================================

def test_record_key_stability_and_location_sensitivity():
    """Verify record_key is deterministic, insensitive to tracking query params,

    and sensitive to differences in location.
    """
    url1 = "https://example-careers.org/jobs/101?utm_source=twitter&utm_medium=cpc&ref=campaign"
    url2 = "https://example-careers.org/jobs/101"

    key1, is_canon1 = compute_record_key(
        title="Software Engineer Intern",
        company="Acme Corp",
        location="Remote",
        source_url=url1,
    )

    key2, is_canon2 = compute_record_key(
        title="software engineer intern",
        company="  ACME CORP  ",
        location="remote",
        source_url=url2,
    )

    # Tracking parameters removed, whitespace collapsed, case lowered -> keys identical
    assert is_canon1 is True
    assert is_canon2 is True
    assert key1 == key2
    assert len(key1) == 64  # SHA-256 length

    # Different location produces a DIFFERENT key
    key_nyc, _ = compute_record_key(
        title="Software Engineer Intern",
        company="Acme Corp",
        location="New York, NY",
        source_url=url2,
    )
    assert key1 != key_nyc, "Different locations must yield different record_keys!"

    # Missing URL fallback
    key_fallback, is_canon_fallback = compute_record_key(
        title="Software Engineer Intern",
        company="Acme Corp",
        location="Remote",
        source_url=None,
    )
    assert is_canon_fallback is False
    assert len(key_fallback) == 64
    assert key_fallback != key1


def test_internship_record_factory():
    """Verify InternshipRecord helper constructor and validation."""
    rec = InternshipRecord.create(
        title="ML Research Intern",
        company="DeepMind",
        location="London, UK",
        source_url="https://deepmind.google/careers/interns?ref=share",
        stipend="Competitive",
        eligibility="Enrolled in MS/PhD",
    )
    assert rec.record_key is not None
    assert rec.company == "DeepMind"
    assert rec.location == "London, UK"
    assert rec.confidence == 1.0
    assert "ref=share" not in (rec.source_url or "")

    # Fallback record
    rec_fallback = InternshipRecord.create(
        title="ML Research Intern",
        company="DeepMind",
        location="London, UK",
        source_url=None,
    )
    assert rec_fallback.provenance.get("fallback_key") is True
    assert rec_fallback.confidence <= 0.75


def test_spreadsheet_repeated_upsert_no_duplicates(tmp_path: Path):
    """Verify repeated upsert of the same record adds no duplicate."""
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()
    target_xlsx = "internships.xlsx"

    rec1 = InternshipRecord.create(
        title="Frontend Intern",
        company="Tech Giant",
        location="San Francisco, CA",
        source_url="https://techgiant.com/jobs/fe",
        stipend="$45/hr",
    )

    # 1. First upsert: file created, 1 inserted
    res1 = upsert_records_to_spreadsheet([rec1], target_path=target_xlsx, allowed_dir=out_dir)
    assert res1.inserted == 1
    assert res1.updated == 0
    assert res1.unchanged == 0
    assert len(res1.sha256) == 64

    # Read back directly with openpyxl to verify physical row count
    headers, rows_dict = read_spreadsheet_records(out_dir / target_xlsx)
    assert len(rows_dict) == 1
    assert rec1.record_key in rows_dict

    # 2. Repeated upsert with the EXACT same record: 0 inserted, 0 updated, 1 unchanged
    res2 = upsert_records_to_spreadsheet([rec1], target_path=target_xlsx, allowed_dir=out_dir)
    assert res2.inserted == 0
    assert res2.updated == 0
    assert res2.unchanged == 1

    headers, rows_dict = read_spreadsheet_records(out_dir / target_xlsx)
    assert len(rows_dict) == 1, "Duplicate row was incorrectly appended!"

    # 3. Upsert with updated field (e.g. stipend changed)
    rec1_updated = InternshipRecord.create(
        title="Frontend Intern",
        company="Tech Giant",
        location="San Francisco, CA",
        source_url="https://techgiant.com/jobs/fe",
        stipend="$50/hr",  # updated stipend
    )
    res3 = upsert_records_to_spreadsheet([rec1_updated], target_path=target_xlsx, allowed_dir=out_dir)
    assert res3.inserted == 0
    assert res3.updated == 1
    assert res3.unchanged == 0

    headers, rows_dict = read_spreadsheet_records(out_dir / target_xlsx)
    assert len(rows_dict) == 1
    assert rows_dict[rec1.record_key][1]["stipend"] == "$50/hr"


def test_spreadsheet_corrupt_or_missing_fails_safely(tmp_path: Path):
    """Verify missing xlsx creates safely and corrupt xlsx raises SpreadsheetCorruptError safely."""
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()

    # Missing file creates cleanly
    missing_file = "non_existent.xlsx"
    rec = InternshipRecord.create(
        title="DevOps Intern",
        company="CloudOps",
        location="Austin, TX",
        source_url="https://cloudops.io/careers",
    )
    res = upsert_records_to_spreadsheet([rec], target_path=missing_file, allowed_dir=out_dir)
    assert res.inserted == 1
    assert (out_dir / missing_file).exists()

    # Corrupted file: create non-zip garbage bytes
    corrupt_file = out_dir / "corrupted.xlsx"
    corrupt_file.write_bytes(b"CORRUPTED_NON_ZIP_BYTES_1234567890")

    with pytest.raises(SpreadsheetCorruptError) as exc_info:
        upsert_records_to_spreadsheet([rec], target_path=corrupt_file.name, allowed_dir=out_dir)
    assert "Failed to read existing spreadsheet" in str(exc_info.value)


def test_spreadsheet_path_traversal_rejected(tmp_path: Path):
    """Verify attempts to escape allowed_dir via traversal or bad extensions are rejected."""
    out_dir = tmp_path / "safe_dir"
    out_dir.mkdir()

    rec = InternshipRecord.create(
        title="Security Intern",
        company="SecCorp",
        location="Remote",
    )

    # 1. Directory traversal attempt
    with pytest.raises(SpreadsheetSecurityError) as exc_traversal:
        upsert_records_to_spreadsheet(
            [rec], target_path="../../outside.xlsx", allowed_dir=out_dir
        )
    assert "traverses outside allowed directory" in str(exc_traversal.value)

    # 2. Disallowed extension
    with pytest.raises(SpreadsheetSecurityError) as exc_ext:
        upsert_records_to_spreadsheet(
            [rec], target_path="data.csv", allowed_dir=out_dir
        )
    assert "Unsupported spreadsheet extension" in str(exc_ext.value)


def test_drafts_never_send_and_reject_fabricated_emails(tmp_path: Path):
    """Verify drafts are saved locally with DRAFTED state, never send, and reject fake emails."""
    drafts_dir = tmp_path / "drafts"

    # 1. Valid recipient
    draft = create_local_draft(
        recipient="university-recruiting@uber.com",
        subject="Internship Application - Software Engineer",
        body="Dear Hiring Team,\nI am writing to express my strong interest...",
        drafts_dir=drafts_dir,
    )
    assert draft.state == "DRAFTED"
    assert Path(draft.eml_path).exists()
    assert Path(draft.txt_path).exists()

    # Check contents of .eml
    eml_content = Path(draft.eml_path).read_text(encoding="utf-8", errors="ignore")
    assert "X-Draft-State: DRAFTED" in eml_content
    assert "X-OfferHunt-Never-Send: true" in eml_content
    assert "university-recruiting@uber.com" in eml_content

    # 2. Fabricated / dummy emails rejection
    fabricated_emails = [
        "john.doe@example.com",
        "recruiter@test.com",
        "hr@placeholder.com",
        "fake@company.com",
        "none@uber.com",
        "dummy@gmail.com",
        "invalid-email-address",
        "missing-at-sign.com",
    ]

    for fake_email in fabricated_emails:
        with pytest.raises(DraftSecurityError):
            create_local_draft(
                recipient=fake_email,
                subject="Test",
                body="Test body",
                drafts_dir=drafts_dir,
            )


# ===========================================================================
# Milestone M2 Tests
# ===========================================================================

def test_db_state_survives_closing_and_reopening(tmp_path: Path):
    """Verify SQLite persistence: tables, constraints, data survive full connection closure and reopening."""
    db_file = tmp_path / "test_offerhunt.db"
    init_database(db_file)

    # Step 1: Open connection, insert run, step, artifact, evidence, metric
    conn1 = open_db_connection(db_file)
    run_id = "run_2026_001"
    step_id = "step_2026_001_1"

    create_run(conn1, run_id=run_id, status="RUNNING", metadata={"mode": "test"})
    create_step(
        conn1,
        step_id=step_id,
        run_id=run_id,
        step_order=1,
        state="RUNNING",
        execution_status="IN_PROGRESS",
    )
    record_evidence(
        conn1,
        evidence_id="evi_01",
        step_id=step_id,
        evidence_type="web_snippet",
        content="Sample offer snippet",
        source_url="https://careers.google.com",
    )
    record_metric(
        conn1,
        metric_id="met_01",
        run_id=run_id,
        metric_name="extraction_latency_ms",
        metric_value=245.5,
    )

    # Close connection completely
    conn1.close()

    # Step 2: Open a fresh brand-new connection and verify all records persist
    conn2 = open_db_connection(db_file)
    try:
        run = get_run(conn2, run_id)
        assert run is not None
        assert run["run_id"] == run_id
        assert run["status"] == "RUNNING"
        assert run["metadata"] == {"mode": "test"}

        step = get_step(conn2, step_id)
        assert step is not None
        assert step["step_id"] == step_id
        assert step["state"] == "RUNNING"
        assert step["execution_status"] == "IN_PROGRESS"

        # Check evidence and metric
        cur = conn2.cursor()
        cur.execute("SELECT content FROM evidence WHERE step_id = ?;", (step_id,))
        evi_row = cur.fetchone()
        assert evi_row is not None
        assert evi_row["content"] == "Sample offer snippet"

        cur.execute("SELECT metric_value FROM metrics_runs WHERE run_id = ?;", (run_id,))
        met_row = cur.fetchone()
        assert met_row is not None
        assert abs(met_row["metric_value"] - 245.5) < 1e-4
    finally:
        conn2.close()


def test_sqlite_integrity_check(tmp_path: Path):
    """Verify SQLite integrity check returns ok."""
    db_file = tmp_path / "integrity_test.db"
    init_database(db_file)

    is_ok, results = check_database_integrity(db_file)
    assert is_ok is True
    assert results == ["ok"]


def test_state_machine_illegal_transition_rejected():
    """Verify state machine strictly enforces valid transitions and rejects illegal transitions."""
    sm = StepStateMachine(
        current_state=StepState.READY,
        execution_status=ExecutionStatus.NOT_STARTED,
        verification_status=VerificationStatus.UNVERIFIED,
    )

    # Illegal transition: READY -> VERIFIED
    with pytest.raises(IllegalStateTransitionError) as exc_info:
        sm.transition_to(StepState.VERIFIED)
    assert "cannot transition from READY to VERIFIED" in str(exc_info.value)

    # Illegal transition: READY -> UNRESOLVED
    with pytest.raises(IllegalStateTransitionError):
        sm.transition_to(StepState.UNRESOLVED)

    # Legal progression: READY -> RUNNING -> EXECUTED -> VERIFIED
    sm.transition_to(StepState.RUNNING)
    assert sm.current_state == StepState.RUNNING
    assert sm.execution_status == ExecutionStatus.IN_PROGRESS

    sm.transition_to(StepState.EXECUTED)
    assert sm.current_state == StepState.EXECUTED
    assert sm.execution_status == ExecutionStatus.COMPLETED

    sm.transition_to(StepState.VERIFIED)
    assert sm.current_state == StepState.VERIFIED
    assert sm.verification_status == VerificationStatus.PASSED


def test_startup_reconciliation_of_interrupted_running_steps(tmp_path: Path):
    """Verify startup check detects interrupted RUNNING steps and marks them for reconciliation."""
    db_file = tmp_path / "recon_test.db"
    init_database(db_file)

    conn = open_db_connection(db_file)
    run_id = "run_interrupted_001"
    step1_id = "step_clean_1"
    step2_id = "step_crashed_2"

    create_run(conn, run_id=run_id, status="RUNNING")
    # Clean step
    create_step(
        conn,
        step_id=step1_id,
        run_id=run_id,
        step_order=1,
        state="VERIFIED",
        execution_status="COMPLETED",
        verification_status="PASSED",
    )
    # Interrupted step in RUNNING state
    create_step(
        conn,
        step_id=step2_id,
        run_id=run_id,
        step_order=2,
        state="RUNNING",
        execution_status="IN_PROGRESS",
        verification_status="UNVERIFIED",
    )
    conn.close()

    # Perform startup reconciliation check
    reconciled = reconcile_interrupted_steps(db_file)
    assert len(reconciled) == 1
    assert reconciled[0]["step_id"] == step2_id
    assert reconciled[0]["new_state"] == "UNRESOLVED"
    assert reconciled[0]["verification_status"] == "REQUIRES_RECONCILIATION"

    # Verify database persistence of the reconciled step
    conn_verify = open_db_connection(db_file)
    try:
        step2 = get_step(conn_verify, step2_id)
        assert step2["state"] == "UNRESOLVED"
        assert step2["execution_status"] == "FAILED"
        assert step2["verification_status"] == "REQUIRES_RECONCILIATION"

        # Check clean step was untouched
        step1 = get_step(conn_verify, step1_id)
        assert step1["state"] == "VERIFIED"
        assert step1["execution_status"] == "COMPLETED"

        # Check reconciliation audit event was recorded
        cur = conn_verify.cursor()
        cur.execute(
            "SELECT event_type, severity, payload FROM events WHERE step_id = ?;",
            (step2_id,),
        )
        evt = cur.fetchone()
        assert evt is not None
        assert evt["event_type"] == "STEP_STARTUP_RECONCILIATION"
        assert evt["severity"] == "WARNING"
    finally:
        conn_verify.close()
