"""Independent verifier and proof-validation engine for OFFERHUNT (Milestone M6).

Strictly independent of LLM text and executor state.
Verifies spreadsheet workbooks and local email drafts directly from disk,
computes cryptographic SHA-256 checksums, maps claims to stored evidence,
and derives final run status with the invariant: summary text can NEVER override a failed check.
"""

from __future__ import annotations

import email
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import openpyxl
from pydantic import BaseModel, Field

from agent.contracts import compute_record_key
from mcp_tools.spreadsheet import (
    DEFAULT_OUTPUT_DIR,
    SPREADSHEET_COLUMNS,
    compute_file_sha256,
    read_spreadsheet_records,
)
from storage.database import (
    DEFAULT_DB_PATH,
    get_utc_iso,
    open_db_connection,
    record_evidence,
)


class RunFinalStatus(str, Enum):
    """Final run lifecycle status values."""
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VerificationCheck(BaseModel):
    """Machine-readable result of an individual verification rule."""
    name: str = Field(..., description="Rule name (e.g. required_columns, unique_keys).")
    passed: bool = Field(..., description="True if check passed.")
    message: str = Field(..., description="Descriptive status message.")
    evidence_id: Optional[str] = Field(default=None, description="Linked evidence record ID if captured.")


class ClaimEvidenceLink(BaseModel):
    """Links an extracted claim to a verified evidence record."""
    claim: str
    verified: bool
    evidence_id: Optional[str] = None
    source_url: Optional[str] = None
    content_hash: Optional[str] = None
    label: str  # "verified" or "not verified"


class VerificationReport(BaseModel):
    """Machine-readable comprehensive verification report."""
    run_id: str
    passed: bool
    final_status: str
    checks: List[VerificationCheck] = Field(default_factory=list)
    claims: List[ClaimEvidenceLink] = Field(default_factory=list)
    artifact_hashes: Dict[str, str] = Field(default_factory=dict)
    summary: str
    verified_at_utc: str = Field(default_factory=get_utc_iso)


