"""Security policies, risk classification, and plan validation for OFFERHUNT (Milestone M4).

Defines:
- Risk classes: read-only, local-write, external-write, irreversible.
- Step count cap (MAX_AGENT_STEPS).
- Graph validation ensuring dependency DAG exists and is strictly acyclic.
- Pre-execution validation enforcing argument boundaries, safe paths, and live catalog containment.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from mcp_tools.spreadsheet import DEFAULT_OUTPUT_DIR
from mcp_tools.tool_errors import ToolErrorCode, validate_safe_url


class RiskLevel(str, Enum):
    """Operation risk classes per specification."""
    READ_ONLY = "read-only"
    LOCAL_WRITE = "local-write"
    EXTERNAL_WRITE = "external-write"
    IRREVERSIBLE = "irreversible"


# Global policy constraints
MAX_AGENT_STEPS = 10
MAX_SEARCH_RESULTS = 10

# Default tool risk classification mapping
TOOL_RISK_MAP: Dict[str, RiskLevel] = {
    "search_internships": RiskLevel.READ_ONLY,
    "read_internships": RiskLevel.READ_ONLY,
    "verify_task_result": RiskLevel.READ_ONLY,
    "get_run_evidence": RiskLevel.READ_ONLY,
    "upsert_internships": RiskLevel.LOCAL_WRITE,
    "create_email_draft": RiskLevel.LOCAL_WRITE,
}


class PlanValidationError(ValueError):
    """Raised when a proposed GoalPlan fails structural, security, or DAG validation."""

    def __init__(self, message: str, step_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.step_id = step_id


def classify_tool_risk(tool_name: str) -> RiskLevel:
    """Classify risk level for a given tool name."""
    return TOOL_RISK_MAP.get(tool_name, RiskLevel.EXTERNAL_WRITE)


def validate_dag_acyclic(steps: List[Any]) -> None:
    """Validate that the step dependencies form a valid directed acyclic graph (DAG).

    Raises:
        PlanValidationError if a dependency does not exist or a cycle is detected.
    """
    step_ids: Set[str] = set()
    adj: Dict[str, List[str]] = {}

    for s in steps:
        s_id = s.get("step_id") if isinstance(s, dict) else getattr(s, "step_id", None)
        if not s_id:
            raise PlanValidationError("Every step must have a non-empty 'step_id'.")
        if s_id in step_ids:
            raise PlanValidationError(f"Duplicate step_id detected: '{s_id}'.", step_id=s_id)
        step_ids.add(s_id)
        adj[s_id] = []

    # Build adjacency list (dep -> step)
    for s in steps:
        s_id = s.get("step_id") if isinstance(s, dict) else getattr(s, "step_id", None)
        deps = (s.get("dependencies") if isinstance(s, dict) else getattr(s, "dependencies", None)) or []
        for d in deps:
            if d not in step_ids:
                raise PlanValidationError(
                    f"Step '{s_id}' depends on non-existent step '{d}'.",
                    step_id=s_id,
                )
            if d == s_id:
                raise PlanValidationError(
                    f"Self-referencing cyclic dependency on step '{s_id}'.",
                    step_id=s_id,
                )
            adj[d].append(s_id)

    # Detect cycles using 3-color DFS
    # 0 = unvisited, 1 = visiting (in recursion stack), 2 = visited
    state: Dict[str, int] = {node: 0 for node in step_ids}

    def dfs(node: str) -> None:
        state[node] = 1
        for neighbor in adj.get(node, []):
            if state[neighbor] == 1:
                raise PlanValidationError(
                    f"Cyclic dependency detected involving step '{node}' and step '{neighbor}'.",
                    step_id=node,
                )
            if state[neighbor] == 0:
                dfs(neighbor)
        state[node] = 2

    for node in step_ids:
        if state[node] == 0:
            dfs(node)


def validate_step_arguments(tool_name: str, args: dict[str, Any], step_id: str) -> None:
    """Validate argument types, bounds, and path safety for an individual step."""
    if not isinstance(args, dict):
        raise PlanValidationError(
            f"Step '{step_id}': 'typed_args' must be a dictionary object.",
            step_id=step_id,
        )

    # Specific tool argument validations
    if tool_name == "search_internships":
        query = args.get("query")
        if not query or not isinstance(query, str):
            raise PlanValidationError(
                f"Step '{step_id}': 'search_internships' requires a non-empty string 'query'.",
                step_id=step_id,
            )
        max_res = args.get("max_results", 5)
        if not isinstance(max_res, int) or max_res > MAX_SEARCH_RESULTS or max_res < 1:
            raise PlanValidationError(
                f"Step '{step_id}': 'max_results' must be an integer between 1 and {MAX_SEARCH_RESULTS}.",
                step_id=step_id,
            )

    elif tool_name == "upsert_internships":
        records = args.get("records")
        if not isinstance(records, list):
            raise PlanValidationError(
                f"Step '{step_id}': 'upsert_internships' requires a list of 'records'.",
                step_id=step_id,
            )
        if len(records) > 50:
            raise PlanValidationError(
                f"Step '{step_id}': 'records' list exceeds 50 items (found {len(records)}).",
                step_id=step_id,
            )
        target_path = str(args.get("target_path", "offers.xlsx"))
        if ".." in target_path or Path(target_path).is_absolute():
            # Check directory confinement
            p = (DEFAULT_OUTPUT_DIR / target_path).resolve()
            try:
                p.relative_to(DEFAULT_OUTPUT_DIR)
            except ValueError:
                raise PlanValidationError(
                    f"Step '{step_id}': target_path '{target_path}' attempts path traversal.",
                    step_id=step_id,
                )

    elif tool_name == "create_email_draft":
        recipient = args.get("recipient")
        if not recipient or not isinstance(recipient, str):
            raise PlanValidationError(
                f"Step '{step_id}': 'create_email_draft' requires a valid 'recipient' string.",
                step_id=step_id,
            )
        if "@" not in recipient:
            raise PlanValidationError(
                f"Step '{step_id}': invalid email syntax for recipient '{recipient}'.",
                step_id=step_id,
            )


def validate_plan_against_policy(
    plan: Any,
    live_catalog_tool_names: Set[str],
) -> None:
    """Comprehensive pre-execution plan validator.

    Verifies:
    1. Step count <= MAX_AGENT_STEPS.
    2. All tools in plan exist in live MCP tool catalog.
    3. Step arguments meet bounds and security requirements.
    4. Dependency graph is fully connected and acyclic.
    """
    steps = getattr(plan, "steps", None) or plan.get("steps") or []

    # 1. Step cap enforcement
    if not steps:
        raise PlanValidationError("Plan must contain at least 1 step.")
    if len(steps) > MAX_AGENT_STEPS:
        raise PlanValidationError(
            f"Plan exceeds step cap: {len(steps)} steps proposed (max {MAX_AGENT_STEPS})."
        )

    # 2. Tool existence in live catalog & argument validation
    for s in steps:
        s_id = s.get("step_id") if isinstance(s, dict) else getattr(s, "step_id", None)
        t_name = s.get("tool_name") if isinstance(s, dict) else getattr(s, "tool_name", None)
        args = (s.get("typed_args") if isinstance(s, dict) else getattr(s, "typed_args", None)) or {}

        if t_name not in live_catalog_tool_names:
            raise PlanValidationError(
                f"Tool '{t_name}' requested in step '{s_id}' does not exist in the live MCP catalog. "
                f"Available tools: {sorted(list(live_catalog_tool_names))}",
                step_id=s_id,
            )

        validate_step_arguments(tool_name=t_name, args=args, step_id=s_id)

    # 3. DAG cycle check
    validate_dag_acyclic(steps)


# ---------------------------------------------------------------------------
# Approval Enforcement at Executor/Tool Boundary (M6)
# ---------------------------------------------------------------------------

import hashlib
import json
import sqlite3

FORBIDDEN_ACTIONS = {
    "send_email",
    "send_mail",
    "transmit_email",
    "execute_shell",
    "run_bash",
    "run_command",
    "arbitrary_write",
    "delete_file",
}


def compute_action_digest(action_type: str, payload: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of an action and its canonical arguments."""
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    content = f"{action_type}:{canonical_payload}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def check_action_policy_allowed(action_type: str, tool_name: str) -> None:
    """Verify that an action or tool is not permanently prohibited by safety policies.

    Raises:
        PlanValidationError with POLICY_BLOCK if action is strictly forbidden.
    """
    clean_action = action_type.lower().strip()
    clean_tool = tool_name.lower().strip()
    if clean_action in FORBIDDEN_ACTIONS or clean_tool in FORBIDDEN_ACTIONS:
        raise PlanValidationError(
            f"Action '{action_type}' / Tool '{tool_name}' is permanently blocked by safety policy (POLICY_BLOCK).",
        )
    if "send" in clean_action or "send" in clean_tool:
        raise PlanValidationError(
            f"Simulated or real send actions are strictly prohibited: '{action_type}' (POLICY_BLOCK).",
        )


