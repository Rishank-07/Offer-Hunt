"""Structured errors, input sanitizers, and security validations for OFFERHUNT MCP tools (Milestone M3).

Enforces security boundaries:
- URL safety (http/https only, strictly rejects localhost and private/internal IP ranges).
- Input length and array size bounds (prevents DoS and memory exhaustion).
- Secret scrubbing (ensures API keys are never leaked in tool outputs or exceptions).
"""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse


class ToolErrorCode(str, Enum):
    """Standardized tool error codes."""
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    OVERSIZED_INPUT = "OVERSIZED_INPUT"
    UNSAFE_URL = "UNSAFE_URL"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    DISALLOWED_PATH = "DISALLOWED_PATH"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    SEARCH_FAILED = "SEARCH_FAILED"
    SPREADSHEET_ERROR = "SPREADSHEET_ERROR"
    DRAFT_ERROR = "DRAFT_ERROR"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"


class MCPToolError(ValueError):
    """Structured, safe MCP tool exception."""

    def __init__(
        self,
        error_code: ToolErrorCode,
        message: str,
        details: Optional[dict[str, Any]] = None,
        retryable: bool = False,
    ):
        clean_msg = scrub_secrets(message)
        super().__init__(clean_msg)
        self.error_code = error_code
        self.message = clean_msg
        self.details = details or {}
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def scrub_secrets(text: str) -> str:
    """Scrub known API keys and authorization tokens from text and error messages."""
    if not isinstance(text, str):
        text = str(text)
    scrubbed = re.sub(
        r"(?i)(api[_-]?key\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{8,}(['\"]?)",
        r"\1[REDACTED]\2",
        text,
    )
    scrubbed = re.sub(
        r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{16,}",
        r"\1[REDACTED]",
        scrubbed,
    )
    scrubbed = re.sub(r"(?i)tvly-[a-zA-Z0-9_\-]{16,}", "[REDACTED_TAVILY_KEY]", scrubbed)
    scrubbed = re.sub(r"(?i)AIza[0-9A-Za-z\-_]{35}", "[REDACTED_GEMINI_KEY]", scrubbed)
    return scrubbed


# Private / reserved IPv4 and IPv6 networks
PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("10.0.0.0/8"),       # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),    # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local / Cloud metadata (e.g. AWS 169.254.169.254)
    ipaddress.ip_network("0.0.0.0/8"),        # Broadcast
    ipaddress.ip_network("::1/128"),          # IPv6 Loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 Unique Local
    ipaddress.ip_network("fe80::/10"),        # IPv6 Link-Local
]

DISALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",
    "169.254.169.254",
}


def validate_safe_url(url: str) -> str:
    """Validate that a URL uses http/https and does not target private or local networks.

    Raises:
        MCPToolError if the URL is invalid or targets private infrastructure.
    """
    if not url or not isinstance(url, str):
        raise MCPToolError(
            ToolErrorCode.UNSAFE_URL,
            "URL must be a non-empty string.",
        )

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise MCPToolError(
            ToolErrorCode.UNSAFE_URL,
            f"Unsupported URL scheme '{scheme}'. Only http and https are allowed.",
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise MCPToolError(
            ToolErrorCode.UNSAFE_URL,
            f"URL '{url}' lacks a valid hostname.",
        )

    if hostname in DISALLOWED_HOSTS or hostname.endswith(".local") or hostname.endswith(".internal"):
        raise MCPToolError(
            ToolErrorCode.UNSAFE_URL,
            f"Access to private/local host '{hostname}' is strictly prohibited.",
        )

    # Check if host is an IP address
    is_ip = False
    try:
        ip = ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        # Not a raw IP literal; hostname is a regular domain name
        pass

    if is_ip:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or not ip.is_global:
            raise MCPToolError(
                ToolErrorCode.UNSAFE_URL,
                f"Access to private/internal IP address '{hostname}' is prohibited.",
            )

    return url.strip()


def validate_array_bounds(
    items: List[Any],
    max_len: int = 50,
    field_name: str = "items",
) -> List[Any]:
    """Validate that an input array does not exceed the allowed maximum size."""
    if not isinstance(items, list):
        raise MCPToolError(
            ToolErrorCode.INVALID_ARGUMENT,
            f"Field '{field_name}' must be a list.",
        )
    if len(items) > max_len:
        raise MCPToolError(
            ToolErrorCode.OVERSIZED_INPUT,
            f"Field '{field_name}' exceeds maximum permitted elements ({len(items)} > {max_len}).",
        )
    return items


def sanitize_search_query(query: str, max_length: int = 200) -> str:
    """Sanitize search query from untrusted inputs: strips control chars and bounds length."""
    if not query or not isinstance(query, str):
        raise MCPToolError(
            ToolErrorCode.INVALID_ARGUMENT,
            "Search query must be a non-empty string.",
        )
    cleaned = "".join(ch for ch in query if ch.isprintable()).strip()
    if not cleaned:
        raise MCPToolError(
            ToolErrorCode.INVALID_ARGUMENT,
            "Search query cannot be empty after sanitization.",
        )
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned
