"""OFFERHUNT - Autonomous Internship Discovery & Proof-Validation Engine (Milestone M7).

Streamlit application providing:
1. 🎯 Offer Discovery Dashboard:
   - Internshala-inspired student discovery portal layout.
   - Task form with synthetic student persona default.
   - Plan DAG view and live step table backed by SQLite persistence.
   - Evidence panel with source links, spreadsheet preview, draft inspector, checksums.
   - Chronological audit timeline with severity filters.
   - Approval inbox with "Simulate Send" demonstrating strict POLICY_BLOCK.
   - Independent verification final report (VERIFIED, PARTIAL, UNRESOLVED, FAILED, CANCELLED).
   - "DEMO DATA - NOT LIVE SEARCH" banner when fixtures or cache are active.
2. 🧪 Reliability Lab:
   - Scenarios F01-F11 fault injection selector.
   - Visible display of injected fault and expected recovery path.
   - Controlled execution comparing OFFERHUNT vs Naive Baseline.
   - Exact ratio metrics (numerator, denominator, total runs; zero invented numbers).
   - CSV and JSON report exports.

Architectural Rule: NO business logic in the UI. Delegates strictly to OfferHuntService and ReliabilityLabRunner.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from agent.service import DEFAULT_PERSONA, OfferHuntService
from mcp_tools.spreadsheet import read_spreadsheet_records, validate_and_resolve_path
from reliability.fault_injector import ReliabilityLabRunner
from reliability.metrics import RatioMetric, ReliabilityMetricsCollector, ScenarioRunMetric
from reliability.scenarios import SCENARIOS_CATALOG, ScenarioId, list_scenarios

# ---------------------------------------------------------------------------
# Streamlit Page Setup & Custom Aesthetics (Internshala-inspired)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="OFFERHUNT",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for modern card hierarchy, badges, and high-contrast typography
st.markdown(
    """
    <style>
    /* Main container styling */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
    }
    /* Internshala-inspired brand accent colors */
    :root {
        --brand-blue: #008BDC;
        --brand-navy: #102A43;
        --brand-cyan: #EBF8FF;
        --brand-green: #00875A;
        --brand-red: #DE350B;
        --brand-amber: #FFAB00;
        --card-bg: #FFFFFF;
        --border-color: #E2E8F0;
    }
    /* Brand Header Banner */
    .brand-header {
        background: linear-gradient(135deg, #0A2540 0%, #1A365D 50%, #008BDC 100%);
        padding: 1.5rem 2rem;
        border-radius: 12px;
        color: white;
        margin-bottom: 1.5rem;
        box-shadow: 0 4px 14px rgba(0, 139, 220, 0.15);
    }
    .brand-title {
        font-size: 2.2rem;
        font-weight: 800;
        letter-spacing: -0.5px;
        margin: 0;
        display: flex;
        align-items: center;
        gap: 0.75rem;
    }
    .brand-subtitle {
        font-size: 1.05rem;
        opacity: 0.9;
        margin-top: 0.25rem;
        font-weight: 400;
    }
    /* Modern card containers */
    .oh-card {
        background: var(--card-bg);
        border: 1px solid var(--border-color);
        border-radius: 10px;
        padding: 1.25rem;
        margin-bottom: 1.25rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    /* Pill badges */
    .badge {
        display: inline-block;
        padding: 0.25rem 0.65rem;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .badge-verified { background-color: #DEF7EC; color: #03543F; border: 1px solid #84E1BC; }
    .badge-failed { background-color: #FDE8E8; color: #9B1C1C; border: 1px solid #F8B4B4; }
    .badge-unresolved { background-color: #FEF08A; color: #713F12; border: 1px solid #FDE047; }
    .badge-partial { background-color: #E1EFFE; color: #1E429F; border: 1px solid #A4CAFE; }
    .badge-running { background-color: #DBEAFE; color: #1E40AF; border: 1px solid #93C5FD; }
    .badge-demo { background-color: #FEF3C7; color: #92400E; border: 1px solid #FCD34D; font-weight: 800; }
    /* Persona Callout Box */
    .persona-box {
        background: #F8FAFC;
        border-left: 4px solid var(--brand-blue);
        padding: 0.85rem 1.15rem;
        border-radius: 4px;
        font-size: 0.9rem;
        color: #334155;
        margin-top: 0.5rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Global Service & Session State Initialization
# ---------------------------------------------------------------------------

@st.cache_resource
def get_service() -> OfferHuntService:
    return OfferHuntService()

service = get_service()

if "metrics_collector" not in st.session_state:
    st.session_state.metrics_collector = ReliabilityMetricsCollector()

if "active_run_id" not in st.session_state:
    recent_runs = service.list_recent_runs(limit=1)
    st.session_state.active_run_id = recent_runs[0]["run_id"] if recent_runs else None

if "last_lab_metric" not in st.session_state:
    st.session_state.last_lab_metric = None

diagnostics = service.get_diagnostics()

# ---------------------------------------------------------------------------
# Header & Demo Banner
# ---------------------------------------------------------------------------

st.markdown(
    """
    <div class="brand-header">
        <div class="brand-title">🎯 OFFERHUNT</div>
        <div class="brand-subtitle">Autonomous Internship Discovery, Contract Extraction & Proof-Validation Engine</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Mandatory banner if demo fixtures or cache are active
if diagnostics["banner_required"]:
    st.warning(
        f"**{diagnostics['banner_text']}** — Search results and plan generation are using vetted catalog fixtures. "
        "No live search or LLM API keys are currently required for full evaluation."
    )

# ---------------------------------------------------------------------------
# Sidebar Navigation & Diagnostics
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🎯 **OFFERHUNT Navigation**")
    app_mode = st.radio(
        "Select Portal Mode",
        ["🎯 Offer Discovery Dashboard", "🧪 Reliability Lab"],
        index=0,
    )

    st.markdown("---")
    st.markdown("#### 🔍 **Run Inspector (SQLite Persisted)**")
    all_runs = service.list_recent_runs(limit=30)
    run_options = {r["run_id"]: f"{r['run_id'][:18]}... ({r['status']})" for r in all_runs}

    if all_runs:
        selected_run = st.selectbox(
            "Select Run to Inspect",
            options=list(run_options.keys()),
            format_func=lambda x: run_options[x],
            index=0 if st.session_state.active_run_id not in run_options else list(run_options.keys()).index(st.session_state.active_run_id),
        )
        if selected_run != st.session_state.active_run_id:
            st.session_state.active_run_id = selected_run
            st.rerun()
    else:
        st.info("No runs found in database. Launch a discovery run below.")

    st.markdown("---")
    st.markdown("#### ⚙️ **System Diagnostics**")
    st.markdown(f"**Gemini Model:** `gemini-2.5-flash`")
    st.markdown(f"**Gemini Status:** {'✅ Ready' if diagnostics['gemini_available'] else '⚠️ Mock Mode'}")
    st.markdown(f"**Tavily Search:** {'✅ Ready' if diagnostics['tavily_available'] else '⚠️ Demo Fixtures'}")
    st.markdown(f"**SQLite WAL:** {'✅ Active' if diagnostics['database_ok'] else '❌ Error'}")
    st.markdown(f"**Demo Fixture:** {'✅ Available' if diagnostics['demo_fixtures_present'] else '❌ Missing'}")
    st.caption("OFFERHUNT adheres to strict zero-fabrication policies. If APIs are missing, clear diagnostics are rendered.")


# ===========================================================================
# TAB 1: 🎯 OFFER DISCOVERY DASHBOARD
# ===========================================================================

if app_mode == "🎯 Offer Discovery Dashboard":
    tabs = st.tabs(["🚀 New Discovery Run", "📋 Plan & Live Steps", "🔎 Evidence & Verification", "📥 Approval Inbox", "📑 Final Report"])

    # -----------------------------------------------------------------------
    # Subtab 1: New Discovery Run
    # -----------------------------------------------------------------------
    with tabs[0]:
        st.markdown("### 🚀 Launch an Autonomous Internship Hunt")
        st.caption("Specify your role, target locations, output workbook path, and run budget.")

        with st.form("discovery_task_form"):
            col1, col2 = st.columns([3, 2])
            with col1:
                goal_input = st.text_input(
                    "Primary Search Goal",
                    value="Find Summer 2026 Software & ML Internships with Stipend",
                    help="High-level goal for the autonomous agent to research and extract.",
                )
            with col2:
                role_input = st.text_input(
                    "Target Role / Field",
                    value="Machine Learning / Software Engineering",
                )

            col3, col4, col5 = st.columns(3)
            with col3:
                loc_input = st.text_input("City / Country / Remote", value="Bangalore, India / Remote")
            with col4:
                seniority_input = st.selectbox(
                    "Seniority Level",
                    ["Undergraduate / Intern", "Pre-final Year", "Recent Graduate", "Master's / Ph.D."],
                )
            with col5:
                max_results_input = st.slider("Max Opportunities to Extract", min_value=1, max_value=10, value=5)

            col6, col7 = st.columns(2)
            with col6:
                output_path_input = st.text_input(
                    "Target Output Spreadsheet (.xlsx)",
                    value="offers.xlsx",
                    help="Target path inside artifacts/ directory. Must be a valid .xlsx filename.",
                )
            with col7:
                tool_budget_input = st.number_input(
                    "Run Budget: Max Tool Invocations",
                    min_value=3,
                    max_value=30,
                    value=10,
                )

            # Validated Output Path Feedback
            try:
                validate_and_resolve_path(output_path_input, allowed_dir=service.artifacts_dir)
                st.success(f"✓ Validated Output Target: `artifacts/{output_path_input}`")
            except Exception as ve:
                st.error(f"❌ Invalid Path: {ve}")

            # Synthetic Student Persona (Default per brief section 6 feature 7)
            st.markdown("#### 👤 Student Persona Context")
            st.markdown(
                f"""
                <div class="persona-box">
                    <strong>Default Synthetic Persona:</strong> {DEFAULT_PERSONA['name']} | {DEFAULT_PERSONA['education']}<br/>
                    <strong>Technical Skills:</strong> {DEFAULT_PERSONA['skills']}<br/>
                    <strong>Target Interests:</strong> {DEFAULT_PERSONA['interests']}<br/>
                    <small style="color: #64748B;"><em>{DEFAULT_PERSONA['disclaimer']}</em></small>
                </div>
                """,
                unsafe_allow_html=True,
            )

            launch_submitted = st.form_submit_button("🚀 Launch Autonomous Run", use_container_width=True, type="primary")

        if launch_submitted:
            with st.spinner("Initializing discovery run, generating plan, and executing bounded tools..."):
                try:
                    new_run_id = asyncio.run(
                        service.execute_discovery_run(
                            goal=goal_input,
                            constraints={
                                "role": role_input,
                                "location": loc_input,
                                "seniority": seniority_input,
                                "max_results": max_results_input,
                            },
                            output_path=output_path_input,
                            run_budget={"max_tool_calls": tool_budget_input, "max_duration_seconds": 120},
                            persona=DEFAULT_PERSONA,
                            force_demo=diagnostics["is_demo_mode"],
                        )
                    )
                    st.session_state.active_run_id = new_run_id
                    st.success(f"Run `{new_run_id}` completed! Inspecting results...")
                    st.rerun()
                except Exception as run_err:
                    st.error(f"Execution Error: {run_err}")

    # Load active run details for inspection
    active_run = None
    if st.session_state.active_run_id:
        active_run = service.get_run_details(st.session_state.active_run_id)

    # -----------------------------------------------------------------------
    # Subtab 2: Plan & Live Step Table
    # -----------------------------------------------------------------------
    with tabs[1]:
        st.markdown("### 📋 Plan Execution & Step Lifecycle")
        if not active_run:
            st.info("No active run selected. Launch a run in the first tab or choose a past run from the sidebar.")
        else:
            run_info = active_run["run"]
            st.markdown(f"**Run ID:** `{run_info['run_id']}` | **Status:** `{run_info['status']}` | **Started:** `{run_info['started_at_utc']}`")

            # Risk classification & dependencies summary
            st.markdown("#### 🎯 Execution Plan DAG")
            steps_data = active_run["steps"]
            if steps_data:
                cols = st.columns(len(steps_data))
                for idx, (col, step) in enumerate(zip(cols, steps_data)):
                    with col:
                        input_p = step.get("input_payload") or {}
                        risk = input_p.get("risk_level", "read-only")
                        badge_color = "🟢" if risk == "read-only" else ("🟠" if risk == "local-write" else "🔴")
                        st.markdown(
                            f"""
                            <div class="oh-card" style="text-align:center;">
                                <div style="font-size: 0.8rem; font-weight:700; color: #64748B;">STEP {step['step_order']}</div>
                                <div style="font-size: 0.95rem; font-weight:700; margin: 0.3rem 0;">{input_p.get('tool_name', 'tool')}</div>
                                <div style="font-size: 0.75rem; color: #475569;">{badge_color} {risk}</div>
                                <div style="margin-top: 0.5rem;"><span class="badge badge-{step['state'].lower()}">{step['state']}</span></div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

            # Live Step Table (Persisted directly from SQLite steps table)
            st.markdown("#### 📊 Live Step Table (SQLite State)")
            st.caption("State persists directly in SQLite (offerhunt.db). A browser refresh reflects the exact same run state.")

            step_rows = []
            for s in steps_data:
                step_rows.append({
                    "Order": s["step_order"],
                    "Step ID": s["step_id"],
                    "Tool": (s.get("input_payload") or {}).get("tool_name", "N/A"),
                    "State": s["state"],
                    "Execution Status": s["execution_status"],
                    "Verification Status": s["verification_status"],
                    "Updated (UTC)": s["updated_at_utc"],
                })
            df_steps = pd.DataFrame(step_rows)
            st.dataframe(df_steps, use_container_width=True)

    # -----------------------------------------------------------------------
    # Subtab 3: Evidence Panel & Spreadsheet Preview
    # -----------------------------------------------------------------------
    with tabs[2]:
        st.markdown("### 🔎 Evidence & Physical Artifact Verification")
        if not active_run:
            st.info("No active run selected.")
        else:
            # 1. Independent Verifier Checklist
            report = active_run["report"]
            st.markdown("#### ✅ Independent Verifier Checklist")
            st.caption("Independent rules evaluate physical disk files and evidence tables directly.")

            check_cols = st.columns(3)
            for idx, chk in enumerate(report["checks"]):
                with check_cols[idx % 3]:
                    status_icon = "✅ PASSED" if chk["passed"] else "❌ FAILED"
                    border_color = "#84E1BC" if chk["passed"] else "#F8B4B4"
                    st.markdown(
                        f"""
                        <div style="border: 1px solid {border_color}; padding: 0.75rem; border-radius: 8px; margin-bottom: 0.5rem;">
                            <strong>{chk['name']}</strong>: {status_icon}<br/>
                            <small>{chk['message']}</small>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

            st.markdown("---")

            # 2. Spreadsheet Preview
            st.markdown("#### 📑 Extracted Internship Offers (`offers.xlsx`)")
            target_path = active_run["run"].get("metadata", {}).get("output_path", "offers.xlsx")
            full_sheet_path = service.artifacts_dir / target_path

            if full_sheet_path.exists():
                try:
                    headers, recs_dict = read_spreadsheet_records(full_sheet_path)
                    if recs_dict:
                        table_data = [recs_dict[k][1] for k in recs_dict]
                        df_offers = pd.DataFrame(table_data)
                        display_cols = [c for c in ["title", "company", "location", "stipend", "deadline", "eligibility", "source_url"] if c in df_offers.columns]
                        st.dataframe(df_offers[display_cols], use_container_width=True)
                    else:
                        st.info("Spreadsheet exists but has 0 data rows.")
                except Exception as exc:
                    st.error(f"Could not load spreadsheet preview: {exc}")
            else:
                st.warning(f"Spreadsheet file '{target_path}' does not exist on disk.")

            st.markdown("---")

            # 3. Draft Inspector & Artifact Hashes
            st.markdown("#### ✉️ Draft Inspector & Cryptographic Checksums")
            artifacts_list = active_run["artifacts"]
            if artifacts_list:
                for art in artifacts_list:
                    st.markdown(
                        f"**Artifact:** `{art['artifact_type']}` | **Path:** `{art['path']}` | **SHA-256:** `{art['sha256'][:16]}...`"
                    )
            else:
                st.caption("No registered artifact records for this run.")

            drafts_dir = service.artifacts_dir / "drafts"
            if drafts_dir.exists():
                eml_files = list(drafts_dir.glob("*.eml"))
                if eml_files:
                    with st.expander("✉️ View Generated Email Draft (.eml)", expanded=False):
                        st.code(eml_files[0].read_text(encoding="utf-8", errors="ignore"), language="text")

            st.markdown("---")

            # 4. Chronological Audit Timeline with Filters
            st.markdown("#### ⏱️ Chronological Audit Timeline")
            severity_filter = st.selectbox("Filter Events by Severity", ["ALL", "INFO", "WARNING", "ERROR", "CRITICAL", "POLICY_BLOCK"])

            events = active_run["events"]
            if severity_filter != "ALL":
                events = [e for e in events if e["severity"] == severity_filter or (severity_filter == "POLICY_BLOCK" and e["event_type"] == "POLICY_BLOCK")]

            for evt in events:
                sev_icon = "ℹ️" if evt["severity"] == "INFO" else ("⚠️" if evt["severity"] == "WARNING" else "🚨")
                with st.expander(f"{sev_icon} [{evt['created_at_utc']}] {evt['event_type']} - Severity: {evt['severity']}"):
                    st.json(evt.get("payload") or {})

    # -----------------------------------------------------------------------
    # Subtab 4: Approval Inbox & Simulate Send
    # -----------------------------------------------------------------------
    with tabs[3]:
        st.markdown("### 📥 Approval Inbox & Policy Boundary")
        st.caption("Operator oversight for external operations. All boundary checks are enforced in SQLite and the executor.")

        # MVP Safety Notice
        st.error(
            "🛑 **SAFETY POLICY NOTICE: Email sending is permanently BLOCKED in the MVP.**\n\n"
            "OFFERHUNT is strictly confined to local drafting (`.eml`/`.txt`). External transmission is blocked by policy."
        )

        col_send1, col_send2 = st.columns([2, 3])
        with col_send1:
            if st.button("⚠️ Simulate Send (Test Policy Block)", type="secondary", use_container_width=True):
                if active_run:
                    block_res = service.simulate_send(active_run["run"]["run_id"])
                    st.error(f"🛑 {block_res['message']}")
                    st.info(f"Audit event logged: `{block_res['event_id']}`")
                else:
                    st.warning("Please select or launch a run first.")

        st.markdown("#### 📋 Boundary Approval Requests")
        if active_run:
            approvals = active_run["approvals"]
            if approvals:
                for app in approvals:
                    col_a, col_b, col_c = st.columns([3, 1, 1])
                    with col_a:
                        st.markdown(f"**Action:** `{app['action_type']}` | **Status:** `{app['status']}` | **Step:** `{app['step_id']}`")
                        st.json(app.get("payload") or {})
                    with col_b:
                        if app["status"] == "PENDING" and st.button(f"Approve", key=f"app_{app['approval_id']}"):
                            service.approve_step(app["approval_id"])
                            st.rerun()
                    with col_c:
                        if app["status"] == "PENDING" and st.button(f"Reject", key=f"rej_{app['approval_id']}"):
                            service.reject_step(app["approval_id"])
                            st.rerun()
            else:
                st.info("No approval requests generated for this run.")

    # -----------------------------------------------------------------------
    # Subtab 5: Final Report & Claim Validation
    # -----------------------------------------------------------------------
    with tabs[4]:
        st.markdown("### 📑 Final Verification Report")
        if not active_run:
            st.info("No active run selected.")
        else:
            rep = active_run["report"]
            status_style = {
                "VERIFIED": "badge-verified",
                "PARTIAL": "badge-partial",
                "UNRESOLVED": "badge-unresolved",
                "FAILED": "badge-failed",
                "CANCELLED": "badge-failed",
            }.get(rep["final_status"], "badge-unresolved")

            st.markdown(
                f"""
                <div class="oh-card">
                    <h2 style="margin:0;">Run Status: <span class="badge {status_style}">{rep['final_status']}</span></h2>
                    <p style="margin-top:0.5rem; color:#475569;">{rep['summary']}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.markdown("#### 🎯 Claims Establishment Breakdown")
            st.caption("Never state that an internship is open or eligible unless the source supports it.")

            claims = rep.get("claims") or []
            if claims:
                for clm in claims:
                    if clm["verified"]:
                        st.success(f"✓ **Verified Claim:** {clm['claim']} (Evidence: `{clm.get('evidence_id')}`)")
                    else:
                        st.warning(f"⚠️ **Unestablished Claim:** {clm['claim']} (No corroborating evidence stored)")
            else:
                st.info("No claim links evaluated for this run.")

            st.markdown(
                """
                > [!IMPORTANT]
                > **Core Verification Invariant:** A descriptive summary can never override a failed check.
                > If any structural rule fails, the overall outcome strictly fails.
                """
            )


# ===========================================================================
# TAB 2: 🧪 RELIABILITY LAB
# ===========================================================================

elif app_mode == "🧪 Reliability Lab":
    st.markdown("### 🧪 OFFERHUNT Reliability Lab")
    st.caption(
        "Controlled fault injection across standardized scenarios F01–F11 per section 18 of the brief. "
        "Strict rule: Fault injection executes exclusively on temporary test fixtures, NEVER on user data."
    )

    scenarios_list = list_scenarios()
    scenario_map = {f"{s.code}: {s.title}": s for s in scenarios_list}

    col_sel1, col_sel2 = st.columns([2, 1])
    with col_sel1:
        chosen_label = st.selectbox(
            "Select Reliability Scenario (F01 - F11)",
            options=list(scenario_map.keys()),
            index=0,
        )
    chosen_scenario = scenario_map[chosen_label]

    with col_sel2:
        st.markdown(f"**Category:** `{chosen_scenario.category}`")
        st.markdown(f"**Expected Final Status:** `{chosen_scenario.expected_final_status}`")

    # Visible Display of Injected Fault and Recovery Path (Mandatory UI Requirement)
    st.markdown(
        f"""
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem;">
            <div style="background: #FFF5F5; border: 1px solid #FEB2B2; border-left: 5px solid #E53E3E; padding: 1rem; border-radius: 8px;">
                <h4 style="margin: 0 0 0.5rem 0; color: #9B1C1C;">💥 Injected Fault</h4>
                <p style="margin: 0; font-size: 0.95rem; color: #742A2A;">{chosen_scenario.injected_fault}</p>
                <small style="color: #9B1C1C; margin-top: 0.5rem; display: block;"><em>{chosen_scenario.description}</em></small>
            </div>
            <div style="background: #F0FFF4; border: 1px solid #9AE6B4; border-left: 5px solid #38A169; padding: 1rem; border-radius: 8px;">
                <h4 style="margin: 0 0 0.5rem 0; color: #22543D;">🛡️ Expected Recovery Path</h4>
                <p style="margin: 0; font-size: 0.95rem; color: #276749;">{chosen_scenario.expected_recovery_path}</p>
                <small style="color: #22543D; margin-top: 0.5rem; display: block;"><em>Preserves idempotency, zero duplicate rows, and strict error classification.</em></small>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Execution controls
    c_btn1, c_btn2, c_btn3 = st.columns(3)
    runner = ReliabilityLabRunner()

    with c_btn1:
        if st.button(f"▶️ Run Scenario ({chosen_scenario.code}) [OFFERHUNT]", type="primary", use_container_width=True):
            with st.spinner(f"Injecting fault {chosen_scenario.code} into temporary fixture..."):
                metric = asyncio.run(runner.run_scenario(chosen_scenario.id, is_baseline=False))
                st.session_state.metrics_collector.record_run(metric)
                st.session_state.last_lab_metric = metric
                st.rerun()

    with c_btn2:
        if st.button(f"⚠️ Run Naive Baseline ({chosen_scenario.code})", type="secondary", use_container_width=True):
            with st.spinner(f"Running unhardened baseline under {chosen_scenario.code}..."):
                metric = asyncio.run(runner.run_scenario(chosen_scenario.id, is_baseline=True))
                st.session_state.metrics_collector.record_run(metric)
                st.session_state.last_lab_metric = metric
                st.rerun()

    with c_btn3:
        if st.button("🚀 Run Complete Controlled Suite (F01–F11)", use_container_width=True):
            with st.spinner("Executing all 11 scenarios across OFFERHUNT and Baseline..."):
                for s in scenarios_list:
                    # Run OFFERHUNT
                    m_oh = asyncio.run(runner.run_scenario(s.id, is_baseline=False))
                    st.session_state.metrics_collector.record_run(m_oh)
                    # Run Baseline
                    m_base = asyncio.run(runner.run_scenario(s.id, is_baseline=True))
                    st.session_state.metrics_collector.record_run(m_base)
                st.success("Completed controlled suite runs!")
                st.rerun()

    # Visibly display latest execution metric
    if st.session_state.last_lab_metric:
        lm: ScenarioRunMetric = st.session_state.last_lab_metric
        st.markdown(f"#### 📊 Latest Execution Telemetry: `{lm.scenario_code}` ({'Baseline' if lm.is_baseline else 'OFFERHUNT'})")
        m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
        m_col1.metric("Final Status", lm.final_status)
        m_col2.metric("Recovery Succeeded", "Yes" if lm.recovery_successful else "No")
        m_col3.metric("Duplicate Rows", "Yes" if lm.duplicate_produced else "0 (Clean)")
        m_col4.metric("Approval Bypassed", "VIOLATION" if lm.approval_bypassed else "0 (Safe)")
        m_col5.metric("Latency", f"{lm.latency_ms:.1f} ms")

    st.markdown("---")

    # Aggregate Metrics Dashboard (Always shows numerator, denominator, and run count)
    st.markdown("### 📈 Measured Reliability Metrics (Zero Invented Numbers)")
    st.caption("All metrics derive strictly from executed controlled runs. Formatted as numerator/denominator (percentage).")

    summary_all = st.session_state.metrics_collector.compute_summary()
    summary_oh = st.session_state.metrics_collector.compute_summary(baseline_only=False)
    summary_base = st.session_state.metrics_collector.compute_summary(baseline_only=True)

    if summary_all.total_runs == 0:
        st.info("No scenario runs recorded in this session yet. Click one of the execution buttons above to gather real telemetry.")
    else:
        # Comparison Table: OFFERHUNT vs Baseline
        comp_data = [
            {
                "Metric Name": "Verified Success Rate",
                "OFFERHUNT (Resilient)": summary_oh.verified_success.display(),
                "Naive Baseline": summary_base.verified_success.display(),
                "Target / Invariant": "100% on valid tasks",
            },
            {
                "Metric Name": "Recovery Success Rate",
                "OFFERHUNT (Resilient)": summary_oh.recovery_success.display(),
                "Naive Baseline": summary_base.recovery_success.display(),
                "Target / Invariant": "100% of injected faults",
            },
            {
                "Metric Name": "Duplicate Row Rate",
                "OFFERHUNT (Resilient)": summary_oh.duplicate_rate.display(),
                "Naive Baseline": summary_base.duplicate_rate.display(),
                "Target / Invariant": "Strictly 0.0%",
            },
            {
                "Metric Name": "False Completion Rate",
                "OFFERHUNT (Resilient)": summary_oh.false_completion_rate.display(),
                "Naive Baseline": summary_base.false_completion_rate.display(),
                "Target / Invariant": "Strictly 0.0%",
            },
            {
                "Metric Name": "Approval Bypass Count",
                "OFFERHUNT (Resilient)": f"{summary_oh.approval_bypass_count} bypasses",
                "Naive Baseline": f"{summary_base.approval_bypass_count} bypasses",
                "Target / Invariant": "Strictly 0",
            },
            {
                "Metric Name": "Total Controlled Runs",
                "OFFERHUNT (Resilient)": str(summary_oh.total_runs),
                "Naive Baseline": str(summary_base.total_runs),
                "Target / Invariant": ">= 11 required",
            },
        ]
        df_comp = pd.DataFrame(comp_data)
        st.dataframe(df_comp, use_container_width=True)

        # Export Controls (CSV and JSON)
        st.markdown("#### 💾 Export Telemetry Data")
        exp_col1, exp_col2, exp_col3 = st.columns([1, 1, 2])

        # Prepare in-memory CSV
        csv_buffer = io.StringIO()
        if st.session_state.metrics_collector.records:
            fieldnames = list(st.session_state.metrics_collector.records[0].__dict__.keys())
            import csv
            writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
            writer.writeheader()
            for rec in st.session_state.metrics_collector.records:
                writer.writerow(rec.__dict__)

        with exp_col1:
            st.download_button(
                label="📥 Download CSV Report",
                data=csv_buffer.getvalue(),
                file_name="offerhunt_reliability_metrics.csv",
                mime="text/csv",
                use_container_width=True,
            )

        with exp_col2:
            json_export_data = {
                "exported_at_utc": datetime.now(timezone.utc).isoformat(),
                "offerhunt_summary": summary_oh.__dict__,
                "baseline_summary": summary_base.__dict__,
                "records": [r.__dict__ for r in st.session_state.metrics_collector.records],
            }
            # Custom encoder for RatioMetric
            def default_encoder(obj):
                if isinstance(obj, RatioMetric):
                    return obj.__dict__
                return str(obj)

            st.download_button(
                label="📥 Download JSON Report",
                data=json.dumps(json_export_data, indent=2, default=default_encoder),
                file_name="offerhunt_reliability_metrics.json",
                mime="application/json",
                use_container_width=True,
            )

        with exp_col3:
            if st.button("🧹 Clear Metrics Telemetry", use_container_width=True):
                st.session_state.metrics_collector.clear()
                st.session_state.last_lab_metric = None
                st.rerun()