def verify_executor_approval(
    conn: sqlite3.Connection,
    step_id: str,
    action_type: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Verify approval state at the executor/tool boundary.

    Enforces:
    - Approvals are cryptographically bound to action digest (action_type + canonical payload).
    - Status must be APPROVED. If REJECTED, immediately blocks execution with no retries.
    - If payload changed since approval was granted, approval is marked EXPIRED/stale and blocked.

    Returns:
        (is_approved, status):
        - (True, "APPROVED") if valid approval exists with matching digest.
        - (False, status) if approval is pending, rejected, expired, or missing.
    """
    # 1. First enforce permanent policy blocks
    check_action_policy_allowed(action_type, tool_name=action_type)

    current_digest = compute_action_digest(action_type, payload)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT approval_id, action_type, payload, status
        FROM approvals
        WHERE step_id = ?
        ORDER BY requested_at_utc DESC
        LIMIT 1;
        """,
        (step_id,),
    )
    row = cur.fetchone()

    if not row:
        return False, "NO_APPROVAL_RECORD"

    status = row["status"]
    saved_payload = json.loads(row["payload"]) if row["payload"] else {}
    saved_digest = compute_action_digest(row["action_type"], saved_payload)

    # Check if arguments changed (stale approval detection)
    if saved_digest != current_digest:
        # Mark approval as EXPIRED due to argument mutation
        with conn:
            conn.execute(
                "UPDATE approvals SET status = 'EXPIRED' WHERE approval_id = ?;",
                (row["approval_id"],),
            )
        return False, "EXPIRED"

    if status == "APPROVED":
        return True, "APPROVED"
    elif status == "REJECTED":
        return False, "REJECTED"
    elif status == "EXPIRED":
        return False, "EXPIRED"

    return False, "PENDING"
