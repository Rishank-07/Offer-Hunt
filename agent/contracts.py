"""Contracts and data models for OFFERHUNT (Milestone M1).

Defines Pydantic models for internship records, error contracts, and
the canonical record_key generator conforming to specification section 15.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator


# Common tracking parameters to strip during URL canonicalization
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "ref_src",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "trk",
    "trackingId",
    "si",
}


def normalize_text(text: Optional[str]) -> str:
    """Normalize text: Unicode NFKC, lowercase, collapse whitespace."""
    if not text:
        return ""
    # 1. Unicode normalize
    normalized = unicodedata.normalize("NFKC", text)
    # 2. Lowercase
    normalized = normalized.lower()
    # 3. Collapse whitespace and strip
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def canonicalize_url(url: Optional[str]) -> str:
    """Canonicalize URL by lowercasing scheme/netloc, removing tracking parameters,

    normalizing path and stripping fragments.
    """
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()

        # Filter out tracking query parameters
        query_pairs = parse_qsl(parts.query, keep_blank_values=False)
        cleaned_pairs = [
            (k, v) for k, v in query_pairs if k.lower() not in TRACKING_PARAMS
        ]
        # Sort query params for deterministic canonical representation
        cleaned_pairs.sort(key=lambda x: x[0])
        clean_query = urlencode(cleaned_pairs)

        # Normalize path: remove trailing slash if not root
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        # Discard fragments (#...)
        canonical = urlunsplit((scheme, netloc, path, clean_query, ""))
        return canonical
    except Exception:
        return normalize_text(url)


def compute_record_key(
    title: str,
    company: str,
    location: str,
    source_url: Optional[str] = None,
) -> tuple[str, bool]:
    """Compute stable, collision-resistant record_key (SHA-256) per section 15.

    The key is formed by normalizing:
    - title
    - company
    - location
    - canonical URL (with tracking params stripped)

    Returns:
        (record_key, is_canonical):
        - record_key: 64-character SHA-256 hex digest.
        - is_canonical: True if canonical URL was present. False if fallback
          lower-confidence key was generated because source_url was missing/empty.

    Lower-confidence fallback:
        When source_url is missing, a documented lower-confidence key is generated
        from f"{norm_company}|{norm_title}|{norm_location}|fallback:no_url".
        This preserves deduplication by company, title, and location but flags
        the lower confidence provenance.
    """
    norm_title = normalize_text(title)
    norm_company = normalize_text(company)
    norm_location = normalize_text(location)

    canonical_url = canonicalize_url(source_url)
    is_canonical = bool(canonical_url)

    if is_canonical:
        composite = f"{norm_company}|{norm_title}|{norm_location}|{canonical_url}"
    else:
        # Documented lower-confidence fallback when URL is missing
        composite = f"{norm_company}|{norm_title}|{norm_location}|fallback:no_url"

    digest = hashlib.sha256(composite.encode("utf-8")).hexdigest()
    return digest, is_canonical


class InternshipRecord(BaseModel):
    """Pydantic model representing a discovered and structured internship offer."""

    record_key: str = Field(
        ...,
        description="Deterministic SHA-256 stable identifier.",
    )
    title: str = Field(..., description="Job or internship position title.")
    company: str = Field(..., description="Hiring organization or company name.")
    location: str = Field(
        ...,
        description="Location (e.g. Remote, City, Country). Differentiates records.",
    )
    eligibility: Optional[str] = Field(
        default=None, description="Eligibility requirements or qualifications."
    )
    stipend: Optional[str] = Field(
        default=None, description="Stipend, salary, or compensation info."
    )
    deadline: Optional[str] = Field(
        default=None, description="Application deadline if present."
    )
    source_url: Optional[str] = Field(
        default=None, description="Canonical source listing or application URL."
    )
    source_title: Optional[str] = Field(
        default=None, description="Title of the source webpage or listing."
    )
    snippet: Optional[str] = Field(
        default=None, description="Raw context snippet or offer description."
    )
    retrieved_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp of record retrieval.",
    )
    source_type: str = Field(
        default="web_search",
        description="Provenance source type: search, catalog, direct_url, etc.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Extraction confidence score between 0.0 and 1.0.",
    )
    status: str = Field(
        default="RAW",
        description="Lifecycle/provenance status: RAW, VERIFIED, DRAFTED, etc.",
    )
    provenance: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional structured provenance and audit metadata.",
    )

    @classmethod
    def create(
        cls,
        title: str,
        company: str,
        location: str,
        source_url: Optional[str] = None,
        eligibility: Optional[str] = None,
        stipend: Optional[str] = None,
        deadline: Optional[str] = None,
        source_title: Optional[str] = None,
        snippet: Optional[str] = None,
        source_type: str = "web_search",
        confidence: float = 1.0,
        status: str = "RAW",
        provenance: Optional[dict[str, Any]] = None,
    ) -> InternshipRecord:
        """Helper constructor that computes normalized stable record_key automatically."""
        key, is_canonical = compute_record_key(title, company, location, source_url)
        # If URL is missing, lower confidence slightly if not explicitly adjusted
        adjusted_confidence = confidence if is_canonical else min(confidence, 0.75)
        prov = provenance or {}
        if not is_canonical:
            prov["fallback_key"] = True
            prov["fallback_reason"] = "Missing source_url at extraction"

        return cls(
            record_key=key,
            title=title.strip(),
            company=company.strip(),
            location=location.strip(),
            eligibility=eligibility.strip() if eligibility else None,
            stipend=stipend.strip() if stipend else None,
            deadline=deadline.strip() if deadline else None,
            source_url=canonicalize_url(source_url) if source_url else None,
            source_title=source_title.strip() if source_title else None,
            snippet=snippet.strip() if snippet else None,
            source_type=source_type,
            confidence=adjusted_confidence,
            status=status,
            provenance=prov,
        )


class ErrorModel(BaseModel):
    """Standardized error contract for tool operations and agent step failures."""

    error_code: str = Field(
        ...,
        description="Standardized error machine code (e.g. SPREADSHEET_CORRUPT, PATH_TRAVERSAL).",
    )
    retryable: bool = Field(
        ...,
        description="Whether this operation is safe and meaningful to retry.",
    )
    message_safe: str = Field(
        ...,
        description="Sanitized user-facing message with no internal secrets or traces.",
    )
    operation_id: str = Field(
        ...,
        description="Identifier of the operation or step that produced the error.",
    )
    outcome_certainty: str = Field(
        ...,
        description="Certainty of outcome: CONFIRMED_FAILED, UNKNOWN, or PARTIAL.",
    )

    @field_validator("outcome_certainty")
    @classmethod
    def validate_outcome_certainty(cls, v: str) -> str:
        allowed = {"CONFIRMED_FAILED", "UNKNOWN", "PARTIAL"}
        if v not in allowed:
            raise ValueError(f"outcome_certainty must be one of {allowed}")
        return v