class IndependentVerifier:
    """Independent verification engine inspecting physical disk artifacts and evidence records."""

    def __init__(
        self,
        db_path: Union[str, Path] = DEFAULT_DB_PATH,
        artifacts_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    ):
        self.db_path = Path(db_path).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve()

    def verify_spreadsheet(
        self,
        target_path: Union[str, Path],
        expected_min_rows: int = 1,
    ) -> tuple[List[VerificationCheck], Dict[str, str]]:
        """Reopen spreadsheet from disk and verify structural and data integrity."""
        checks: List[VerificationCheck] = []
        artifact_hashes: Dict[str, str] = {}

        p = Path(target_path)
        if not p.is_absolute():
            resolved = (self.artifacts_dir / p).resolve()
        else:
            resolved = p.resolve()

        # 1. Existence check
        if not resolved.exists():
            checks.append(
                VerificationCheck(
                    name="file_exists",
                    passed=False,
                    message=f"Spreadsheet file does not exist at '{resolved}'.",
                )
            )
            return checks, artifact_hashes

        checks.append(
            VerificationCheck(
                name="file_exists",
                passed=True,
                message=f"Spreadsheet file exists at '{resolved}'.",
            )
        )

        # Compute SHA-256
        sha256 = compute_file_sha256(resolved)
        artifact_hashes[resolved.name] = sha256

        # 2. Open workbook with openpyxl directly
        try:
            wb = openpyxl.load_workbook(resolved, data_only=True)
            ws = wb.active
            if ws is None:
                checks.append(
                    VerificationCheck(
                        name="workbook_readable",
                        passed=False,
                        message="Workbook does not contain an active sheet.",
                    )
                )
                return checks, artifact_hashes

            checks.append(
                VerificationCheck(
                    name="workbook_readable",
                    passed=True,
                    message="Workbook loaded successfully via openpyxl.",
                )
            )
        except Exception as exc:
            checks.append(
                VerificationCheck(
                    name="workbook_readable",
                    passed=False,
                    message=f"Failed to load workbook from disk: {exc}",
                )
            )
            return checks, artifact_hashes

        # 3. Required columns check
        header_vals = [str(cell.value or "").strip() for cell in ws[1]]
        missing_cols = [col for col in SPREADSHEET_COLUMNS if col not in header_vals]
        if missing_cols:
            checks.append(
                VerificationCheck(
                    name="required_columns",
                    passed=False,
                    message=f"Missing required columns: {missing_cols}",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="required_columns",
                    passed=True,
                    message=f"All {len(SPREADSHEET_COLUMNS)} required columns present.",
                )
            )

        # 4. Read rows, check row count, uniqueness of record_key, non-empty source URLs
        key_col_idx = header_vals.index("record_key") if "record_key" in header_vals else -1
        url_col_idx = header_vals.index("source_url") if "source_url" in header_vals else -1

        keys_seen: Set[str] = set()
        duplicate_keys: List[str] = []
        empty_url_rows: List[int] = []
        valid_row_count = 0

        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not row or all(v is None for v in row):
                continue
            valid_row_count += 1

            if key_col_idx >= 0 and key_col_idx < len(row):
                key = str(row[key_col_idx] or "").strip()
                if key:
                    if key in keys_seen:
                        duplicate_keys.append(key)
                    else:
                        keys_seen.add(key)

            if url_col_idx >= 0 and url_col_idx < len(row):
                url = str(row[url_col_idx] or "").strip()
                if not url or url == "None":
                    empty_url_rows.append(row_idx)

        # Row count check
        if valid_row_count < expected_min_rows:
            checks.append(
                VerificationCheck(
                    name="row_count",
                    passed=False,
                    message=f"Found {valid_row_count} rows (expected >= {expected_min_rows}).",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="row_count",
                    passed=True,
                    message=f"Row count verified ({valid_row_count} rows).",
                )
            )

        # Unique keys check
        if duplicate_keys:
            checks.append(
                VerificationCheck(
                    name="unique_keys",
                    passed=False,
                    message=f"Found duplicate record_keys: {duplicate_keys[:5]}",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="unique_keys",
                    passed=True,
                    message="All record_keys in workbook are strictly unique.",
                )
            )

        # Non-empty source URLs check
        if empty_url_rows:
            checks.append(
                VerificationCheck(
                    name="source_urls_present",
                    passed=False,
                    message=f"Found {len(empty_url_rows)} rows with empty source_urls: rows {empty_url_rows[:5]}",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="source_urls_present",
                    passed=True,
                    message="All rows contain verified non-empty source_url.",
                )
            )

        return checks, artifact_hashes

    def verify_draft(
        self,
        draft_path: Union[str, Path],
        expected_recipient: Optional[str] = None,
        expected_subject: Optional[str] = None,
    ) -> tuple[List[VerificationCheck], Dict[str, str]]:
        """Verify local email draft artifact."""
        checks: List[VerificationCheck] = []
        artifact_hashes: Dict[str, str] = {}

        p = Path(draft_path).resolve()
        if not p.exists():
            checks.append(
                VerificationCheck(
                    name="draft_exists",
                    passed=False,
                    message=f"Draft file not found at '{p}'.",
                )
            )
            return checks, artifact_hashes

        checks.append(
            VerificationCheck(
                name="draft_exists",
                passed=True,
                message=f"Draft file exists at '{p}'.",
            )
        )

        # Extension check
        if p.suffix.lower() not in (".eml", ".txt"):
            checks.append(
                VerificationCheck(
                    name="draft_extension",
                    passed=False,
                    message=f"Invalid draft extension '{p.suffix}'. Must be .eml or .txt.",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="draft_extension",
                    passed=True,
                    message=f"Valid draft extension '{p.suffix}'.",
                )
            )

        sha256 = compute_file_sha256(p)
        artifact_hashes[p.name] = sha256

        # Read and verify content
        raw_bytes = p.read_bytes()
        try:
            msg = email.message_from_bytes(raw_bytes)
            # Verify state is strictly DRAFTED, not SENT
            draft_state = msg.get("X-Draft-State") or ""
            is_drafted = "DRAFTED" in draft_state or "DRAFTED" in p.read_text(encoding="utf-8", errors="ignore")
            is_sent = "SENT" in draft_state

            if is_drafted and not is_sent:
                checks.append(
                    VerificationCheck(
                        name="draft_state",
                        passed=True,
                        message="Draft state verified as DRAFTED (not SENT).",
                    )
                )
            else:
                checks.append(
                    VerificationCheck(
                        name="draft_state",
                        passed=False,
                        message=f"Draft state check failed: state='{draft_state}', is_sent={is_sent}.",
                    )
                )

            # Check recipient match if expected
            if expected_recipient:
                recip = msg.get("To") or ""
                if expected_recipient.lower() in recip.lower():
                    checks.append(
                        VerificationCheck(
                            name="recipient_match",
                            passed=True,
                            message=f"Recipient matches expected '{expected_recipient}'.",
                        )
                    )
                else:
                    checks.append(
                        VerificationCheck(
                            name="recipient_match",
                            passed=False,
                            message=f"Recipient mismatch: found '{recip}' vs expected '{expected_recipient}'.",
                        )
                    )

            # Check subject match if expected
            if expected_subject:
                subj = msg.get("Subject") or ""
                if expected_subject.lower() in subj.lower():
                    checks.append(
                        VerificationCheck(
                            name="subject_match",
                            passed=True,
                            message="Subject matches expected content.",
                        )
                    )
                else:
                    checks.append(
                        VerificationCheck(
                            name="subject_match",
                            passed=False,
                            message=f"Subject mismatch: found '{subj}' vs expected '{expected_subject}'.",
                        )
                    )

        except Exception as exc:
            checks.append(
                VerificationCheck(
                    name="draft_parseable",
                    passed=False,
                    message=f"Could not parse draft email message: {exc}",
                )
            )

        return checks, artifact_hashes

    def link_claims_to_evidence(
        self,
        run_id: str,
        claims: List[str],
    ) -> List[ClaimEvidenceLink]:
        """Link proposed claims to SQLite evidence records."""
        links: List[ClaimEvidenceLink] = []
        if not self.db_path.exists():
            for c in claims:
                links.append(ClaimEvidenceLink(claim=c, verified=False, label="not verified"))
            return links

        conn = open_db_connection(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.evidence_id, e.content, e.source_url, e.hash
                FROM evidence e
                JOIN steps s ON e.step_id = s.step_id
                WHERE s.run_id = ?;
                """,
                (run_id,),
            )
            evidence_rows = cur.fetchall()

            for claim_text in claims:
                matched = None
                for ev in evidence_rows:
                    content = ev["content"] or ""
                    # Check substring or keyword overlap
                    if any(word.lower() in content.lower() for word in claim_text.split() if len(word) > 4):
                        matched = ev
                        break

                if matched:
                    links.append(
                        ClaimEvidenceLink(
                            claim=claim_text,
                            verified=True,
                            evidence_id=matched["evidence_id"],
                            source_url=matched["source_url"],
                            content_hash=matched["hash"],
                            label="verified",
                        )
                    )
                else:
                    links.append(
                        ClaimEvidenceLink(
                            claim=claim_text,
                            verified=False,
                            label="not verified",
                        )
                    )

            return links
        finally:
            conn.close()

    def generate_verification_report(
        self,
        run_id: str,
        step_states: List[str],
        spreadsheet_path: Optional[Union[str, Path]] = None,
        draft_path: Optional[Union[str, Path]] = None,
        claims: Optional[List[str]] = None,
        summary_text: str = "",
    ) -> VerificationReport:
        """Generate a complete machine-readable verification report and derive final run status.

        CRITICAL INVARIANT: The summary text can NEVER override a failed check!
        If any verification check fails, the final status CANNOT be VERIFIED.
        """
        all_checks: List[VerificationCheck] = []
        all_hashes: Dict[str, str] = {}

        if spreadsheet_path:
            s_checks, s_hashes = self.verify_spreadsheet(spreadsheet_path)
            all_checks.extend(s_checks)
            all_hashes.update(s_hashes)

        if draft_path:
            d_checks, d_hashes = self.verify_draft(draft_path)
            all_checks.extend(d_checks)
            all_hashes.update(d_hashes)

        # Compute claim-evidence links
        claim_links: List[ClaimEvidenceLink] = []
        if claims:
            claim_links = self.link_claims_to_evidence(run_id, claims)

        # Derive final status
        all_checks_passed = len(all_checks) > 0 and all(c.passed for c in all_checks)
        has_failed_check = any(not c.passed for c in all_checks)

        if "FAILED" in step_states or has_failed_check:
            final_status = RunFinalStatus.FAILED.value
        elif "CANCELLED" in step_states:
            final_status = RunFinalStatus.CANCELLED.value
        elif "UNRESOLVED" in step_states:
            final_status = RunFinalStatus.UNRESOLVED.value
        elif all(s == "VERIFIED" for s in step_states) and all_checks_passed:
            final_status = RunFinalStatus.VERIFIED.value
        elif any(s == "VERIFIED" for s in step_states) and all_checks_passed:
            final_status = RunFinalStatus.PARTIAL.value
        else:
            final_status = RunFinalStatus.UNRESOLVED.value

        # Enforce invariant: summary text can never override a failed check
        if has_failed_check and final_status == RunFinalStatus.VERIFIED.value:
            final_status = RunFinalStatus.FAILED.value

        summary = (
            f"Verification Complete: {final_status}. "
            f"Checks: {sum(1 for c in all_checks if c.passed)}/{len(all_checks)} passed. "
            f"{summary_text}"
        )

        return VerificationReport(
            run_id=run_id,
            passed=all_checks_passed,
            final_status=final_status,
            checks=all_checks,
            claims=claim_links,
            artifact_hashes=all_hashes,
            summary=summary,
        )
