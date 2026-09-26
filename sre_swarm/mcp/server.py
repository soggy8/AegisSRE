"""
MCP Server — Stateless HTTP endpoint
=====================================
Implements the MCP 2026-07-28 stateless spec for the AegisSRE system.

Spec contract:
  - Single POST route at /mcp
  - No session state — every request is fully self-describing
  - Required headers on every request:
      Mcp-Protocol-Version: 2026-07-28
      Mcp-Method:           tools/call
      Authorization:        Bearer <MCP_API_KEY>
  - Request body: { "tool": str, "arguments": dict }
  - Response body: the tool-specific output dict

Tools implemented:
  get_telemetry_context  — fetches eBPF/OTLP spans for the given trace IDs
  analyze_root_cause     — runs LLM inference and returns a compensation plan

Run with:
    python -m sre_swarm.mcp.server
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel
import uvicorn

load_dotenv()

MCP_API_KEY        = os.getenv("MCP_API_KEY", "dev-key")
MCP_PROTOCOL       = "2026-07-28"
MCP_SERVER_PORT    = int(os.getenv("MCP_SERVER_PORT", "8081"))

# Optional LLM credentials — at least one must be set for analyze_root_cause.
# If neither is present we fall back to a deterministic heuristic so the system
# stays runnable without API keys in development.
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
WATSONX_API_KEY    = os.getenv("WATSONX_API_KEY", "")
WATSONX_PROJECT_ID = os.getenv("WATSONX_PROJECT_ID", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AegisSRE MCP Server", version="0.1.0")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MCPRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _verify_auth(authorization: str | None) -> None:
    """
    Validate the Bearer token against MCP_API_KEY.
    Raises HTTP 401 if the header is missing or the token does not match.
    """
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is required",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != MCP_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Bearer token",
        )


def _verify_protocol(mcp_protocol_version: str | None) -> None:
    """
    Validate that the client announces the supported protocol version.
    Raises HTTP 400 for unknown versions.
    """
    if mcp_protocol_version != MCP_PROTOCOL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported Mcp-Protocol-Version: {mcp_protocol_version!r}. "
                   f"Expected: {MCP_PROTOCOL!r}",
        )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_get_telemetry_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Fetch OTLP-shaped spans for the requested trace IDs and service.

    Expected arguments:
        trace_ids: list[str]  — trace IDs to fetch spans for
        service:   str        — primary service under investigation

    Returns:
        { "spans": [...], "cpu_overhead_pct": float }

    Delegates entirely to the telemetry pipeline; this handler is stateless.
    """
    from sre_swarm.telemetry.pipeline import get_spans, CPU_OVERHEAD_PCT  # noqa: PLC0415

    trace_ids: list[str] = arguments.get("trace_ids", [])
    service: str = arguments.get("service", "")

    if not trace_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="arguments.trace_ids must be a non-empty list",
        )
    if not service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="arguments.service is required",
        )

    spans = get_spans(trace_ids=trace_ids, service=service)
    logger.info(
        "get_telemetry_context: service=%s traces=%s returned %d spans",
        service, trace_ids, len(spans),
    )
    return {"spans": spans, "cpu_overhead_pct": CPU_OVERHEAD_PCT}


def _handle_analyze_root_cause(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Run root-cause analysis and produce a Saga compensation plan.

    Expected arguments:
        alert_summary:      str   — one-line human alert text
        telemetry_context:  dict  — full output from get_telemetry_context
                                    (contains "spans" and "cpu_overhead_pct")

    Returns:
        {
            "root_cause":    str,
            "confidence":    float,   # 0.0–1.0
            "compensations": [
                { "endpoint": str, "method": str, "payload": dict },
                ...
            ]
        }

    Implementation strategy:
      1. If OPENAI_API_KEY is set, use the OpenAI chat completions API.
      2. If WATSONX_API_KEY + WATSONX_PROJECT_ID are set, use watsonx.ai.
      3. Otherwise, fall back to a deterministic heuristic that inspects the
         spans directly — suitable for local dev without API credentials.
    """
    alert_summary: str = arguments.get("alert_summary", "")
    telemetry_context: dict[str, Any] = arguments.get("telemetry_context", {})
    spans: list[dict] = telemetry_context.get("spans", [])

    if not alert_summary:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="arguments.alert_summary is required",
        )

    if OPENAI_API_KEY:
        return _analyze_with_openai(alert_summary, spans)

    if WATSONX_API_KEY and WATSONX_PROJECT_ID:
        return _analyze_with_watsonx(alert_summary, spans)

    # Fallback: deterministic heuristic (no LLM required)
    return _analyze_heuristic(alert_summary, spans)


# ---------------------------------------------------------------------------
# LLM back-ends
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer.
Given an alert summary and a list of OpenTelemetry spans, identify the root cause
of the incident and produce a JSON compensation plan.

Respond ONLY with valid JSON in this exact shape — no markdown, no explanation:
{
  "root_cause":    "<one sentence describing the root cause>",
  "confidence":    <float between 0.0 and 1.0>,
  "compensations": [
    { "endpoint": "<path such as /cancelOrder>", "method": "POST", "payload": { ... } },
    ...
  ]
}

Available compensation endpoints on the mock microservices at http://localhost:9090:
  POST /cancelOrder   { "order_id": "<id>" }
  POST /refundPayment { "payment_id": "<id>" }

If no compensating transactions are needed, return an empty compensations list.
"""


def _build_user_message(alert_summary: str, spans: list[dict]) -> str:
    return (
        f"Alert: {alert_summary}\n\n"
        f"Spans:\n{json.dumps(spans, indent=2)}"
    )


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """
    Parse a JSON blob from an LLM response, stripping any markdown fences.
    Raises HTTPException 502 if the JSON is malformed.
    """
    # Strip common markdown wrappers LLMs tend to add
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned non-JSON output: %s", raw[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM returned malformed JSON: {exc}",
        )


