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

import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio import activity


MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
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
    Non-retryable errors (e.g. 400 Bad Request) should raise
    `temporalio.exceptions.ApplicationError(non_retryable=True)`.

    TODO: Replace the stub body with a real HTTP call once the MCP server
          (sre_swarm/mcp/server.py) is implemented.
    """
    activity.logger.info("Calling MCP tool: %s", request.tool_name)

    # --- STUB: simulate calling the MCP server ----------------------------
    # Replace this block with the real implementation below once the server
    # is running. The commented-out code shows the correct request shape.
    #
    # async with httpx.AsyncClient() as client:
    #     response = await client.post(
    #         f"{MCP_SERVER_URL}/mcp",
    #         headers={
    #             "Mcp-Protocol-Version": MCP_PROTOCOL,
    #             "Mcp-Method":           "tools/call",
    #             "Authorization":        f"Bearer {MCP_API_KEY}",
    #             "Content-Type":         "application/json",
    #         },
    #         json={
    #             "tool": request.tool_name,
    #             "arguments": request.arguments,
    #         },
    #         timeout=25.0,
    #     )
    #     response.raise_for_status()
    #     body = response.json()
    #     return MCPToolResult(tool_name=request.tool_name, output=body)
    # --- END STUB ----------------------------------------------------------

    stub_outputs: dict[str, dict] = {
        "get_telemetry_context": {
            "spans": [
                {
                    "trace_id": request.arguments.get("trace_ids", ["trace-001"])[0],
                    "service":  request.arguments.get("service", "unknown"),
                    "error":    True,
                    "latency_ms": 4500,
                    "status_code": 500,
                }
            ],
            "cpu_overhead_pct": 2.4,  # simulated eBPF overhead
        },
        "analyze_root_cause": {
            "root_cause": "Payment service timeout caused order saga to partially commit",
            "confidence": 0.91,
            "compensations": [
                {"endpoint": "/cancelOrder",   "method": "POST", "payload": {"order_id": "ord-789"}},
                {"endpoint": "/refundPayment", "method": "POST", "payload": {"payment_id": "pay-456"}},
            ],
        },
    }

    output = stub_outputs.get(request.tool_name, {"message": "tool not found"})
    return MCPToolResult(tool_name=request.tool_name, output=output)
