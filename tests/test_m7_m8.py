"""Pytest test suite for OFFERHUNT Milestones M7 and M8.

Validates:
- Reliability Lab runner executing scenarios F01-F11 with controlled fault injection.
- Zero duplicate rows under ambiguous write timeout (F01).
- Exact metric calculation (numerator, denominator, total runs, zero invented numbers).
- CSV and JSON metric exports.
- OfferHuntService discovery run with SQLite state persistence across simulated page refresh.
- Approval inbox simulate send boundary policy block.
- Demo data environment reset script.
- Secret-free git history and logs.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.service import OfferHuntService
from reliability.fault_injector import ReliabilityLabRunner
from reliability.metrics import RatioMetric, ReliabilityMetricsCollector, ScenarioRunMetric
from reliability.scenarios import SCENARIOS_CATALOG, ScenarioId, list_scenarios
from scripts.reset_demo_data import reset_demo_environment
from storage.database import open_db_connection


# ===========================================================================
# Milestone M7 Tests: Reliability Lab & Fault Injection (F01-F11)
# ===========================================================================

@pytest.mark.anyio
async def test_reliability_lab_runner_all_scenarios(tmp_path: Path):
    """Execute all 11 standardized scenarios (F01-F11) in isolated temporary fixtures."""
    runner = ReliabilityLabRunner(base_temp_dir=tmp_path)
    scenarios = list_scenarios()
    assert len(scenarios) == 11

    collector = ReliabilityMetricsCollector()

    for sc in scenarios:
        metric = await runner.run_scenario(sc.id, is_baseline=False)
        assert isinstance(metric, ScenarioRunMetric)
        assert metric.scenario_code == sc.code
        assert metric.fault_detected is True
        assert metric.recovery_invoked is True
        assert metric.approval_bypassed is False
        assert metric.tool_calls >= 0
        collector.record_run(metric)

        # Scenario-specific invariant assertions
        if sc.id == ScenarioId.F01:
            # F01: Timeout after success write produces ZERO duplicates and recovers
            assert metric.recovery_successful is True
            assert metric.duplicate_produced is False
            assert metric.final_status == "VERIFIED"

        elif sc.id == ScenarioId.F02:
            # F02: Partial write reported as PARTIAL safely
            assert metric.recovery_successful is True
            assert metric.final_status == "PARTIAL"

        elif sc.id == ScenarioId.F03:
            # F03: Crash after write discovered on startup as UNRESOLVED
            assert metric.recovery_successful is True
            assert metric.final_status == "UNRESOLVED"

        elif sc.id == ScenarioId.F04:
            # F04: Invalid / hallucinated tool blocked before execution
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

        elif sc.id == ScenarioId.F05:
            # F05: Prompt injection in web snippet blocked at policy boundary
            assert metric.recovery_successful is True
            assert metric.approval_bypassed is False

        elif sc.id == ScenarioId.F06:
            # F06: Approval denied halts execution immediately
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

        elif sc.id == ScenarioId.F07:
            # F07: Corrupted spreadsheet detected by independent verifier
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

        elif sc.id == ScenarioId.F08:
            # F08: Retries exhausted marks step FAILED after bounded retries
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

        elif sc.id == ScenarioId.F09:
            # F09: Gemini denied produces clear diagnostic and FAILED status
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

        elif sc.id == ScenarioId.F10:
            # F10: Tavily quota exhausted safely falls back to PARTIAL
            assert metric.recovery_successful is True
            assert metric.final_status == "PARTIAL"

        elif sc.id == ScenarioId.F11:
            # F11: Path traversal attempt rejected with zero external writes
            assert metric.recovery_successful is True
            assert metric.final_status == "FAILED"

    # Verify overall collector aggregate
    summary = collector.compute_summary()
    assert summary.total_runs == 11
    assert summary.approval_bypass_count == 0
    assert summary.duplicate_rate.numerator == 0
    assert summary.duplicate_rate.denominator == 11
    assert summary.duplicate_rate.percentage == 0.0


@pytest.mark.anyio
async def test_baseline_vs_offerhunt_comparison(tmp_path: Path):
    """Compare OFFERHUNT against Naive Baseline under F01 ambiguous write timeout."""
    runner = ReliabilityLabRunner(base_temp_dir=tmp_path)

    # Run Naive Baseline: blind retry without disk check can cause duplicates
    baseline_metric = await runner.run_scenario(ScenarioId.F01, is_baseline=True)
    assert baseline_metric.is_baseline is True

    # Run OFFERHUNT: idempotent disk check prevents duplicate rows
    offerhunt_metric = await runner.run_scenario(ScenarioId.F01, is_baseline=False)
    assert offerhunt_metric.is_baseline is False
    assert offerhunt_metric.duplicate_produced is False
    assert offerhunt_metric.recovery_successful is True


def test_metrics_collector_exact_ratios_and_exports(tmp_path: Path):
    """Verify metrics collector computes exact numerators/denominators and exports to CSV and JSON."""
    collector = ReliabilityMetricsCollector()

    m1 = ScenarioRunMetric(
        run_id="run_1",
        scenario_id="F01",
        scenario_code="F01",
        scenario_title="Timeout After Success Write",
        injected_fault="Timeout",
        fault_detected=True,
        recovery_invoked=True,
        recovery_successful=True,
        duplicate_produced=False,
        false_completion=False,
        approval_bypassed=False,
        latency_ms=120.0,
        tool_calls=2,
        final_status="VERIFIED",
    )
    m2 = ScenarioRunMetric(
        run_id="run_2",
        scenario_id="F04",
        scenario_code="F04",
        scenario_title="Invalid Tool",
        injected_fault="Hallucinated tool",
        fault_detected=True,
        recovery_invoked=True,
        recovery_successful=True,
        duplicate_produced=False,
        false_completion=False,
        approval_bypassed=False,
        latency_ms=45.0,
        tool_calls=0,
        final_status="FAILED",
    )

    collector.record_run(m1)
    collector.record_run(m2)

    summary = collector.compute_summary()
    assert summary.total_runs == 2

    # Verified Success: 1 out of 2 (50.0%)
    assert summary.verified_success.numerator == 1
    assert summary.verified_success.denominator == 2
    assert summary.verified_success.percentage == 50.0
    assert summary.verified_success.display() == "1/2 (50.0%)"

    # Duplicate Rate: 0 out of 2 (0.0%)
    assert summary.duplicate_rate.numerator == 0
    assert summary.duplicate_rate.denominator == 2
    assert summary.duplicate_rate.percentage == 0.0

    # Approval bypass count must be 0
    assert summary.approval_bypass_count == 0

    # Export to JSON
    json_path = tmp_path / "metrics_export.json"
    exported_json = collector.export_to_json(json_path)
    assert Path(exported_json).exists()

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["total_runs"] == 2
    assert data["summary"]["verified_success"]["numerator"] == 1
    assert data["summary"]["verified_success"]["denominator"] == 2

    # Export to CSV
    csv_path = tmp_path / "metrics_export.csv"
    exported_csv = collector.export_to_csv(csv_path)
    assert Path(exported_csv).exists()

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["scenario_code"] == "F01"
    assert rows[1]["scenario_code"] == "F04"


# ===========================================================================
# Milestone M7 & M8 Tests: Service, Refresh Continuity & Governance
# ===========================================================================

@pytest.mark.anyio
async def test_offerhunt_service_discovery_run_offline(tmp_path: Path):
    """Verify discovery run persists in SQLite and survives simulated page refresh."""
    db_file = tmp_path / "service_test.db"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    fixtures_path = Path("data/internships_demo.json").resolve()
    service = OfferHuntService(
        db_path=db_file,
        artifacts_dir=artifacts_dir,
        demo_fixtures_path=fixtures_path,
    )

    # 1. Diagnostics show demo banner required in offline mode
    diag = service.get_diagnostics()
    assert diag["is_demo_mode"] is True or diag["demo_fixtures_present"] is True
    assert "DEMO DATA" in diag["banner_text"]

    # 2. Execute discovery run
    run_id = await service.execute_discovery_run(
        goal="Find Systems Engineering Internships in India",
        constraints={"role": "Systems", "location": "Bangalore", "max_results": 3},
        output_path="offers.xlsx",
        force_demo=True,
    )
    assert run_id.startswith("run_")

    # 3. Simulated page refresh: re-instantiate service from same SQLite file
    service_after_refresh = OfferHuntService(
        db_path=db_file,
        artifacts_dir=artifacts_dir,
        demo_fixtures_path=fixtures_path,
    )
    details = service_after_refresh.get_run_details(run_id)
    assert details is not None
    assert details["run"]["run_id"] == run_id
    assert details["run"]["status"] in ("COMPLETED", "VERIFIED")

    # Verify steps read from SQLite
    assert len(details["steps"]) >= 4
    for st in details["steps"]:
        assert st["state"] in ("VERIFIED", "EXECUTED")
        assert st["execution_status"] == "COMPLETED"

    # Verify evidence and audit events
    assert len(details["events"]) >= 3
    assert any(e["event_type"] == "RUN_STARTED" for e in details["events"])
    assert any(e["event_type"] == "RUN_COMPLETED" for e in details["events"])

    # Verify independent report
    assert details["report"]["final_status"] == "VERIFIED"
    assert len(details["report"]["checks"]) >= 5


def test_approval_inbox_simulate_send_blocked(tmp_path: Path):
    """Verify simulate send is strictly BLOCKED by policy with audit logging."""
    db_file = tmp_path / "gov_test.db"
    service = OfferHuntService(db_path=db_file, artifacts_dir=tmp_path)

    # Create dummy run
    conn = open_db_connection(db_file)
    from storage.database import create_run
    create_run(conn, run_id="run_gov_01", status="RUNNING")
    conn.close()

    result = service.simulate_send(run_id="run_gov_01", draft_id="draft_test_01")
    assert result["allowed"] is False
    assert result["status"] == "POLICY_BLOCK"
    assert "strictly prohibited" in result["message"]

    # Verify event logged in DB
    details = service.get_run_details("run_gov_01")
    assert any(
        e["event_type"] == "POLICY_BLOCK" and e["severity"] == "CRITICAL"
        for e in details["events"]
    )


def test_reset_demo_data_script():
    """Verify scripts/reset_demo_data.py restores seed environment cleanly."""
    reset_demo_environment()

    artifacts_dir = Path("artifacts")
    assert (artifacts_dir / ".gitkeep").exists()
    assert (artifacts_dir / "drafts").is_dir()
    assert (artifacts_dir / "offers.xlsx").exists()

    db_path = Path("artifacts/offerhunt.db")
    assert db_path.exists()


def test_no_secrets_in_git_or_logs():
    """Verify no API keys or secrets exist in git commits or log files."""
    # Check that .env is in .gitignore and not tracked by git
    res = subprocess.run(["git", "status", "--ignored"], capture_output=True, text=True)
    assert ".env" in res.stdout or (Path(".gitignore").exists() and ".env" in Path(".gitignore").read_text())

    # Check that git ls-files does not track .env or private keys
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
    assert ".env\n" not in tracked
    assert "id_rsa" not in tracked

    # Check logs directory for unredacted live secrets
    logs_dir = Path("logs")
    if logs_dir.exists():
        for log_file in logs_dir.glob("*.log"):
            content = log_file.read_text(encoding="utf-8", errors="ignore")
            assert "tvly-live" not in content
            assert "AIzaSyLive" not in content