def _analyze_with_openai(alert_summary: str, spans: list[dict]) -> dict[str, Any]:
    """Call the OpenAI chat completions API for root-cause analysis."""
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="openai package not installed. Run: pip install openai",
        ) from exc

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    logger.info("analyze_root_cause: using OpenAI backend")
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(alert_summary, spans)},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    raw = response.choices[0].message.content or ""
    return _parse_llm_json(raw)


def _analyze_with_watsonx(alert_summary: str, spans: list[dict]) -> dict[str, Any]:
    """Call watsonx.ai for root-cause analysis."""
    try:
        from ibm_watsonx_ai import APIClient, Credentials  # noqa: PLC0415
        from ibm_watsonx_ai.foundation_models import ModelInference  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ibm-watsonx-ai package not installed. Run: pip install ibm-watsonx-ai",
        ) from exc

    logger.info("analyze_root_cause: using watsonx.ai backend")
    credentials = Credentials(api_key=WATSONX_API_KEY, url="https://us-south.ml.cloud.ibm.com")
    client = APIClient(credentials)
    model = ModelInference(
        model_id="ibm/granite-3-3-8b-instruct",
        api_client=client,
        project_id=WATSONX_PROJECT_ID,
    )
    prompt = f"{_SYSTEM_PROMPT}\n\n{_build_user_message(alert_summary, spans)}"
    result = model.generate_text(prompt=prompt, params={"max_new_tokens": 512, "temperature": 0.0})
    return _parse_llm_json(result)


def _analyze_heuristic(alert_summary: str, spans: list[dict]) -> dict[str, Any]:
    """
    Deterministic fallback when no LLM credentials are configured.

    Inspects the spans for error signals and picks the first erroring span's
    service as the root cause.  No compensations are emitted because the
    heuristic has no way to know real order/payment IDs — an LLM must be
    configured (OPENAI_API_KEY or WATSONX_API_KEY+WATSONX_PROJECT_ID) to
    produce a meaningful compensation plan.
    """
    logger.warning(
        "analyze_root_cause: no LLM credentials found — using heuristic fallback. "
        "Set OPENAI_API_KEY or WATSONX_API_KEY+WATSONX_PROJECT_ID for real inference."
    )

    error_spans = [s for s in spans if s.get("error")]
    if error_spans:
        culprit = error_spans[0]["service"]
        root_cause = (
            f"{culprit} returned HTTP {error_spans[0].get('status_code', '?')} "
            f"with {error_spans[0].get('latency_ms', '?')} ms latency — "
            f"likely caused the saga to partially commit. "
            f"Configure an LLM to generate a compensation plan with real IDs."
        )
        confidence = round(min(0.5 + len(error_spans) * 0.08, 0.90), 2)
    else:
        root_cause = f"No error spans found; alert was: {alert_summary}"
        confidence = 0.30

    # No compensations: the heuristic has no real order/payment IDs to act on.
    # The workflow will resolve immediately with the root_cause message above.
    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "compensations": [],
    }


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "get_telemetry_context": _handle_get_telemetry_context,
    "analyze_root_cause":    _handle_analyze_root_cause,
}


@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    body: MCPRequest,
    authorization: str | None = Header(default=None),
    mcp_protocol_version: str | None = Header(default=None, alias="Mcp-Protocol-Version"),
    mcp_method: str | None = Header(default=None, alias="Mcp-Method"),
) -> dict[str, Any]:
    """
    Stateless MCP tool dispatch endpoint.

    Required headers:
        Mcp-Protocol-Version: 2026-07-28
        Mcp-Method:           tools/call
        Authorization:        Bearer <MCP_API_KEY>

    Request body:
        { "tool": "<tool_name>", "arguments": { ... } }

    Returns the tool output dict directly on success.
    """
    _verify_auth(authorization)
    _verify_protocol(mcp_protocol_version)

    if mcp_method != "tools/call":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported Mcp-Method: {mcp_method!r}. Expected: 'tools/call'",
        )

    handler = _TOOL_HANDLERS.get(body.tool)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown tool: {body.tool!r}. Available: {list(_TOOL_HANDLERS)}",
        )

    logger.info("MCP tool call: tool=%s arguments_keys=%s", body.tool, list(body.arguments))
    return handler(body.arguments)


@app.get("/health")
async def health() -> dict[str, str]:
    """Simple liveness probe."""
    return {"status": "ok", "protocol_version": MCP_PROTOCOL}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "sre_swarm.mcp.server:app",
        host="0.0.0.0",
        port=MCP_SERVER_PORT,
        reload=False,
    )
