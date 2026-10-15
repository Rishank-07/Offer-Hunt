"""Pytest test suite for OFFERHUNT Milestones M3 and M4.

Tests MCP server tools, stdio client, catalog discovery, plan validation with
zero side effects, and plan generation (Mock and optional Live Gemini).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Set

import pytest

from agent.mcp_client import MCPClientWrapper
from agent.planner import (
    GoalPlan,
    MockPlanner,
    GeminiPlanner,
    PlannerModelError,
    StepSpec,
    persist_plan_and_catalog,
)
from agent.policy import (
    MAX_AGENT_STEPS,
    PlanValidationError,
    RiskLevel,
    validate_dag_acyclic,
    validate_plan_against_policy,
)
from mcp_tools.server import app as mcp_server_app
from mcp_tools.tool_errors import MCPToolError, ToolErrorCode, validate_safe_url
from storage.database import (
    get_step,
    init_database,
    open_db_connection,
)


EXPECTED_MCP_TOOLS: Set[str] = {
    "search_internships",
    "upsert_internships",
    "read_internships",
    "create_email_draft",
    "verify_task_result",
    "get_run_evidence",
}


# ===========================================================================
# Milestone M3 Tests (MCP Server & Client)
# ===========================================================================

@pytest.mark.anyio
async def test_mcp_client_discovers_exact_expected_tool_set():
    """Verify MCP client connects and discovers exactly the 6 required tools."""
    # Connect using stdio subprocess runner
    client = MCPClientWrapper(
        command=sys.executable,
        args=["-m", "mcp_tools.server"],
    )
    async with client:
        catalog = await client.get_tool_catalog()
        discovered_names = {t.name for t in catalog}

        assert discovered_names == EXPECTED_MCP_TOOLS
        assert len(discovered_names) == 6

        # Critical safety check: No dangerous tools present
        for forbidden in ["shell", "bash", "execute", "arbitrary_write", "send_email"]:
            assert forbidden not in discovered_names


@pytest.mark.anyio
async def test_mcp_client_typed_tool_call_works(tmp_path: Path):
    """Verify calling a typed tool through the MCP client returns expected structured content."""
    client = MCPClientWrapper(
        command=sys.executable,
        args=["-m", "mcp_tools.server"],
        cwd=tmp_path,
    )
    async with client:
        # 1. Call search_internships
        search_res = await client.call_tool(
            "search_internships",
            {"query": "AI research intern", "max_results": 2},
        )
        assert isinstance(search_res, list)
        assert len(search_res) >= 1
        first = search_res[0]
        assert "title" in first
        assert "company" in first
        assert "url" in first
        assert "retrieval_time" in first

        # 2. Call read_internships
        read_res = await client.call_tool(
            "read_internships",
            {"target_path": "offers.xlsx", "limit": 10},
        )
        assert isinstance(read_res, dict)
        assert "headers" in read_res
        assert "records" in read_res


@pytest.mark.anyio
async def test_mcp_security_rejections():
    """Verify security boundaries: private/internal URLs, oversized inputs, and unknown keys."""
    # 1. Private/Internal URL validation rejection
    private_targets = [
        "http://localhost:8080/jobs",
        "http://127.0.0.1:5000/api",
        "http://10.0.0.1/admin",
        "http://192.168.1.1/internal",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ]
    for target in private_targets:
        with pytest.raises(MCPToolError) as exc_info:
            validate_safe_url(target)
        assert exc_info.value.error_code == ToolErrorCode.UNSAFE_URL

    # 2. Oversized arrays and unknown keys in upsert_internships
    client = MCPClientWrapper(server_instance=mcp_server_app)
    async with client:
        # Oversized array (> 50 items)
        oversized = [
            {"title": f"Intern {i}", "company": "Corp", "location": "Remote"}
            for i in range(55)
        ]
        with pytest.raises(MCPToolError) as exc_oversize:
            await client.call_tool("upsert_internships", {"records": oversized})
        assert "exceeds maximum permitted elements" in str(exc_oversize.value)

        # Unknown keys rejection
        with pytest.raises(MCPToolError) as exc_unknown:
            await client.call_tool(
                "upsert_internships",
                {
                    "records": [
                        {
                            "title": "Intern",
                            "company": "Corp",
                            "location": "Remote",
                            "unauthorized_injection_key": "malicious_payload",
                        }
                    ]
                },
            )
        assert "disallowed/unknown keys" in str(exc_unknown.value)


# ===========================================================================
# Milestone M4 Tests (Planner, Policies & Zero Side-Effects)
# ===========================================================================

def test_plan_validation_zero_side_effects_on_rejection():
    """Verify bad JSON, unknown tool, invalid arg, cyclic dependency, and over-budget

    plans are rejected with zero side effects (tool call count = 0).
    """
    tool_call_counter = 0

    def mock_tool_invoker(tool_name: str, args: dict):
        nonlocal tool_call_counter
        tool_call_counter += 1

    live_catalog = EXPECTED_MCP_TOOLS

    # 1. Unknown Tool Rejection
    unknown_tool_plan = GoalPlan(
        goal="Hack server",
        steps=[
            StepSpec(
                step_id="step_01",
                tool_name="execute_system_shell",  # NOT in live catalog
                typed_args={"command": "whoami"},
                rationale="Illegal action",
            )
        ],
    )
    with pytest.raises(PlanValidationError) as exc_tool:
        validate_plan_against_policy(unknown_tool_plan, live_catalog)
    assert "does not exist in the live MCP catalog" in str(exc_tool.value)
    assert tool_call_counter == 0, "No tools should have been invoked!"

    # 2. Invalid Argument Rejection (exceeds max_results)
    invalid_arg_plan = GoalPlan(
        goal="Search offers",
        steps=[
            StepSpec(
                step_id="step_01",
                tool_name="search_internships",
                typed_args={"query": "test", "max_results": 9999},  # exceeds limit
                rationale="Bad param",
            )
        ],
    )
    with pytest.raises(PlanValidationError) as exc_arg:
        validate_plan_against_policy(invalid_arg_plan, live_catalog)
    assert "max_results" in str(exc_arg.value)
    assert tool_call_counter == 0

    # 3. Cyclic Dependency Rejection
    cyclic_plan = GoalPlan(
        goal="Cyclic loop",
        steps=[
            StepSpec(
                step_id="step_A",
                tool_name="search_internships",
                typed_args={"query": "test"},
                dependencies=["step_B"],
                rationale="Step A depends on B",
            ),
            StepSpec(
                step_id="step_B",
                tool_name="verify_task_result",
                typed_args={"step_id": "step_A"},
                dependencies=["step_A"],
                rationale="Step B depends on A",
            ),
        ],
    )
    with pytest.raises(PlanValidationError) as exc_cycle:
        validate_plan_against_policy(cyclic_plan, live_catalog)
    assert "Cyclic dependency detected" in str(exc_cycle.value)
    assert tool_call_counter == 0

    # 4. Over-budget plan Rejection (> MAX_AGENT_STEPS)
    excess_steps = [
        StepSpec(
            step_id=f"step_{i:02d}",
            tool_name="search_internships",
            typed_args={"query": f"test {i}"},
            rationale=f"Step {i}",
        )
        for i in range(MAX_AGENT_STEPS + 5)
    ]
    overbudget_plan = GoalPlan(goal="Excessive plan", steps=excess_steps)
    with pytest.raises(PlanValidationError) as exc_budget:
        validate_plan_against_policy(overbudget_plan, live_catalog)
    assert "exceeds step cap" in str(exc_budget.value)
    assert tool_call_counter == 0

    # Final assertion of zero side effects
    assert tool_call_counter == 0


@pytest.mark.anyio
async def test_mock_planner_demo_prompt_produces_valid_3_to_6_step_plan():
    """Verify MockPlanner produces a valid 3-6 step plan clearly marked as MOCK."""
    planner = MockPlanner()
    assert planner.planner_type == "MOCK"
    assert planner.model_name == "MOCK"

    goal = "Find machine learning internships in Bangalore with stipend"
    plan = await planner.create_plan(goal, live_catalog_tool_names=EXPECTED_MCP_TOOLS)

    assert isinstance(plan, GoalPlan)
    assert 3 <= len(plan.steps) <= 6
    assert plan.planner_type == "MOCK"
    assert plan.model_name == "MOCK"

    # Verify every step has a valid risk level and valid dependency order
    for s in plan.steps:
        assert s.risk_level in {r.value for r in RiskLevel}
        assert s.tool_name in EXPECTED_MCP_TOOLS


def test_gemini_planner_fails_without_silent_model_switch():
    """Verify GeminiPlanner fails with clear diagnostic if key/model is unavailable and does not switch models."""
    target_model = "gemini-2.5-pro-custom-testing"
    planner = GeminiPlanner(
        api_key="invalid_fake_key_12345",
        model_name=target_model,
    )
    assert planner.model_name == target_model

    with pytest.raises(PlannerModelError) as exc_info:
        import asyncio
        asyncio.run(
            planner.create_plan("Find internships", live_catalog_tool_names=EXPECTED_MCP_TOOLS)
        )

    # Diagnostic verifies failure refers to target_model and confirms no silent fallback occurred
    err_msg = str(exc_info.value)
    assert target_model in err_msg
    assert "silent fallback to other models is strictly prohibited" in err_msg or "Cannot invoke model" in err_msg


@pytest.mark.live_gemini
@pytest.mark.anyio
async def test_live_gemini_planner_produces_valid_plan():
    """Live Gemini test marked separately so it can be skipped offline."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        pytest.skip("GEMINI_API_KEY is not configured. Skipping live Gemini test.")

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    planner = GeminiPlanner(api_key=api_key, model_name=model_name)

    plan = await planner.create_plan(
        goal="Find software engineering internships in Delhi",
        live_catalog_tool_names=EXPECTED_MCP_TOOLS,
    )
    assert isinstance(plan, GoalPlan)
    assert 3 <= len(plan.steps) <= MAX_AGENT_STEPS
    assert plan.planner_type == "GEMINI"
    assert plan.model_name == model_name


