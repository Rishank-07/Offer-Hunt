"""Error classification, backoff calculation, and recovery reconciliation for OFFERHUNT (Milestone M5).

Implements recovery rules and state reconciliation per specification section 16:
- Error classification: VALIDATION, AUTH_OR_PERMISSION, QUOTA_OR_RATE_LIMIT,
  TRANSIENT_TRANSPORT, AMBIGUOUS_AFTER_EFFECT, PARTIAL_RESULT, VERIFICATION_MISMATCH, POLICY_BLOCK.
- Safe reconciliation for ambiguous outcomes (timeout after write): checks spreadsheet first;
  never blind-retries.
- Startup check that reconciles interrupted RUNNING steps without blindly replaying them.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from agent.contracts import compute_record_key
from agent.state_machine import (
    ExecutionStatus,
    StepState,
    VerificationStatus,
    reconcile_interrupted_steps,
)
from mcp_tools.spreadsheet import (
    DEFAULT_OUTPUT_DIR,
    SpreadsheetCorruptError,
    SpreadsheetSecurityError,
    read_spreadsheet_records,
)
from mcp_tools.tool_errors import MCPToolError, ToolErrorCode, scrub_secrets


class ErrorCategory(str, Enum):
    """Standardized error classifications per section 16."""
    VALIDATION = "VALIDATION"
    AUTH_OR_PERMISSION = "AUTH_OR_PERMISSION"
    QUOTA_OR_RATE_LIMIT = "QUOTA_OR_RATE_LIMIT"
    TRANSIENT_TRANSPORT = "TRANSIENT_TRANSPORT"
    AMBIGUOUS_AFTER_EFFECT = "AMBIGUOUS_AFTER_EFFECT"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"
    POLICY_BLOCK = "POLICY_BLOCK"


# Mapping error codes or exception types to classifications
NON_RETRYABLE_CATEGORIES: Set[ErrorCategory] = {
    ErrorCategory.VALIDATION,
    ErrorCategory.AUTH_OR_PERMISSION,
    ErrorCategory.POLICY_BLOCK,
    ErrorCategory.VERIFICATION_MISMATCH,
}


def classify_error(
    exc: Exception,
    tool_name: str = "",
    has_started_write: bool = False,
) -> ErrorCategory:
    """Classify an exception into standard categories based on error signatures and context.

    If a timeout or connection break happens after a write operation started,
    it is classified as AMBIGUOUS_AFTER_EFFECT rather than TRANSIENT_TRANSPORT.
    """
    err_str = scrub_secrets(str(exc)).lower()

    # Policy block
    if "policy" in err_str or "disallowed" in err_str or "unauthorized" in err_str and "approval" in err_str:
        return ErrorCategory.POLICY_BLOCK

    # Auth or permission
    if "401" in err_str or "403" in err_str or "permission_denied" in err_str or "invalid api key" in err_str:
        return ErrorCategory.AUTH_OR_PERMISSION

    # Quota or rate limit
    if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str or "rate limit" in err_str:
        return ErrorCategory.QUOTA_OR_RATE_LIMIT

    # Validation & security
    if isinstance(exc, (SpreadsheetSecurityError, ValueError)):
        return ErrorCategory.VALIDATION

    if isinstance(exc, MCPToolError):
        if exc.error_code in (
            ToolErrorCode.INVALID_ARGUMENT,
            ToolErrorCode.OVERSIZED_INPUT,
            ToolErrorCode.UNSAFE_URL,
            ToolErrorCode.PATH_TRAVERSAL,
            ToolErrorCode.DISALLOWED_PATH,
            ToolErrorCode.UNKNOWN_KEY,
        ):
            return ErrorCategory.VALIDATION
        if exc.error_code == ToolErrorCode.VERIFICATION_FAILED:
            return ErrorCategory.VERIFICATION_MISMATCH

    # Timeouts and network disconnects
    if (
        "timeout" in err_str
        or "connection reset" in err_str
        or "connection closed" in err_str
        or "broken pipe" in err_str
    ):
        if has_started_write:
            return ErrorCategory.AMBIGUOUS_AFTER_EFFECT
        return ErrorCategory.TRANSIENT_TRANSPORT

    # Ambiguous outcome
    if has_started_write:
        return ErrorCategory.AMBIGUOUS_AFTER_EFFECT

    return ErrorCategory.TRANSIENT_TRANSPORT


def compute_backoff_delay(
    attempt: int,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
    jitter: bool = True,
) -> float:
    """Compute exponential backoff with full jitter to avoid thundering herds.

    Formula: min(max_seconds, base_seconds * (2 ** (attempt - 1))) * uniform(0.5, 1.0)
    """
    if attempt <= 0:
        attempt = 1
    delay = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
    if jitter:
        delay = delay * (0.5 + random.random() * 0.5)
    return max(0.1, round(delay, 2))


def reconcile_ambiguous_spreadsheet_write(
    target_path: Union[str, Path],
    expected_keys: List[str],
    allowed_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
) -> tuple[bool, int]:
    """Reconcile an ambiguous spreadsheet write outcome.

    Reads the spreadsheet from disk to check if the expected records are already present.

    Returns:
        (all_present, found_count):
        - all_present: True if ALL expected_keys exist in the spreadsheet.
        - found_count: Number of expected_keys successfully found on disk.
    """
    allowed_dir_path = Path(allowed_dir).resolve()
    target = Path(target_path)
    if not target.is_absolute():
        resolved = (allowed_dir_path / target).resolve()
    else:
        resolved = target.resolve()

    if not resolved.exists():
        return False, 0

    try:
        _, existing_records = read_spreadsheet_records(resolved)
        found = 0
        for k in expected_keys:
            if k in existing_records:
                found += 1

        all_present = (found == len(expected_keys)) and len(expected_keys) > 0
        return all_present, found
    except Exception:
        return False, 0


def run_startup_reconciliation(db_conn_or_path: Any) -> List[dict[str, Any]]:
    """Startup check finding interrupted RUNNING steps and safely marking them UNRESOLVED."""
    return reconcile_interrupted_steps(db_conn_or_path)
