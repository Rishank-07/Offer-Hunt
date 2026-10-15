"""Web research and internship search tool integration for OFFERHUNT (Milestone M3).

Connects to Tavily Search with strict query sanitization, result bounding,
secret redaction, and standardized output formatting.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from mcp_tools.tool_errors import (
    MCPToolError,
    ToolErrorCode,
    sanitize_search_query,
    scrub_secrets,
    validate_safe_url,
)


def extract_company_from_title(title: str) -> str:
    """Best-effort heuristic to extract company from search title if company is embedded (e.g. 'Software Intern - Google')."""
    if " - " in title:
        parts = title.split(" - ")
        return parts[-1].strip()
    if " | " in title:
        parts = title.split(" | ")
        return parts[-1].strip()
    if " at " in title.lower():
        idx = title.lower().rfind(" at ")
        return title[idx + 4 :].strip()
    return "Unknown Company"


def search_internships_tavily(
    query: str,
    max_results: int = 5,
    location: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Search for internships using Tavily Search API.

    - Treats `query` as UNTRUSTED user input and sanitizes it.
    - Limits `max_results` between 1 and 10.
    - Formats output with standardized fields:
      title, company, url, snippet, retrieval_time, source_type, source_confidence.
    - Redacts API keys from any errors.
    """
    clean_query = sanitize_search_query(query, max_length=200)

    # Bound max_results strictly between 1 and 10
    bounded_max_results = max(1, min(int(max_results), 10))

    if location and isinstance(location, str):
        clean_loc = sanitize_search_query(location, max_length=100)
        full_query = f"{clean_query} {clean_loc}"
    else:
        full_query = clean_query

    tavily_key = api_key or os.getenv("TAVILY_API_KEY")

    # If Tavily key is missing or set to placeholder, provide safe structured fallback results for offline/testing
    if not tavily_key or tavily_key == "your_tavily_api_key_here":
        retrieval_time = datetime.now(timezone.utc).isoformat()
        return [
            {
                "title": f"Software Engineer Intern ({clean_query[:30]})",
                "company": "Tech Corp",
                "url": "https://careers.techcorp.org/internships/software",
                "snippet": f"Exciting internship position matching '{clean_query}'. Competitive stipend and mentorship.",
                "retrieval_time": retrieval_time,
                "source_type": "web_search_mock",
                "source_confidence": 0.85,
            }
        ]

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=tavily_key)
        response = client.search(
            query=full_query,
            max_results=bounded_max_results,
        )
        raw_results = response.get("results", [])

        now_utc = datetime.now(timezone.utc).isoformat()
        formatted: List[dict[str, Any]] = []

        for item in raw_results:
            raw_url = item.get("url") or ""
            # Validate URL is safe (not localhost/private)
            try:
                safe_url = validate_safe_url(raw_url)
            except MCPToolError:
                continue

            raw_title = item.get("title") or "Untitled Internship"
            company = extract_company_from_title(raw_title)

            # Score confidence based on Tavily score if present
            score = float(item.get("score") or 0.80)
            confidence = max(0.1, min(score, 1.0))

            formatted.append({
                "title": raw_title.strip(),
                "company": company,
                "url": safe_url,
                "snippet": (item.get("content") or item.get("snippet") or "").strip(),
                "retrieval_time": now_utc,
                "source_type": "web_search",
                "source_confidence": round(confidence, 2),
            })

        return formatted

    except MCPToolError:
        raise
    except Exception as exc:
        clean_error = scrub_secrets(str(exc))
        raise MCPToolError(
            ToolErrorCode.SEARCH_FAILED,
            f"Tavily search execution failed: {clean_error}",
            retryable=True,
        ) from None
