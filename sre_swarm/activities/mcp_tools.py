"""
MCP Tool Activities
===================
Non-deterministic Activities that call the stateless MCP server.

MCP spec targeted: 2026-07-28 (stateless)
  - No session handshake. Every request is a self-describing HTTP POST.
  - Required headers:
      Mcp-Protocol-Version: 2026-07-28
      Mcp-Method: tools/call
  - The server must be stateless; auth context is passed per-request.

These are Activities (not Workflows) because:
  - They perform network I/O (non-deterministic).
  - Temporal retries them safely without replaying the whole workflow.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio import activity


MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8081")
MCP_API_KEY    = os.getenv("MCP_API_KEY", "dev-key")
MCP_PROTOCOL   = "2026-07-28"


# ---------------------------------------------------------------------------
# Data models shared with the Workflow
# ---------------------------------------------------------------------------

@dataclass
class MCPToolRequest:
    """Arguments for a single MCP tool invocation."""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPToolResult:
    """Structured result returned from the MCP server."""
    tool_name: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Activity definitions
# ---------------------------------------------------------------------------

@activity.defn
async def call_mcp_tool(request: MCPToolRequest) -> MCPToolResult:
    """
    Call a tool on the stateless MCP server.

    Implements the 2026-07-28 stateless spec:
      POST {MCP_SERVER_URL}/mcp
      Mcp-Protocol-Version: 2026-07-28
      Mcp-Method: tools/call

    The Activity is retried automatically by Temporal on transient failures.
    Non-retryable errors (e.g. 400 Bad Request — unknown tool or bad arguments)
    raise ApplicationError(non_retryable=True) so Temporal does not waste
    retry budget on a logically invalid request.

    For analyze_root_cause the MCP server streams the LLM response token-by-token.
    We consume the stream with periodic heartbeats so Temporal does not cancel the
    activity for inactivity while tokens are still arriving.
    """
    import asyncio  # noqa: PLC0415
    from temporalio.exceptions import ApplicationError  # noqa: PLC0415

    activity.logger.info("Calling MCP tool: %s", request.tool_name)

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"{MCP_SERVER_URL}/mcp",
            headers={
                "Mcp-Protocol-Version": MCP_PROTOCOL,
                "Mcp-Method":           "tools/call",
                "Authorization":        f"Bearer {MCP_API_KEY}",
                "Content-Type":         "application/json",
            },
            json={
                "tool": request.tool_name,
                "arguments": request.arguments,
            },
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=5.0),
        ) as response:
            if response.status_code in (400, 401, 404):
                # Client-side error — retrying will not help; read body for detail
                text = await response.aread()
                raise ApplicationError(
                    f"MCP server rejected the request [{response.status_code}]: {text.decode()}",
                    non_retryable=True,
                )

            response.raise_for_status()

            # Consume the response body, heartbeating every ~5 s so Temporal
            # knows this activity is still making progress (important for
            # streaming LLM calls that may take tens of seconds).
            chunks: list[bytes] = []
            last_heartbeat = asyncio.get_event_loop().time()
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                now = asyncio.get_event_loop().time()
                if now - last_heartbeat >= 5.0:
                    activity.heartbeat()
                    last_heartbeat = now

        body = json.loads(b"".join(chunks))
        return MCPToolResult(tool_name=request.tool_name, output=body)
