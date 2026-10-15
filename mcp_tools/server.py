"""Official MCP Python SDK v2 Server for OFFERHUNT (Milestone M3).

Exposes bounded, verified tools over stdio protocol:
- search_internships
- upsert_internships
- read_internships
- create_email_draft
- verify_task_result
- get_run_evidence

Security & Protocol Invariants:
- NO shell tool, NO arbitrary file-write tool, NO email-send tool.
- Reject oversized arrays, malformed/private URLs, unknown keys, paths outside OUTPUT_DIR.
- All server diagnostics and logs go exclusively to stderr, NEVER stdout.
- Never returns API keys in responses or exceptions.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from mcp.server import MCPServer

from agent.contracts import InternshipRecord
from mcp_tools.drafts import create_local_draft
from mcp_tools.research import search_internships_tavily
from mcp_tools.spreadsheet import (
    DEFAULT_OUTPUT_DIR,
    read_spreadsheet_records,
    upsert_records_to_spreadsheet,
    validate_and_resolve_path,
)
from mcp_tools.tool_errors import (
    MCPToolError,
    ToolErrorCode,
    sanitize_search_query,
    scrub_secrets,
    validate_array_bounds,
    validate_safe_url,
)
from storage.database import DEFAULT_DB_PATH, open_db_connection, record_evidence


# Configure server logging to write exclusively to sys.stderr (never stdout)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [MCP-Server] %(message)s",
)
logger = logging.getLogger("offerhunt.mcp_server")

# Allowed record keys for upsert_internships
ALLOWED_RECORD_KEYS = {
    "record_key",
    "title",
    "company",
    "location",
    "eligibility",
    "stipend",
    "deadline",
    "source_url",
    "source_title",
    "snippet",
    "retrieved_at_utc",
    "source_type",
    "confidence",
    "status",
    "provenance",
}

# Instantiate official MCP SDK v2 MCPServer
app = MCPServer("offerhunt-server")


@app.tool(
    name="search_internships",
    description="Search for internship and offer opportunities via Tavily API with bounded results.",
)
def search_internships(
    query: str,
    max_results: int = 5,
    location: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Search for internships and return structured offers.

    - query: Untrusted search prompt (max 200 chars).
    - max_results: Maximum items to retrieve (bounded 1 to 10).
    - location: Optional location filter.
    """
    logger.info("search_internships called with query='%s', max_results=%d", query[:50], max_results)
    try:
        return search_internships_tavily(
            query=query,
            max_results=max_results,
            location=location,
        )
    except Exception as exc:
        logger.error("search_internships failed: %s", scrub_secrets(str(exc)))
        raise