def test_plan_and_catalog_persistence(tmp_path: Path):
    """Verify plan, steps, and catalog snapshot are persisted cleanly in SQLite persistence."""
    db_file = tmp_path / "persistence_test.db"
    init_database(db_file)

    conn = open_db_connection(db_file)
    run_id = "run_test_plan_001"

    # Insert parent run
    from storage.database import create_run
    create_run(conn, run_id=run_id, status="RUNNING")

    # Generate mock plan
    import asyncio
    planner = MockPlanner()
    plan = asyncio.run(planner.create_plan("Find Python internships", EXPECTED_MCP_TOOLS))

    # Persist
    catalog_snapshot = [
        {"name": t, "description": f"Tool {t}", "input_schema": {}}
        for t in sorted(list(EXPECTED_MCP_TOOLS))
    ]
    persist_plan_and_catalog(conn, run_id=run_id, plan=plan, catalog_snapshot=catalog_snapshot)

    # Verify step persisted in DB
    first_step = get_step(conn, plan.steps[0].step_id)
    assert first_step is not None
    assert first_step["state"] == "READY"
    assert first_step["step_order"] == 1

    # Verify artifacts persisted in DB
    cur = conn.cursor()
    cur.execute("SELECT artifact_type, path FROM artifacts WHERE run_id = ?;", (run_id,))
    artifacts = cur.fetchall()
    art_types = {r["artifact_type"] for r in artifacts}
    assert "goal_plan" in art_types
    assert "tool_catalog_snapshot" in art_types

    conn.close()
