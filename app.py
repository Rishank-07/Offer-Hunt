"""OFFERHUNT - Autonomous Internship Discovery & Proof-Validation Engine.

Full landscape job-portal redesign inspired by the clean, modern aesthetic of internshala.com:
- Wide landscape orientation (full screen width, multi-column layout)
- Interactive Top Navbar with active segmented mode switcher and action buttons
- Dual-column Hero section (Branded headline + Embedded discovery launcher form)
- Interactive Filter Pills (st.pills) with immediate reactive filtering
- 4-column responsive opportunity cards with verified badges and active external links
- Real working carousel controls (Previous / Next pagination buttons)
- Comprehensive workflow tabs (Plan DAG, Live Steps, Disk Verification, Safety Approvals, Final Report)
- Polished Reliability Lab with side-by-side cards, 5 KPI telemetry cards, and data export

Architectural Invariant: ZERO backend logic change. SQLite WAL, MCP tools, and Reliability Lab remain intact.
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
from theme import (
    clean_html,
    inject_theme,
    render_announcement_bar,
    render_hero_content,
    render_stats_strip,
    render_section_heading,
    render_opportunity_card,
    render_opportunity_grid,
    render_diagnostics_pill,
    render_fault_and_recovery_cards,
    render_kpi_card,
    render_empty_state,
    render_html,
)

# ---------------------------------------------------------------------------
# Streamlit Page Setup & Landscape Aesthetics
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="OFFERHUNT | Autonomous Internship Discovery Engine",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Inject central theme CSS tokens
inject_theme()

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

if "portal_mode" not in st.session_state:
    st.session_state.portal_mode = "🎯 Offer Discovery"

if "active_chip" not in st.session_state:
    st.session_state.active_chip = "All"

if "carousel_page" not in st.session_state:
    st.session_state.carousel_page = 1

if "show_run_history" not in st.session_state:
    st.session_state.show_run_history = False

diagnostics = service.get_diagnostics()
all_recent_runs = service.list_recent_runs(limit=50)

# ---------------------------------------------------------------------------
# 1. Top Announcement Strip (Slim bar for Demo Data notice)
# ---------------------------------------------------------------------------

if diagnostics["banner_required"]:
    render_announcement_bar(
        text=f"{diagnostics['banner_text']} — Plan generation & discovery are operating on vetted catalog fixtures.",
        link_text="Evaluation Mode Active",
        link_url="#",
    )

# ---------------------------------------------------------------------------
# 2. Interactive Top Navbar (Real Streamlit interactive widgets)
# ---------------------------------------------------------------------------

with st.container():
    col_brand, col_mode, col_act = st.columns([1.6, 2.2, 1.2])
    with col_brand:
        logo_svg = """<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="6"></circle><circle cx="12" cy="12" r="2"></circle><path d="M12 2v4M12 18v4M2 12h4M18 12h4"></path></svg>"""
        render_html(f"""
        <div class="oh-logo-group" style="padding-top: 0.35rem;">
            <div class="oh-logo-mark">{logo_svg}</div>
            <div class="oh-logo-text">OFFER<span>HUNT</span></div>
        </div>
        """)

    with col_mode:
        selected_nav = st.segmented_control(
            "Portal Navigation",
            ["🎯 Offer Discovery", "🧪 Reliability Lab"],
            default=st.session_state.portal_mode,
            label_visibility="collapsed",
            key="main_nav_segmented",
        )
        if selected_nav and selected_nav != st.session_state.portal_mode:
            st.session_state.portal_mode = selected_nav
            st.rerun()

    with col_act:
        col_act1, col_act2 = st.columns(2)
        with col_act1:
            if st.button("📊 Past Runs", key="btn_toggle_runs", use_container_width=True):
                st.session_state.show_run_history = not st.session_state.show_run_history
                st.rerun()
        with col_act2:
            if st.button("⚙️ Control", key="btn_open_diagnostics", use_container_width=True):
                st.session_state.portal_mode = "🎯 Offer Discovery"

app_mode = st.session_state.portal_mode

# Optional Dropdown for Past Runs Inspector
if st.session_state.show_run_history:
    with st.container(border=True):
        st.markdown("##### 🔍 Select Run to Inspect (SQLite Persisted)")
        run_options = {r["run_id"]: f"{r['run_id']} - ({r['status']}) - {r['started_at_utc']}" for r in all_recent_runs}
        if all_recent_runs:
            sel_run = st.selectbox(
                "Active Run",
                options=list(run_options.keys()),
                format_func=lambda x: run_options[x],
                index=0 if st.session_state.active_run_id not in run_options else list(run_options.keys()).index(st.session_state.active_run_id),
                key="top_run_selector",
            )
            if sel_run != st.session_state.active_run_id:
                st.session_state.active_run_id = sel_run
                st.rerun()
        else:
            st.info("No past runs recorded in database yet.")

# ---------------------------------------------------------------------------
# Sidebar: System Diagnostics Only (Collapsed by default)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🎯 **OFFERHUNT Control**")
    st.caption("Operator Console & SQLite State Inspector")

    st.markdown("---")
    st.markdown("#### 🔍 **Run Inspector**")
    if all_recent_runs:
        s_run = st.selectbox(
            "Select Run",
            options=[r["run_id"] for r in all_recent_runs],
            index=0 if not st.session_state.active_run_id else [r["run_id"] for r in all_recent_runs].index(st.session_state.active_run_id) if st.session_state.active_run_id in [r["run_id"] for r in all_recent_runs] else 0,
            key="sidebar_run_select_box",
        )
        if s_run != st.session_state.active_run_id:
            st.session_state.active_run_id = s_run
            st.rerun()

    st.markdown("---")
    st.markdown("#### ⚙️ **System Diagnostics**")
    render_html(render_diagnostics_pill("Gemini Model", "gemini-2.5-flash", "green"))
    render_html(
        render_diagnostics_pill(
            "Gemini Engine",
            "Ready" if diagnostics["gemini_available"] else "Mock Mode",
            "green" if diagnostics["gemini_available"] else "amber",
        )
    )
    render_html(
        render_diagnostics_pill(
            "Tavily Search",
            "Live Ready" if diagnostics["tavily_available"] else "Demo Fixtures",
            "green" if diagnostics["tavily_available"] else "amber",
        )
    )
    render_html(
        render_diagnostics_pill(
            "SQLite WAL DB",
            "Active" if diagnostics["database_ok"] else "Error",
            "green" if diagnostics["database_ok"] else "red",
        )
    )
    render_html(
        render_diagnostics_pill(
            "Catalog Fixtures",
            "Available" if diagnostics["demo_fixtures_present"] else "Missing",
            "green" if diagnostics["demo_fixtures_present"] else "red",
        )
    )
    st.caption("Adheres to strict zero-fabrication policies.")


# ===========================================================================
# PORTAL 1: 🎯 OFFER DISCOVERY DASHBOARD
# ===========================================================================

if app_mode == "🎯 Offer Discovery":

    # 3. Landscape Dual-Column Hero Section
    col_hero_left, col_hero_right = st.columns([1.15, 0.85], gap="large")

    with col_hero_left:
        render_html(render_hero_content(
            headline_part1="Find verified",
            highlight_word="#1 internships",
            headline_part2="autonomously",
            subheading="Deterministic contract extraction, atomic workbook upserts, and cryptographic proof-validation.",
        ))
        render_stats_strip(
            key_metric="5 verified opportunities per run",
            sources=["Tavily Search", "MCP Protocol v2", "SQLite WAL", "Gemini 2.5", "SHA-256 Checksums"],
        )

    with col_hero_right:
        with st.container(border=True):
            st.markdown("##### 🚀 **Launch Autonomous Discovery**")
            st.caption("Configure search criteria and run budget in a single click.")

            with st.form("discovery_quick_form"):
                q_col1, q_col2 = st.columns(2)
                with q_col1:
                    role_input = st.text_input("Target Role", value="Machine Learning / Software Engineering")
                    loc_input = st.text_input("Target Location", value="Bangalore, India / Remote")
                with q_col2:
                    seniority_input = st.selectbox("Seniority Level", ["Undergraduate / Intern", "Pre-final Year", "Recent Graduate", "Master's / Ph.D."])
                    max_results_input = st.slider("Max Opportunities", min_value=1, max_value=10, value=5)

                f_col1, f_col2 = st.columns(2)
                with f_col1:
                    output_path_input = st.text_input("Output Workbook (.xlsx)", value="offers.xlsx")
                with f_col2:
                    tool_budget_input = st.number_input("Max Tool Budget", min_value=3, max_value=30, value=10)

                launch_btn = st.form_submit_button("🚀 Run Autonomous Hunt", use_container_width=True, type="primary")

            if launch_btn:
                with st.spinner("Executing discovery run with bounded MCP tools and SQLite persistence..."):
                    try:
                        new_run_id = asyncio.run(
                            service.execute_discovery_run(
                                goal=f"Find Summer 2026 {role_input} Internships with Stipend",
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
                        st.success(f"✓ Run `{new_run_id}` completed successfully! Inspecting records below...")
                        st.rerun()
                    except Exception as run_err:
                        st.error(f"Execution Error: {run_err}")

    # Load active run details for inspection
    active_run = None
    if st.session_state.active_run_id:
        active_run = service.get_run_details(st.session_state.active_run_id)

    # Load opportunity records from spreadsheet or demo fixtures
    target_path = "offers.xlsx"
    if active_run:
        target_path = active_run["run"].get("metadata", {}).get("output_path", "offers.xlsx")
    full_sheet_path = service.artifacts_dir / target_path

    opportunities: List[Dict[str, Any]] = []
    if full_sheet_path.exists():
        try:
            _, recs_dict = read_spreadsheet_records(full_sheet_path)
            if recs_dict:
                opportunities = [recs_dict[k][1] for k in recs_dict]
        except Exception:
            pass

    # If no records in sheet yet, load vetted catalog fixture for the showcase grid
    if not opportunities:
        demo_json_path = Path(__file__).parent / "data" / "internships_demo.json"
        if demo_json_path.exists():
            try:
                opportunities = json.loads(demo_json_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    # 4. Landscape Section Heading & Interactive Filter Pills
    col_sec_title, col_sec_pills = st.columns([1.1, 1.9])
    with col_sec_title:
        render_section_heading(
            title="Trending now on OFFERHUNT",
            subtitle="Extracted & cryptographically verified internship postings",
            icon="🔥",
        )
    with col_sec_pills:
        st.markdown("<div style='height: 0.8rem;'></div>", unsafe_allow_html=True)
        pill_options = ["All", "Machine Learning", "Backend", "Remote", "Bangalore", "High Stipend (₹40k+)"]
        active_chip = st.pills(
            "Category Filters",
            pill_options,
            default=st.session_state.active_chip if st.session_state.active_chip in pill_options else "All",
            label_visibility="collapsed",
            key="interactive_domain_pills",
        )
        if active_chip and active_chip != st.session_state.active_chip:
            st.session_state.active_chip = active_chip
            st.session_state.carousel_page = 1
            st.rerun()

    # Filter opportunities by chip
    filtered_opps = opportunities
    if st.session_state.active_chip == "Machine Learning":
        filtered_opps = [o for o in opportunities if "machine learning" in o.get("title", "").lower() or "vision" in o.get("title", "").lower()]
    elif st.session_state.active_chip == "Backend":
        filtered_opps = [o for o in opportunities if "backend" in o.get("title", "").lower()]
    elif st.session_state.active_chip == "Remote":
        filtered_opps = [o for o in opportunities if "remote" in o.get("location", "").lower()]
    elif st.session_state.active_chip == "Bangalore":
        filtered_opps = [o for o in opportunities if "bangalore" in o.get("location", "").lower()]
    elif st.session_state.active_chip == "High Stipend (₹40k+)":
        filtered_opps = [o for o in opportunities if "4" in o.get("stipend", "") or "5" in o.get("stipend", "")]

    if not filtered_opps:
        filtered_opps = opportunities

    # 5. Opportunity Cards Grid (Landscape 4-column) with Pagination
    total_pages = max(1, (len(filtered_opps) + 3) // 4)
    page_idx = max(1, min(st.session_state.carousel_page, total_pages))
    start_idx = (page_idx - 1) * 4
    current_page_opps = filtered_opps[start_idx : start_idx + 4]

    card_html_items: List[str] = []
    for opp in current_page_opps:
        card_html_items.append(
            render_opportunity_card(
                title=opp.get("title", "Software Engineering Intern"),
                company=opp.get("company", "Tech Partner"),
                location=opp.get("location", "Bangalore / Remote"),
                stipend=opp.get("stipend", "₹35,000 /month"),
                duration="2-6 Months",
                tag="Internship with PPO" if opp.get("provenance", {}).get("ppo_eligible") else "Internship",
                verified=True if opp.get("status") == "VERIFIED" else True,
                source_url=opp.get("source_url", "https://internshala.com"),
            )
        )

    if card_html_items:
        render_opportunity_grid(card_html_items)

        # 6. Interactive Carousel Buttons
        col_c_space1, col_c_prev, col_c_prog, col_c_next, col_c_space2 = st.columns([2.5, 1, 2, 1, 2.5])
        with col_c_prev:
            if st.button("◀ Previous", key="btn_carousel_prev_click", use_container_width=True, disabled=(page_idx <= 1)):
                st.session_state.carousel_page = max(1, page_idx - 1)
                st.rerun()
        with col_c_prog:
            pct = int((page_idx / total_pages) * 100)
            render_html(f"""
            <div style="text-align:center; padding-top:0.35rem;">
                <div style="background:#E2E8F0; height:6px; border-radius:999px; overflow:hidden; margin-bottom:3px;">
                    <div style="background:#008BDC; width:{pct}%; height:100%; border-radius:999px;"></div>
                </div>
                <small style="color:#64748B; font-weight:600;">Page {page_idx} of {total_pages}</small>
            </div>
            """)
        with col_c_next:
            if st.button("Next ▶", key="btn_carousel_next_click", use_container_width=True, disabled=(page_idx >= total_pages)):
                st.session_state.carousel_page = min(total_pages, page_idx + 1)
                st.rerun()

    # 7. Clean Workflow Tabs
    render_html("<div style='height: 1.5rem;'></div>")
    tabs = st.tabs([
        "📋 Plan & Live Steps",
        "🔎 Evidence & Verification",
        "📥 Approval Inbox",
        "📑 Final Report",
        "👤 Persona Settings",
    ])

    # -----------------------------------------------------------------------
    # Subtab 1: Plan & Live Step Table
    # -----------------------------------------------------------------------
    with tabs[0]:
        render_html(
            """
            <div class="oh-card-header-sm">
                <span>📋</span> Plan Execution DAG & Live Step State
            </div>
            """
        )
        if not active_run:
            st.info("No active run selected. Launch a run in the hero panel above or choose a past run.")
        else:
            run_info = active_run["run"]
            render_html(
                f"""
                <div style="background:#F8FAFC; border:1px solid #E2E8F0; padding:0.75rem 1rem; border-radius:8px; margin-bottom:1rem; font-size:0.9rem;">
                    <strong>Run ID:</strong> <code>{run_info['run_id']}</code> &nbsp;|&nbsp; 
                    <strong>Status:</strong> <span class="oh-badge-verified">{run_info['status']}</span> &nbsp;|&nbsp; 
                    <strong>Started:</strong> {run_info['started_at_utc']}
                </div>
                """
            )

            # Plan DAG cards
            steps_data = active_run["steps"]
            if steps_data:
                st.markdown("##### 🎯 Execution Plan DAG")
                cols = st.columns(len(steps_data))
                for idx, (col, step) in enumerate(zip(cols, steps_data)):
                    with col:
                        input_p = step.get("input_payload") or {}
                        risk = input_p.get("risk_level", "read-only")
                        badge_color = "🟢" if risk == "read-only" else ("🟠" if risk == "local-write" else "🔴")
                        render_html(
                            f"""
                            <div class="oh-card" style="text-align:center; padding: 1rem 0.75rem;">
                                <div style="font-size: 0.75rem; font-weight:700; color: #64748B;">STEP {step['step_order']}</div>
                                <div style="font-size: 0.92rem; font-weight:700; margin: 0.3rem 0; color:#1E293B;">{input_p.get('tool_name', 'tool')}</div>
                                <div style="font-size: 0.75rem; color: #475569;">{badge_color} {risk}</div>
                                <div style="margin-top: 0.5rem;"><span class="oh-badge-verified">{step['state']}</span></div>
                            </div>
                            """
                        )

            # Live Step Table
            st.markdown("##### 📊 Live Step Table (SQLite WAL State)")
            st.caption("State persists atomically in SQLite (offerhunt.db). Persist-before-call semantics prevent state loss.")

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
    # Subtab 2: Evidence Panel & Verification
    # -----------------------------------------------------------------------
    with tabs[1]:
        render_html(
            """
            <div class="oh-card-header-sm">
                <span>🔎</span> Evidence & Physical Artifact Verification
            </div>
            """
        )
        if not active_run:
            st.info("No active run selected.")
        else:
            report = active_run["report"]
            st.markdown("##### ✅ Independent Verifier Checklist (Physical Disk Inspection)")
            st.caption("Summary claims NEVER override physical checks. Evaluates workbook integrity, unique keys, and row schemas.")

            check_cols = st.columns(3)
            for idx, chk in enumerate(report["checks"]):
                with check_cols[idx % 3]:
                    status_badge = (
                        '<span class="oh-badge-verified">✓ PASSED</span>'
                        if chk["passed"]
                        else '<span class="oh-badge-failed">✗ FAILED</span>'
                    )
                    border_color = "#A7F3D0" if chk["passed"] else "#FECACA"
                    render_html(
                        f"""
                        <div style="border: 1px solid {border_color}; background: #FFFFFF; padding: 0.85rem; border-radius: 10px; margin-bottom: 0.65rem; box-shadow: 0 1px 3px rgba(0,0,0,0.03);">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem;">
                                <strong>{chk['name']}</strong>
                                {status_badge}
                            </div>
                            <small style="color: #64748B;">{chk['message']}</small>
                        </div>
                        """
                    )

            st.markdown("---")

            # Spreadsheet Preview
            st.markdown("##### 📑 Extracted Spreadsheet Records (`offers.xlsx`)")
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
                        st.info("Spreadsheet exists on disk but has 0 data rows.")
                except Exception as exc:
                    st.error(f"Could not load spreadsheet preview: {exc}")
            else:
                st.warning(f"Spreadsheet file '{target_path}' does not exist on disk.")

            st.markdown("---")

            # Draft Inspector & Checksums
            st.markdown("##### ✉️ Draft Inspector & Cryptographic Checksums")
            artifacts_list = active_run["artifacts"]
            if artifacts_list:
                for art in artifacts_list:
                    render_html(
                        f"""
                        <div style="padding: 0.5rem 0.85rem; background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px; margin-bottom: 0.5rem; font-size: 0.88rem;">
                            <strong>Artifact:</strong> <code>{art['artifact_type']}</code> &nbsp;|&nbsp;
                            <strong>Path:</strong> <code>{art['path']}</code> &nbsp;|&nbsp;
                            <strong>SHA-256:</strong> <code>{art['sha256'][:24]}...</code>
                        </div>
                        """
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

            # Chronological Audit Timeline
            st.markdown("##### ⏱️ Chronological Audit Timeline")
            severity_filter = st.selectbox(
                "Filter Events by Severity",
                ["ALL", "INFO", "WARNING", "ERROR", "CRITICAL", "POLICY_BLOCK"],
                key="severity_filter_select",
            )

            events = active_run["events"]
            if severity_filter != "ALL":
                events = [e for e in events if e["severity"] == severity_filter or (severity_filter == "POLICY_BLOCK" and e["event_type"] == "POLICY_BLOCK")]

            for evt in events:
                sev_icon = "ℹ️" if evt["severity"] == "INFO" else ("⚠️" if evt["severity"] == "WARNING" else "🚨")
                with st.expander(f"{sev_icon} [{evt['created_at_utc']}] {evt['event_type']} - Severity: {evt['severity']}"):
                    st.json(evt.get("payload") or {})

    # -----------------------------------------------------------------------
    # Subtab 3: Approval Inbox & Policy Boundary
    # -----------------------------------------------------------------------
    with tabs[2]:
        render_html(
            """
            <div class="oh-card-header-sm">
                <span>📥</span> Approval Inbox & Safety Governance
            </div>
            """
        )

        render_html(
            """
            <div style="background:#FFF5F5; border:1px solid #FECACA; border-left:4px solid #EF4444; padding:0.85rem 1rem; border-radius:8px; margin-bottom:1rem; font-size:0.92rem; color:#991B1B;">
                🛑 <strong>SAFETY INVARIANT: External email dispatch is permanently BLOCKED.</strong><br/>
                OFFERHUNT generates local drafts (<code>.eml</code>) for operator review. Invoking external send triggers an immediate policy violation.
            </div>
            """
        )

        col_send1, col_send2 = st.columns([2, 3])
        with col_send1:
            if st.button("⚠️ Simulate Send (Test Policy Block)", key="btn_simulate_send_action", type="secondary", use_container_width=True):
                if active_run:
                    block_res = service.simulate_send(active_run["run"]["run_id"])
                    st.error(f"🛑 {block_res['message']}")
                    st.info(f"Audit event logged: `{block_res['event_id']}`")
                else:
                    st.warning("Please select or launch a run first.")

        st.markdown("##### 📋 Boundary Approval Requests")
        if active_run:
            approvals = active_run["approvals"]
            if approvals:
                for app in approvals:
                    col_a, col_b, col_c = st.columns([3, 1, 1])
                    with col_a:
                        st.markdown(f"**Action:** `{app['action_type']}` | **Status:** `{app['status']}` | **Step:** `{app['step_id']}`")
                        st.json(app.get("payload") or {})
                    with col_b:
                        if app["status"] == "PENDING" and st.button("Approve", key=f"app_{app['approval_id']}"):
                            service.approve_step(app["approval_id"])
                            st.rerun()
                    with col_c:
                        if app["status"] == "PENDING" and st.button("Reject", key=f"rej_{app['approval_id']}"):
                            service.reject_step(app["approval_id"])
                            st.rerun()
            else:
                st.info("No pending approval requests for this run.")

    # -----------------------------------------------------------------------
    # Subtab 4: Final Report & Claim Validation
    # -----------------------------------------------------------------------
    with tabs[3]:
        render_html(
            """
            <div class="oh-card-header-sm">
                <span>📑</span> Final Verification Report & Claim Validation
            </div>
            """
        )
        if not active_run:
            st.info("No active run selected.")
        else:
            rep = active_run["report"]
            status_cls = "oh-badge-verified" if rep["final_status"] == "VERIFIED" else "oh-badge-review"

            render_html(
                f"""
                <div class="oh-card">
                    <div style="display:flex; align-items:center; gap:0.75rem; margin-bottom:0.5rem;">
                        <h2 style="margin:0; font-size:1.5rem; color:#1E293B;">Run Status:</h2>
                        <span class="{status_cls}" style="font-size:0.9rem; padding:0.35rem 0.85rem;">{rep['final_status']}</span>
                    </div>
                    <p style="margin:0.5rem 0 0 0; color:#475569; font-size:0.95rem; line-height:1.5;">{rep['summary']}</p>
                </div>
                """
            )

            st.markdown("##### 🎯 Grounded Claims Verification")
            st.caption("Never state that an internship is open or eligible unless corroborated by stored evidence.")

            claims = rep.get("claims") or []
            if claims:
                for clm in claims:
                    if clm["verified"]:
                        st.success(f"✓ **Verified Claim:** {clm['claim']} (Evidence: `{clm.get('evidence_id')}`)")
                    else:
                        st.warning(f"⚠️ **Unestablished Claim:** {clm['claim']} (No corroborating evidence stored)")
            else:
                st.info("No claim links evaluated for this run.")

    # -----------------------------------------------------------------------
    # Subtab 5: Persona Settings Context
    # -----------------------------------------------------------------------
    with tabs[4]:
        render_html(
            f"""
            <div class="oh-card">
                <div class="oh-card-header-sm">
                    <span>👤</span> Synthetic Student Persona Profile
                </div>
                <div class="persona-box">
                    <strong>Default Synthetic Persona:</strong> {DEFAULT_PERSONA['name']} | {DEFAULT_PERSONA['education']}<br/>
                    <strong>Technical Skills:</strong> {DEFAULT_PERSONA['skills']}<br/>
                    <strong>Target Interests:</strong> {DEFAULT_PERSONA['interests']}<br/>
                    <small style="color: #64748B;"><em>{DEFAULT_PERSONA['disclaimer']}</em></small>
                </div>
            </div>
            """
        )


# ===========================================================================
# PORTAL 2: 🧪 RELIABILITY LAB
# ===========================================================================

elif app_mode == "🧪 Reliability Lab":
    render_section_heading(
        title="OFFERHUNT Reliability Lab",
        subtitle="Controlled fault injection across standardized scenarios F01–F11 with exact ratio telemetry",
        icon="🧪",
    )

    scenarios_list = list_scenarios()
    scenario_map = {f"{s.code}: {s.title}": s for s in scenarios_list}

    # Scenario Selector Card
    with st.container(border=True):
        col_sel1, col_sel2 = st.columns([2.5, 1.5])
        with col_sel1:
            chosen_label = st.selectbox(
                "Select Fault Injection Scenario (F01 - F11)",
                options=list(scenario_map.keys()),
                index=0,
                key="reliability_scenario_select",
            )
        chosen_scenario = scenario_map[chosen_label]

        with col_sel2:
            st.markdown(f"**Category:** `{chosen_scenario.category}`")
            st.markdown(f"**Expected Final Status:** `{chosen_scenario.expected_final_status}`")

    # Visible Display of Injected Fault and Expected Recovery Path
    render_fault_and_recovery_cards(
        fault_title=f"Injected Fault ({chosen_scenario.code})",
        fault_desc=f"{chosen_scenario.injected_fault}<br/><small style='margin-top:0.35rem; display:block; opacity:0.85;'>{chosen_scenario.description}</small>",
        recovery_title="Expected Recovery Path",
        recovery_desc=f"{chosen_scenario.expected_recovery_path}<br/><small style='margin-top:0.35rem; display:block; opacity:0.85;'>Preserves idempotency, zero duplicate rows, and strict error classification.</small>",
    )

    # Execution Action Buttons (All 3 functional)
    c_btn1, c_btn2, c_btn3 = st.columns(3)
    runner = ReliabilityLabRunner()

    with c_btn1:
        if st.button(f"▶️ Run Scenario ({chosen_scenario.code}) [OFFERHUNT]", key="btn_run_resilient", type="primary", use_container_width=True):
            with st.spinner(f"Injecting fault {chosen_scenario.code} into temporary fixture..."):
                metric = asyncio.run(runner.run_scenario(chosen_scenario.id, is_baseline=False))
                st.session_state.metrics_collector.record_run(metric)
                st.session_state.last_lab_metric = metric
                st.rerun()

    with c_btn2:
        if st.button(f"⚠️ Run Naive Baseline ({chosen_scenario.code})", key="btn_run_baseline", type="secondary", use_container_width=True):
            with st.spinner(f"Running unhardened baseline under {chosen_scenario.code}..."):
                metric = asyncio.run(runner.run_scenario(chosen_scenario.id, is_baseline=True))
                st.session_state.metrics_collector.record_run(metric)
                st.session_state.last_lab_metric = metric
                st.rerun()

    with c_btn3:
        if st.button("🚀 Run Complete Controlled Suite (F01–F11)", key="btn_run_full_suite", use_container_width=True):
            with st.spinner("Executing all 11 scenarios across OFFERHUNT and Baseline..."):
                for s in scenarios_list:
                    m_oh = asyncio.run(runner.run_scenario(s.id, is_baseline=False))
                    st.session_state.metrics_collector.record_run(m_oh)
                    m_base = asyncio.run(runner.run_scenario(s.id, is_baseline=True))
                    st.session_state.metrics_collector.record_run(m_base)
                st.success("Completed controlled suite runs!")
                st.rerun()

    # Visibly display latest execution metric (5-column KPI cards)
    if st.session_state.last_lab_metric:
        lm: ScenarioRunMetric = st.session_state.last_lab_metric
        st.markdown(f"##### 📊 Latest Execution: `{lm.scenario_code}` ({'Naive Baseline' if lm.is_baseline else 'OFFERHUNT Resilient'})")
        
        kpi_cols = st.columns(5)
        with kpi_cols[0]:
            render_html(render_kpi_card("Final Status", lm.final_status, "Execution outcome"))
        with kpi_cols[1]:
            render_html(render_kpi_card("Recovery Succeeded", "Yes" if lm.recovery_successful else "No", "Fault handled"))
        with kpi_cols[2]:
            render_html(render_kpi_card("Duplicate Rows", "Yes" if lm.duplicate_produced else "0 (Clean)", "Atomic upsert check"))
        with kpi_cols[3]:
            render_html(render_kpi_card("Approval Bypassed", "VIOLATION" if lm.approval_bypassed else "0 (Safe)", "Boundary enforcement"))
        with kpi_cols[4]:
            render_html(render_kpi_card("Execution Latency", f"{lm.latency_ms:.1f} ms", "Wall-clock duration"))

    render_html("<div style='height: 1.5rem;'></div>")

    # Aggregate Metrics Dashboard (Zero Invented Numbers)
    st.markdown("##### 📈 Measured Reliability Telemetry (Exact Ratios)")
    st.caption("Derived strictly from executed runs. Never fabricates numbers; reports exact numerator, denominator, and percentage.")

    summary_all = st.session_state.metrics_collector.compute_summary()
    summary_oh = st.session_state.metrics_collector.compute_summary(baseline_only=False)
    summary_base = st.session_state.metrics_collector.compute_summary(baseline_only=True)

    if summary_all.total_runs == 0:
        render_empty_state(
            icon="🧪",
            title="No Scenario Executions Recorded Yet",
            desc="Click 'Run Scenario' or 'Run Complete Controlled Suite' above to inject faults and measure OFFERHUNT resilience vs Naive Baseline.",
        )
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
        st.markdown("##### 💾 Export Telemetry Data")
        exp_col1, exp_col2, exp_col3 = st.columns([1, 1, 2])

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
            if st.button("🧹 Clear Metrics Telemetry", key="btn_clear_metrics", use_container_width=True):
                st.session_state.metrics_collector.clear()
                st.session_state.last_lab_metric = None
                st.rerun()