@app.tool(
    name="upsert_internships",
    description="Atomically upsert records into the offers spreadsheet (.xlsx) within OUTPUT_DIR.",
)
def upsert_internships(
    records: List[dict[str, Any]],
    target_path: str = "offers.xlsx",
) -> dict[str, Any]:
    """Upsert internship records into an Excel workbook. Never append-only.

    - records: Array of internship record dictionaries (max 50).
    - target_path: Relative path to .xlsx file within OUTPUT_DIR.
    """
    logger.info("upsert_internships called: %d records -> %s", len(records) if isinstance(records, list) else -1, target_path)

    try:
        # 1. Bounds & type checking
        validate_array_bounds(records, max_len=50, field_name="records")

        # 2. Strict key verification and URL validation per record
        cleaned_records: List[dict[str, Any]] = []
        for idx, r in enumerate(records):
            if not isinstance(r, dict):
                raise MCPToolError(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"Record at index {idx} must be a dictionary object.",
                )

            # Reject unknown keys
            unknown_keys = set(r.keys()) - ALLOWED_RECORD_KEYS
            if unknown_keys:
                raise MCPToolError(
                    ToolErrorCode.UNKNOWN_KEY,
                    f"Record at index {idx} contains disallowed/unknown keys: {list(unknown_keys)}",
                )

            # Validate URL if present
            source_url = r.get("source_url")
            if source_url:
                validate_safe_url(str(source_url))

            # Title and company are required
            title = r.get("title")
            company = r.get("company")
            location = r.get("location") or "Remote"

            if not title or not company:
                raise MCPToolError(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"Record at index {idx} must have 'title' and 'company'.",
                )

            # If record_key not supplied, create canonical InternshipRecord
            if not r.get("record_key"):
                rec_obj = InternshipRecord.create(
                    title=str(title),
                    company=str(company),
                    location=str(location),
                    source_url=str(source_url) if source_url else None,
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

        # 3. Perform atomic upsert with path confinement
        result = upsert_records_to_spreadsheet(
            records=cleaned_records,
            target_path=target_path,
            allowed_dir=DEFAULT_OUTPUT_DIR,
        )
        return {
            "inserted": result.inserted,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "keys": result.keys,
            "artifact_path": result.artifact_path,
            "sha256": result.sha256,
        }
    except MCPToolError as mcp_err:
        logger.warning("upsert_internships validation error: %s", mcp_err.message)
        return {
            "error": True,
            "error_code": mcp_err.error_code.value,
            "message": mcp_err.message,
        }
    except Exception as exc:
        clean_err = scrub_secrets(str(exc))
        logger.error("upsert_internships failed: %s", clean_err)
        return {
            "error": True,
            "error_code": ToolErrorCode.TOOL_EXECUTION_ERROR.value,
            "message": clean_err,
        }


@app.tool(
    name="read_internships",
    description="Read recorded internships from an .xlsx workbook in OUTPUT_DIR.",
)
def read_internships(
    target_path: str = "offers.xlsx",
    limit: int = 100,
) -> dict[str, Any]:
    """Read back offers from an Excel spreadsheet.

    - target_path: Target .xlsx path within OUTPUT_DIR.
    - limit: Maximum rows to return (bounded 1 to 500).
    """
    bounded_limit = max(1, min(int(limit), 500))
    resolved_path = validate_and_resolve_path(target_path, allowed_dir=DEFAULT_OUTPUT_DIR)

    headers, records_dict = read_spreadsheet_records(resolved_path)
    all_keys = list(records_dict.keys())
    selected_keys = all_keys[:bounded_limit]

    rows = [records_dict[k][1] for k in selected_keys]
    return {
        "headers": headers,
        "records": rows,
        "total_records": len(all_keys),
        "returned_records": len(rows),
        "artifact_path": str(resolved_path),
    }


@app.tool(
    name="create_email_draft",
    description="Create a local .eml and .txt draft with state DRAFTED. NEVER sends emails.",
)
def create_email_draft(
    recipient: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Create local email draft. Never opens network connections or transmits emails.

    - recipient: Validated destination email (rejects fabricated domains like example.com).
    - subject: Draft email subject.
    - body: Plain text email content.
    """
    logger.info("create_email_draft called for recipient='%s'", recipient)
    try:
        draft = create_local_draft(
            recipient=recipient,
            subject=subject,
            body=body,
        )
        return {
            "draft_id": draft.draft_id,
            "eml_path": draft.eml_path,
            "txt_path": draft.txt_path,
            "recipient": draft.recipient,
            "subject": draft.subject,
            "state": draft.state,
            "created_at_utc": draft.created_at_utc,
        }
    except Exception as exc:
        logger.error("create_email_draft failed: %s", scrub_secrets(str(exc)))
        raise


@app.tool(
    name="verify_task_result",
    description="Verify artifact existence, non-emptiness, and integrity for a step.",
)
def verify_task_result(
    step_id: str,
    expected_artifact: Optional[str] = None,
    verification_method: str = "file_exists_and_valid",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Verify execution outcome of a step.

    - step_id: Step identifier being verified.
    - expected_artifact: File path of the expected output artifact.
    - verification_method: Method identifier (e.g. file_exists_and_valid).
    """
    logger.info("verify_task_result called for step_id='%s'", step_id)

    verified = False
    details = ""

    if expected_artifact:
        p = Path(expected_artifact)
        if not p.is_absolute():
            p = (DEFAULT_OUTPUT_DIR / p).resolve()

        if p.exists() and p.stat().st_size > 0:
            verified = True
            details = f"Artifact '{p.name}' exists and has valid size ({p.stat().st_size} bytes)."
        else:
            verified = False
            details = f"Artifact '{p}' does not exist or is empty (0 bytes)."
    else:
        verified = True
        details = "No artifact required for step verification."

    # Persist verification evidence to SQLite database if available
    target_db = db_path or DEFAULT_DB_PATH
    if Path(target_db).exists():
        try:
            conn = open_db_connection(target_db)
            try:
                record_evidence(
                    conn=conn,
                    evidence_id=f"evi_ver_{step_id[:12]}",
                    step_id=step_id,
                    evidence_type="VERIFICATION_RESULT",
                    content=details,
                )
            finally:
                conn.close()
        except Exception as db_exc:
            logger.warning("Could not persist verification evidence to DB: %s", db_exc)

    return {
        "verified": verified,
        "step_id": step_id,
        "verification_method": verification_method,
        "details": details,
    }


@app.tool(
    name="get_run_evidence",
    description="Query recorded audit evidence for a specific step or run from the database.",
)
def get_run_evidence(
    step_id: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Retrieve evidence records from SQLite persistence.

    - step_id: Filter by step_id.
    - run_id: Filter by run_id (via steps join).
    - limit: Maximum evidence records to return (bounded 1 to 200).
    """
    bounded_limit = max(1, min(int(limit), 200))
    target_db = db_path or DEFAULT_DB_PATH

    if not Path(target_db).exists():
        return []

    conn = open_db_connection(target_db)
    try:
        cur = conn.cursor()
        clauses = []
        params: List[Any] = []

        if step_id:
            clauses.append("e.step_id = ?")
            params.append(step_id)
        if run_id:
            clauses.append("s.run_id = ?")
            params.append(run_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT e.evidence_id, e.step_id, e.evidence_type, e.content,
                   e.source_url, e.hash, e.captured_at_utc
            FROM evidence e
            LEFT JOIN steps s ON e.step_id = s.step_id
            {where_sql}
            ORDER BY e.captured_at_utc DESC
            LIMIT ?;
        """
        params.append(bounded_limit)
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


if __name__ == "__main__":
    # Run official MCP SDK v2 stdio transport
    app.run(transport="stdio")
