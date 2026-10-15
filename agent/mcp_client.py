"""MCP Client wrapper for OFFERHUNT (Milestone M3).

Connects over stdio transport using the official MCP Python SDK v2:
- mcp.client.stdio.stdio_client (with StdioServerParameters)
- mcp.client.session.ClientSession

Caches the discovered tool catalog strictly for the current server process session.
Enforces per-call timeouts and unpacks structured outputs.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import MCPServer

from mcp_tools.tool_errors import MCPToolError, ToolErrorCode, scrub_secrets


@dataclass
class ToolDefinition:
    """Catalog definition for an exposed tool."""
    name: str
    description: str
    input_schema: dict[str, Any]


class MCPClientWrapper:
    """Async client wrapper for connecting to an OFFERHUNT MCP server over stdio.

    Features:
    - Stdio transport via official MCP Python SDK v2.
    - Automatic initialization and catalog discovery.
    - In-memory catalog caching for the duration of the server session.
    - Timeout-protected tool invocations.
    - Support for in-memory MCPServer for high-performance direct testing.
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        server_instance: Optional[MCPServer] = None,
    ):
        self.command = command or sys.executable
        self.args = args or ["-m", "mcp_tools.server"]
        self.cwd = cwd or Path.cwd()

        client_env = dict(os.environ) if env is None else dict(env)
        repo_root = str(Path(__file__).resolve().parent.parent)
        if "PYTHONPATH" in client_env:
            client_env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{client_env['PYTHONPATH']}"
        else:
            client_env["PYTHONPATH"] = repo_root
        self.env = client_env

        self.server_instance = server_instance

        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._cached_catalog: Optional[List[ToolDefinition]] = None
        self._is_connected = False

    async def connect(self) -> None:
        """Establish connection with the MCP server and initialize session."""
        if self._is_connected:
            return

        self._exit_stack = AsyncExitStack()

        if self.server_instance is not None:
            # In-memory MCPServer connection via Client adapter
            from mcp.client.client import Client

            client_adapter = Client(self.server_instance)
            await self._exit_stack.enter_async_context(client_adapter)
            self._session = client_adapter.session
        else:
            # Stdio transport connection
            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                cwd=str(self.cwd),
                env=self.env,
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._session = session

        self._is_connected = True
        # Preload and cache catalog for this session
        await self.get_tool_catalog(force_refresh=True)

    async def close(self) -> None:
        """Close MCP connection and invalidate session catalog."""
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None

        self._session = None
        self._cached_catalog = None
        self._is_connected = False

    async def __aenter__(self) -> MCPClientWrapper:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def get_tool_catalog(self, force_refresh: bool = False) -> List[ToolDefinition]:
        """Return the tool catalog for the current server process session.

        Caches the catalog in-memory; only refetches if force_refresh is True.
        """
        if not self._is_connected or not self._session:
            raise RuntimeError("MCPClientWrapper is not connected. Call connect() first.")

        if self._cached_catalog is not None and not force_refresh:
            return self._cached_catalog

        result = await self._session.list_tools()
        catalog: List[ToolDefinition] = []
        for t in result.tools:
            catalog.append(
                ToolDefinition(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.input_schema or {},
                )
            )

        self._cached_catalog = catalog
        return self._cached_catalog

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        """Call a named tool with bounded execution timeout.

        Returns structured content dict or parsed content.
        Raises MCPToolError on tool execution failure or timeout.
        """
        if not self._is_connected or not self._session:
            raise RuntimeError("MCPClientWrapper is not connected. Call connect() first.")

        # Verify tool is in the live catalog
        catalog = await self.get_tool_catalog()
        known_tools = {t.name for t in catalog}
        if name not in known_tools:
            raise MCPToolError(
                ToolErrorCode.INVALID_ARGUMENT,
                f"Unknown tool '{name}'. Tool does not exist in live MCP catalog.",
            )

        args = arguments or {}

        try:
            call_coro = self._session.call_tool(name, args)
            res = await asyncio.wait_for(call_coro, timeout=timeout)

            if getattr(res, "is_error", False):
                err_text = ""
                if hasattr(res, "content") and res.content:
                    err_text = " ".join(getattr(c, "text", str(c)) for c in res.content)
                raise MCPToolError(
                    ToolErrorCode.TOOL_EXECUTION_ERROR,
                    f"Tool '{name}' execution reported error: {scrub_secrets(err_text)}",
                )

            # Prefer structured_content, fallback to text content
            if hasattr(res, "structured_content") and res.structured_content is not None:
                sc = res.structured_content
                if isinstance(sc, dict) and sc.get("error") is True:
                    raise MCPToolError(
                        ToolErrorCode(sc.get("error_code", ToolErrorCode.TOOL_EXECUTION_ERROR.value)),
                        sc.get("message", "Tool execution failed"),
                    )
                if isinstance(sc, dict) and "result" in sc and len(sc) == 1:
                    return sc["result"]
                return sc

            if hasattr(res, "content") and res.content:
                text_parts = [getattr(c, "text", str(c)) for c in res.content]
                return "\n".join(text_parts)

            return res

        except asyncio.TimeoutError:
            raise MCPToolError(
                ToolErrorCode.TOOL_EXECUTION_ERROR,
                f"Tool '{name}' invocation timed out after {timeout} seconds.",
                retryable=True,
            ) from None
        except MCPToolError:
            raise
        except Exception as exc:
            clean_err = scrub_secrets(str(exc))
            raise MCPToolError(
                ToolErrorCode.TOOL_EXECUTION_ERROR,
                f"Tool '{name}' invocation failed: {clean_err}",
            ) from None
