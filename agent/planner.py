"""Planner and GoalPlan generation engine for OFFERHUNT (Milestone M4).

Generates verified GoalPlans using Gemini structured output with Pydantic schemas,
strictly validates plans against live MCP catalogs and safety policies before execution,
and provides a clearly labeled MockPlanner for deterministic test/offline execution.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from agent.policy import (
    MAX_AGENT_STEPS,
    PlanValidationError,
    RiskLevel,
    classify_tool_risk,
    validate_plan_against_policy,
)
from mcp_tools.tool_errors import scrub_secrets
from storage.database import create_artifact, create_step, get_utc_iso


load_dotenv()


class PlannerModelError(RuntimeError):
    """Raised when the specified GEMINI_MODEL is unavailable or fails diagnostics.

    Guarantees no silent model fallbacks.
    """
    pass


class StepSpec(BaseModel):
    """Specification of a single planned agent step."""
    step_id: str = Field(..., description="Unique step identifier (e.g. step_01).")
    tool_name: str = Field(..., description="Name of the MCP tool to invoke.")
    typed_args: Dict[str, Any] = Field(default_factory=dict, description="Typed arguments for the tool.")
    dependencies: List[str] = Field(default_factory=list, description="IDs of prerequisite steps.")
    rationale: str = Field(..., description="Reasoning and justification for this step.")
    expected_artifact: Optional[str] = Field(default=None, description="Expected output artifact filename/path.")
    risk_level: str = Field(default=RiskLevel.READ_ONLY.value, description="Risk classification: read-only, local-write, external-write, irreversible.")
    retry_policy: Dict[str, Any] = Field(
        default_factory=lambda: {"max_retries": 2, "backoff": "exponential"},
        description="Retry policy config.",
    )
    verification_method: str = Field(
        default="file_exists_and_valid",
        description="Verification method identifier.",
    )

    @field_validator("risk_level")
    @classmethod
    def validate_risk(cls, v: str) -> str:
        valid = {r.value for r in RiskLevel}
        if v not in valid:
            return RiskLevel.READ_ONLY.value
        return v


class GoalPlan(BaseModel):
    """Structured execution plan for achieving an internship discovery goal."""
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:12]}")
    goal: str = Field(..., description="User's high-level internship search goal.")
    steps: List[StepSpec] = Field(..., description="Ordered list of steps forming a DAG.")
    planner_type: str = Field(default="GEMINI", description="GEMINI or MOCK.")
    model_name: Optional[str] = Field(default=None, description="Gemini model identifier used.")
    created_at_utc: str = Field(default_factory=get_utc_iso)


class BasePlanner:
    """Abstract base planner interface."""

    async def create_plan(
        self,
        goal: str,
        live_catalog_tool_names: Set[str],
    ) -> GoalPlan:
        raise NotImplementedError


class MockPlanner(BasePlanner):
    """Clearly labeled MOCK planner for offline use and automated testing.

    Label: 'MOCK'. Never presented or disguised as Gemini.
    Generates deterministic 3-6 step plans adhering to all safety invariants.
    """

    def __init__(self):
        self.planner_type = "MOCK"
        self.model_name = "MOCK"

    async def create_plan(
        self,
        goal: str,
        live_catalog_tool_names: Set[str],
    ) -> GoalPlan:
        """Create a valid deterministic 4-step plan for the given goal."""
        step1 = StepSpec(
            step_id="step_01_search",
            tool_name="search_internships",
            typed_args={"query": goal[:100], "max_results": 5},
            dependencies=[],
            rationale="Search online for matching internship opportunities.",
            expected_artifact=None,
            risk_level=RiskLevel.READ_ONLY.value,
            verification_method="evidence_captured",
        )
        step2 = StepSpec(
            step_id="step_02_upsert",
            tool_name="upsert_internships",
            typed_args={
                "records": [
                    {
                        "title": "Software Engineering Intern",
                        "company": "TopTech",
                        "location": "Remote",
                        "source_url": "https://careers.toptech.com/interns",
                        "stipend": "$40/hr",
                    }
                ],
                "target_path": "offers.xlsx",
            },
            dependencies=["step_01_search"],
            rationale="Atomically record discovered internships into the tracking spreadsheet.",
            expected_artifact="offers.xlsx",
            risk_level=RiskLevel.LOCAL_WRITE.value,
            verification_method="file_exists_and_valid",
        )
        step3 = StepSpec(
            step_id="step_03_verify",
            tool_name="verify_task_result",
            typed_args={"step_id": "step_02_upsert", "expected_artifact": "offers.xlsx"},
            dependencies=["step_02_upsert"],
            rationale="Verify spreadsheet artifact creation and integrity.",
            expected_artifact=None,
            risk_level=RiskLevel.READ_ONLY.value,
            verification_method="file_exists_and_valid",
        )
        step4 = StepSpec(
            step_id="step_04_draft",
            tool_name="create_email_draft",
            typed_args={
                "recipient": "internships@toptech.com",
                "subject": f"Inquiry regarding Internship Opportunities - {goal[:30]}",
                "body": "Hello,\nI am writing to express interest in the software engineering internship.",
            },
            dependencies=["step_03_verify"],
            rationale="Create local email inquiry draft without sending.",
            expected_artifact=None,
            risk_level=RiskLevel.LOCAL_WRITE.value,
            verification_method="file_exists_and_valid",
        )

        plan = GoalPlan(
            goal=goal,
            steps=[step1, step2, step3, step4],
            planner_type="MOCK",
            model_name="MOCK",
        )

        # Validate against policy before returning
        validate_plan_against_policy(plan, live_catalog_tool_names)
        return plan


class GeminiPlanner(BasePlanner):
    """Planner utilizing Google Gemini with structured Pydantic output.

    Requires GEMINI_MODEL. Fails with clear diagnostic if model is unavailable.
    NEVER silently switches to an alternative model.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.planner_type = "GEMINI"

    async def create_plan(
        self,
        goal: str,
        live_catalog_tool_names: Set[str],
    ) -> GoalPlan:
        """Call Gemini to generate a structured GoalPlan adhering to all policies."""
        if not self.api_key or self.api_key == "your_gemini_api_key_here":
            raise PlannerModelError(
                f"GEMINI_API_KEY is missing or invalid. Cannot invoke model '{self.model_name}'. "
                f"Fail with clear diagnostic: silent model fallback is prohibited."
            )

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)

            tools_list_str = ", ".join(sorted(list(live_catalog_tool_names)))
            prompt = (
                f"You are the planner for OFFERHUNT, an AI internship discovery agent.\n"
                f"User Goal: {goal}\n\n"
                f"Available tools in live catalog: {tools_list_str}\n"
                f"Constraints:\n"
                f"- Number of steps must be between 3 and 6 (maximum allowed: {MAX_AGENT_STEPS}).\n"
                f"- Every step tool_name MUST exist in the live catalog.\n"
                f"- Steps must form an acyclic dependency graph.\n"
                f"- Only search_internships, upsert_internships, read_internships, create_email_draft, verify_task_result, get_run_evidence may be used.\n"
                f"- Never propose shell tools or email sending tools.\n"
            )

            def _clean_schema(s: Any) -> Any:
                if isinstance(s, dict):
                    s.pop("additionalProperties", None)
                    s.pop("additional_properties", None)
                    for k, v in list(s.items()):
                        s[k] = _clean_schema(v)
                elif isinstance(s, list):
                    return [_clean_schema(item) for item in s]
                return s

            clean_schema = _clean_schema(GoalPlan.model_json_schema())

            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=clean_schema,
                ),
            )

            if not response.text:
                raise PlannerModelError(
                    f"Gemini model '{self.model_name}' returned empty response."
                )

            plan_dict = json.loads(response.text)
            plan = GoalPlan.model_validate(plan_dict)
            plan.model_name = self.model_name
            plan.planner_type = "GEMINI"

            # Pre-execution validation
            validate_plan_against_policy(plan, live_catalog_tool_names)
            return plan

        except PlanValidationError:
            raise
        except Exception as exc:
            clean_err = scrub_secrets(str(exc))
            raise PlannerModelError(
                f"Model '{self.model_name}' failed during plan generation: {clean_err}. "
                f"Aborting per policy: silent fallback to other models is strictly prohibited."
            ) from None


def persist_plan_and_catalog(
    conn: Any,
    run_id: str,
    plan: GoalPlan,
    catalog_snapshot: List[Any],
) -> None:
    """Persist the generated GoalPlan and the live tool catalog snapshot with the run."""
    plan_dict = plan.model_dump()
    plan_json = json.dumps(plan_dict, indent=2)

    # 1. Save plan as artifact
    plan_artifact_id = f"art_plan_{plan.plan_id}"
    create_artifact(
        conn=conn,
        artifact_id=plan_artifact_id,
        run_id=run_id,
        artifact_type="goal_plan",
        path=f"artifacts/plans/{plan.plan_id}.json",
        sha256="",
        metadata={"planner_type": plan.planner_type, "model_name": plan.model_name},
    )

    # 2. Save tool catalog snapshot as artifact
    catalog_dicts = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        if hasattr(t, "name")
        else dict(t)
        for t in catalog_snapshot
    ]
    create_artifact(
        conn=conn,
        artifact_id=f"art_cat_{run_id}",
        run_id=run_id,
        artifact_type="tool_catalog_snapshot",
        path=f"artifacts/catalogs/catalog_{run_id}.json",
        sha256="",
        metadata={"tools_count": len(catalog_dicts), "tools": [t["name"] for t in catalog_dicts]},
    )

    # 3. Create steps in DB
    for order, step in enumerate(plan.steps, start=1):
        create_step(
            conn=conn,
            step_id=step.step_id,
            run_id=run_id,
            step_order=order,
            state="READY",
            execution_status="NOT_STARTED",
            verification_status="UNVERIFIED",
            input_payload=step.model_dump(),
        )
