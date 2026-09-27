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
        extra_context:      list  — operator follow-up questions, optional

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
    extra_context: list[str] = [str(q) for q in arguments.get("extra_context") or []]
    spans: list[dict] = telemetry_context.get("spans", [])

    if not alert_summary:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="arguments.alert_summary is required",
        )

    if OPENAI_API_KEY:
        from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
            rca_openai_allowed,
            record_rca_openai_call,
        )

        if rca_openai_allowed():
            record_rca_openai_call()
            return _analyze_with_openai(alert_summary, spans, extra_context)
        logger.warning(
            "OpenAI daily RCA cap reached — using heuristic fallback for this request.",
        )

    if WATSONX_API_KEY and WATSONX_PROJECT_ID:
        return _analyze_with_watsonx(alert_summary, spans, extra_context)

    # Fallback: deterministic heuristic (no LLM required)
    return _analyze_heuristic(alert_summary, spans, extra_context)


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

IMPORTANT: Some spans may include "order_id" and/or "payment_id" fields containing
real IDs. You MUST use those exact values in the compensation payloads — never invent
IDs. If no span contains these fields, check the alert summary for ID references.

If the operator asked follow-up questions, answer them in the root_cause
and revise the compensation plan accordingly.

If no compensating transactions are needed, return an empty compensations list.
"""


def _build_user_message(
    alert_summary: str,
    spans: list[dict],
    extra_context: list[str] | None = None,
) -> str:
    message = (
        f"Alert: {alert_summary}\n\n"
        f"Spans:\n{json.dumps(spans, indent=2)}"
    )
    if extra_context:
        questions = "\n".join(f"- {question}" for question in extra_context)
        message += (
            "\n\nOperator follow-up questions:\n"
            f"{questions}\n"
            "Revise the root cause and compensation plan using these questions."
        )
    return message


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


def _analyze_with_openai(
    alert_summary: str,
    spans: list[dict],
    extra_context: list[str] | None = None,
) -> dict[str, Any]:
    """Call the OpenAI chat completions API for root-cause analysis (streaming)."""
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="openai package not installed. Run: pip install openai",
        ) from exc

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    logger.info("analyze_root_cause: using OpenAI backend (streaming)")
    chunks: list[str] = []
    with client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(alert_summary, spans, extra_context)},
        ],
        temperature=0.0,
        max_tokens=512,
        stream=True,
    ) as stream:
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                chunks.append(delta)
    raw = "".join(chunks)
    result = _parse_llm_json(raw)
    result["confidence"] = _finalize_confidence(
        spans,
        result.get("compensations", []),
        extra_context,
        result.get("confidence"),
    )
    return result


def _analyze_with_watsonx(
    alert_summary: str,
    spans: list[dict],
    extra_context: list[str] | None = None,
) -> dict[str, Any]:
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
    prompt = f"{_SYSTEM_PROMPT}\n\n{_build_user_message(alert_summary, spans, extra_context)}"
    result = model.generate_text(prompt=prompt, params={"max_new_tokens": 512, "temperature": 0.0})
    parsed = _parse_llm_json(result)
    parsed["confidence"] = _finalize_confidence(
        spans,
        parsed.get("compensations", []),
        extra_context,
        parsed.get("confidence"),
    )
    return parsed


def _compensations_match_span_ids(
    compensations: list[dict],
    order_id: str | None,
    payment_id: str | None,
) -> bool:
    """True when each planned rollback uses the IDs found on error spans."""
    if not compensations:
        return False
    need_order = bool(order_id)
    need_payment = bool(payment_id)
    have_order = not need_order
    have_payment = not need_payment
    for comp in compensations:
        payload = comp.get("payload") or {}
        ep = comp.get("endpoint", "")
        if ep == "/cancelOrder":
            if need_order:
                if payload.get("order_id") != order_id:
                    return False
                have_order = True
        elif ep == "/refundPayment":
            if need_payment:
                if payload.get("payment_id") != payment_id:
                    return False
                have_payment = True
    return have_order and have_payment


def _evidence_confidence(
    spans: list[dict],
    compensations: list[dict],
    extra_context: list[str] | None = None,
) -> float:
    """
    Deterministic confidence from telemetry + plan quality.

    Keeps the default demo incident (payment timeout fixture with gateway +
    payment errors) in the high-80s / low-90s so the human-approval beat still
    runs, while single clear failures with IDs can reach auto-approve.
    """
    error_spans = [s for s in spans if s.get("error")]
    if not error_spans:
        return 0.35

    order_id, payment_id = _compensation_ids_from_spans(spans)
    has_both_ids = bool(order_id and payment_id)
    comp_ok = _compensations_match_span_ids(compensations, order_id, payment_id)

    score = 0.52
    if len(error_spans) == 1:
        score += 0.18
    elif len(error_spans) == 2:
        score += 0.12
    else:
        score += min(len(error_spans) * 0.04, 0.16)

    if order_id:
        score += 0.07
    if payment_id:
        score += 0.07
    if compensations and comp_ok:
        score += 0.10
    elif compensations:
        score += 0.04

    if extra_context:
        score -= 0.07 * len(extra_context)

    payment_primary = any(
        s.get("service") == "payment-service"
        and int(s.get("status_code") or 0) >= 500
        for s in error_spans
    )
    error_services = {s.get("service") for s in error_spans if s.get("service")}

    score = max(score, 0.25)

    if len(error_services) >= 2 and payment_primary and has_both_ids and comp_ok:
        # Typical Trigger incident fixture: strong plan, still below auto-approve.
        score += 0.05
        score = min(score, 0.94)
    elif payment_primary and has_both_ids and comp_ok and len(error_spans) == 1:
        score = max(score, 0.96)

    return round(min(max(score, 0.0), 0.99), 2)


def _finalize_confidence(
    spans: list[dict],
    compensations: list[dict],
    extra_context: list[str] | None,
    llm_confidence: float | None = None,
) -> float:
    """Blend evidence with optional model-reported confidence."""
    evidence = _evidence_confidence(spans, compensations, extra_context)
    if llm_confidence is None:
        return evidence
    try:
        llm = float(llm_confidence)
    except (TypeError, ValueError):
        return evidence
    llm = min(max(llm, 0.0), 1.0)
    # Models often cluster around 0.85–0.92; telemetry should dominate.
    blended = 0.75 * evidence + 0.25 * llm
    return round(min(max(blended, 0.0), 0.99), 2)


def _compensation_ids_from_spans(spans: list[dict]) -> tuple[str | None, str | None]:
    """Collect order_id / payment_id annotated on error spans by the telemetry pipeline."""
    order_id: str | None = None
    payment_id: str | None = None
    for span in spans:
        if not span.get("error"):
            continue
        if not order_id and span.get("order_id"):
            order_id = str(span["order_id"])
        if not payment_id and span.get("payment_id"):
            payment_id = str(span["payment_id"])
    return order_id, payment_id


def _analyze_heuristic(
    alert_summary: str,
    spans: list[dict],
    extra_context: list[str] | None = None,
) -> dict[str, Any]:
    """
    Deterministic fallback when no LLM credentials are configured.

    Inspects error spans for the failing service and, when the telemetry
    pipeline embedded real order/payment IDs, emits a minimal saga plan so
    demos work without an API key.
    """
    logger.warning(
        "analyze_root_cause: no LLM credentials found — using heuristic fallback. "
        "Set OPENAI_API_KEY or WATSONX_API_KEY+WATSONX_PROJECT_ID for real inference."
    )

    error_spans = [s for s in spans if s.get("error")]
    if error_spans:
        culprit = error_spans[0]["service"]
        if len(error_spans) == 1:
            root_cause = (
                f"{culprit} returned HTTP {error_spans[0].get('status_code', '?')} "
                f"with {error_spans[0].get('latency_ms', '?')} ms latency — "
                f"likely caused the saga to partially commit."
            )
        else:
            summary = ", ".join(
                f"{s.get('service', '?')} ({s.get('status_code', '?')}, {s.get('latency_ms', '?')}ms)"
                for s in error_spans
            )
            root_cause = (
                f"Trace shows {len(error_spans)} failing spans: {summary}. "
                f"Likely root: {culprit} — saga may have partially committed."
            )
    else:
        root_cause = f"No error spans found; alert was: {alert_summary}"

    if extra_context:
        root_cause = f"{root_cause} Follow-up considered: {'; '.join(extra_context)}"

    order_id, payment_id = _compensation_ids_from_spans(spans)
    compensations: list[dict[str, Any]] = []
    if order_id:
        compensations.append({
            "endpoint": "/cancelOrder",
            "method": "POST",
            "payload": {"order_id": order_id},
        })
    if payment_id:
        compensations.append({
            "endpoint": "/refundPayment",
            "method": "POST",
            "payload": {"payment_id": payment_id},
        })
    if error_spans and not compensations:
        root_cause = (
            f"{root_cause} No order_id/payment_id on spans — "
            f"set OPENAI_API_KEY for LLM-generated compensations."
        )

    confidence = _evidence_confidence(spans, compensations, extra_context)

    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "compensations": compensations,
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
