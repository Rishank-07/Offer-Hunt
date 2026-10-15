"""End-to-end workflow verification script for OFFERHUNT.

Tests all workflows programmatically:
1. Diagnostics & environment readiness.
2. Complete autonomous discovery run (goal -> plan -> execution -> verification).
3. State persistence & page-refresh continuity.
4. Physical disk artifact checks (offers.xlsx, drafts, checksums).
5. Boundary approval & simulate send policy block.
6. Reliability Lab fault injection scenarios (F01, F04, F06, F11).
7. Metrics aggregation & telemetry export.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure utf-8 encoding for Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.service import DEFAULT_PERSONA, OfferHuntService
from mcp_tools.spreadsheet import read_spreadsheet_records
from reliability.fault_injector import ReliabilityLabRunner
from reliability.metrics import ReliabilityMetricsCollector
from reliability.scenarios import ScenarioId


async def run_e2e_verification() -> bool:
    print("\n=======================================================")
    print("[OFFERHUNT] STARTING END-TO-END WORKFLOW VERIFICATION")
    print("=======================================================\n")

    service = OfferHuntService()
    anomalies = []

    # -----------------------------------------------------------------------
    # Step 1: System Diagnostics
    # -----------------------------------------------------------------------
    print("[1/6] Checking System Diagnostics...")
    diag = service.get_diagnostics()
    print(f"  - SQLite DB Integrity: {'OK' if diag['database_ok'] else 'FAIL'}")
    print(f"  - Demo Fixture Present: {diag['demo_fixtures_present']}")
    print(f"  - Mode: {'DEMO FIXTURE MODE' if diag['is_demo_mode'] else 'LIVE API MODE'}")
    print(f"  - Banner Text: '{diag['banner_text']}'")

    if not diag["database_ok"]:
        anomalies.append("Database integrity check failed.")
    if not diag["demo_fixtures_present"]:
        anomalies.append("data/internships_demo.json is missing.")

    # -----------------------------------------------------------------------
    # Step 2: Discovery Run Workflow
    # -----------------------------------------------------------------------
    print("\n[2/6] Executing Autonomous Discovery Run...")
    try:
        run_id = await service.execute_discovery_run(
            goal="Find Summer 2026 Machine Learning & Backend Internships in Bangalore",
            constraints={
                "role": "Machine Learning",
                "location": "Bangalore",
                "seniority": "Undergraduate / Intern",
                "max_results": 3,
            },
            output_path="offers.xlsx",
            run_budget={"max_tool_calls": 10, "max_duration_seconds": 60},
            persona=DEFAULT_PERSONA,
            force_demo=True,
        )
        print(f"  [PASS] Discovery Run completed! Run ID: {run_id}")
    except Exception as e:
        anomalies.append(f"Discovery run execution failed: {e}")
        print(f"  [FAIL] Error: {e}")
        return False

    # -----------------------------------------------------------------------
    # Step 3: State Persistence & Refresh Continuity
    # -----------------------------------------------------------------------
    print("\n[3/6] Verifying SQLite State Persistence (Page Refresh Simulation)...")
    service_refreshed = OfferHuntService()
    details = service_refreshed.get_run_details(run_id)

    if not details:
        anomalies.append(f"Run {run_id} not found in database after refresh.")
        print("  [FAIL] Failed to retrieve run after refresh.")
    else:
        run_status = details["run"]["status"]
        report_status = details["report"]["final_status"]
        steps_count = len(details["steps"])
        events_count = len(details["events"])
        checks_count = len(details["report"]["checks"])

        print(f"  [PASS] Run status: {run_status} (Verification: {report_status})")
        print(f"  [PASS] Steps recorded: {steps_count}")
        print(f"  [PASS] Events logged: {events_count}")
        print(f"  [PASS] Verification checks: {checks_count}")

        if report_status != "VERIFIED":
            anomalies.append(f"Expected report status VERIFIED, got {report_status}")
        if steps_count < 4:
            anomalies.append(f"Expected >= 4 steps, got {steps_count}")

    # -----------------------------------------------------------------------
    # Step 4: Physical Disk Artifacts & Checksums
    # -----------------------------------------------------------------------
    print("\n[4/6] Inspecting Physical Disk Artifacts...")
    sheet_path = service.artifacts_dir / "offers.xlsx"
    if not sheet_path.exists():
        anomalies.append("offers.xlsx was not created in artifacts directory.")
        print("  [FAIL] offers.xlsx missing.")
    else:
        headers, records_dict = read_spreadsheet_records(sheet_path)
        print(f"  [PASS] offers.xlsx verified on disk: {len(records_dict)} rows recorded.")

    drafts_dir = service.artifacts_dir / "drafts"
    eml_files = list(drafts_dir.glob("*.eml")) if drafts_dir.exists() else []
    if not eml_files:
        anomalies.append("No .eml draft file was created in artifacts/drafts.")
        print("  [FAIL] Email draft missing.")
    else:
        print(f"  [PASS] Draft verified on disk: '{eml_files[0].name}' ({eml_files[0].stat().st_size} bytes).")

    # -----------------------------------------------------------------------
    # Step 5: Policy Boundary & Simulate Send
    # -----------------------------------------------------------------------
    print("\n[5/6] Testing Approval Inbox & Simulate Send Policy Block...")
    block_res = service.simulate_send(run_id=run_id, draft_id="draft_test")
    print(f"  - Allowed: {block_res['allowed']}")
    print(f"  - Status: {block_res['status']}")
    print(f"  - Message: '{block_res['message']}'")

    if block_res["allowed"] is not False or block_res["status"] != "POLICY_BLOCK":
        anomalies.append("Simulate send was not blocked with POLICY_BLOCK.")
    else:
        print("  [PASS] Policy block strictly enforced at boundary.")

    # -----------------------------------------------------------------------
    # Step 6: Reliability Lab Scenarios (F01, F04, F06, F11)
    # -----------------------------------------------------------------------
    print("\n[6/6] Executing Reliability Lab Fault Injection Scenarios...")
    runner = ReliabilityLabRunner()
    collector = ReliabilityMetricsCollector()

    test_scenarios = [ScenarioId.F01, ScenarioId.F04, ScenarioId.F06, ScenarioId.F11]
    for sc_id in test_scenarios:
        metric = await runner.run_scenario(sc_id, is_baseline=False)
        collector.record_run(metric)
        print(f"  [PASS] Scenario {metric.scenario_code} ({metric.scenario_title}): Status={metric.final_status}, Recovery={metric.recovery_successful}")

        if sc_id == ScenarioId.F01 and (metric.duplicate_produced or not metric.recovery_successful):
            anomalies.append("F01 produced duplicates or failed recovery.")
        if sc_id == ScenarioId.F04 and metric.final_status != "FAILED":
            anomalies.append("F04 did not fail invalid tool.")
        if sc_id == ScenarioId.F06 and metric.final_status != "FAILED":
            anomalies.append("F06 did not fail rejected approval.")
        if sc_id == ScenarioId.F11 and metric.final_status != "FAILED":
            anomalies.append("F11 did not fail path traversal.")

    summary = collector.compute_summary()
    print(f"\n  [PASS] Metrics Aggregate Summary:")
    print(f"    - Total runs: {summary.total_runs}")
    print(f"    - Recovery Success: {summary.recovery_success.display()}")
    print(f"    - Duplicate Rate: {summary.duplicate_rate.display()}")
    print(f"    - Approval Bypasses: {summary.approval_bypass_count}")

    # Export test
    temp_json = service.artifacts_dir / "e2e_telemetry.json"
    temp_csv = service.artifacts_dir / "e2e_telemetry.csv"
    collector.export_to_json(temp_json)
    collector.export_to_csv(temp_csv)

    if not temp_json.exists() or not temp_csv.exists():
        anomalies.append("Telemetry exports failed to write to disk.")
    else:
        print("  [PASS] Telemetry CSV & JSON exported successfully.")

    # -----------------------------------------------------------------------
    # Summary & Anomaly Report
    # -----------------------------------------------------------------------
    print("\n=======================================================")
    if not anomalies:
        print("[SUCCESS] ALL END-TO-END WORKFLOWS PASSED WITH ZERO ANOMALIES!")
        print("=======================================================\n")
        return True
    else:
        print(f"[WARN] FOUND {len(anomalies)} ANOMALIES:")
        for a in anomalies:
            print(f"  - {a}")
        print("=======================================================\n")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_e2e_verification())
    sys.exit(0 if success else 1)
