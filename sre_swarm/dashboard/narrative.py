"""
Post-incident narrative for the dashboard "Explain" action.

Uses OpenAI when OPENAI_API_KEY is set; otherwise a deterministic template.
Results are cached in memory per workflow id.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

_NARRATIVE_CACHE: dict[str, dict[str, Any]] = {}

_SYSTEM_PROMPT = """\
You explain SRE incidents to engineering leaders and hackathon judges.
Use ONLY facts present in the incident JSON. Do not invent services, IDs, or outcomes.
If something is missing, say it was not recorded.

Respond with a single JSON object (no markdown fences) with exactly these string fields:
{
  "what_happened": "2-4 sentences on the failure and blast radius",
  "how_solved": "2-4 sentences on analysis, approval/auto-approve, compensations, verification",
  "real_world": "2-4 sentences on why this pattern matters in production (sagas, human gate, durable workflows)"
}
Keep each field under 120 words. Plain text only.
"""


def build_incident_context(workflow_id: str, status: dict[str, Any]) -> dict[str, Any]:
    """Normalize workflow query output for prompts and templates."""
    return {
        "workflow_id": workflow_id,
        "incident_id": workflow_id.replace("incident-", "", 1),
        "status": str(status.get("status", "")).split(".")[-1].lower(),
        "affected_service": status.get("affected_service"),
        "alert_summary": status.get("alert_summary"),
        "error_spans": status.get("error_spans") or [],
        "root_cause": status.get("root_cause"),
        "confidence": status.get("confidence"),
        "auto_approved": status.get("auto_approved"),
        "rollback_approved": status.get("rollback_approved"),
        "extra_context": status.get("extra_context") or [],
        "compensations": status.get("compensations") or [],
        "compensation_results": status.get("compensation_results") or [],
        "verification": status.get("verification") or {},
        "resolution_notes": status.get("resolution_notes") or "",
    }


def narrative_from_template(context: dict[str, Any]) -> dict[str, str]:
    """Deterministic explainer when no LLM key is configured."""
    errors = context.get("error_spans") or []
    if errors:
        err_text = ", ".join(
            f"{e.get('service', '?')} HTTP {e.get('status_code', '?')} "
            f"({e.get('latency_ms', '?')} ms)"
            for e in errors
        )
        what = (
            f"Alert on {context.get('affected_service') or 'the service'}: "
            f"{context.get('alert_summary') or 'checkout degradation'}. "
            f"Telemetry showed failing spans: {err_text}. "
            f"Analysis: {context.get('root_cause') or 'root cause not recorded'}."
        )
    else:
        what = (
            f"Incident {context.get('incident_id')} on "
            f"{context.get('affected_service') or 'unknown service'}. "
            f"{context.get('root_cause') or 'Details were not captured in telemetry.'}"
        )

    comps = context.get("compensation_results") or context.get("compensations") or []
    comp_lines = []
    for c in comps:
        if "ok" in c:
            mark = "succeeded" if c.get("ok") else "failed"
            comp_lines.append(f"{c.get('method', 'POST')} {c.get('endpoint', '?')} {mark}")
        else:
            comp_lines.append(f"{c.get('method', 'POST')} {c.get('endpoint', '?')} (planned)")

    approval = "auto-approved" if context.get("auto_approved") else (
        "approved by an operator" if context.get("rollback_approved") else "approval path unclear"
    )
    conf = context.get("confidence")
    conf_txt = f" Confidence was {round(float(conf) * 100)}%." if conf is not None else ""
    verify = context.get("verification") or {}
    if verify.get("healthy") is True:
        verify_txt = " Post-rollback verification passed on mock services and telemetry."
    elif verify.get("healthy") is False:
        verify_txt = " Verification did not fully pass; the workflow may still be open or failed."
    else:
        verify_txt = ""

    how = (
        f"The swarm pulled traces, ran root-cause analysis,{conf_txt} "
        f"The rollback plan was {approval}."
    )
    if comp_lines:
        how += f" Compensations: {'; '.join(comp_lines)}."
    how += verify_txt
    if context.get("resolution_notes"):
        how += f" {context['resolution_notes'][:280]}"

    real = (
        "Checkout flows often commit orders and payments in steps; when payment fails late, "
        "you can leave a successful order with a failed charge. AegisSRE drafts saga "
        "compensations (cancel/refund) from traces, keeps a human in the loop when confidence "
        "is below threshold, and runs the plan through Temporal so retries and worker crashes "
        "do not lose progress. Verification checks that state actually matches the plan—not "
        "just that scripts ran."
    )
    if context.get("extra_context"):
        real += (
            " Follow-up questions from operators can re-run analysis before approval, "
            "similar to a real incident channel."
        )

    return {
        "what_happened": what.strip(),
        "how_solved": how.strip(),
        "real_world": real.strip(),
    }


def _parse_narrative_json(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    for key in ("what_happened", "how_solved", "real_world"):
        if key not in data or not str(data[key]).strip():
            raise ValueError(f"Missing narrative field: {key}")
    return {k: str(data[k]).strip() for k in ("what_happened", "how_solved", "real_world")}


def generate_narrative_openai(context: dict[str, Any]) -> dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("openai package not installed") from exc

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Incident record:\n" + json.dumps(context, indent=2),
            },
        ],
        temperature=0.3,
        max_tokens=650,
    )
    raw = response.choices[0].message.content or ""
    return _parse_narrative_json(raw)


def get_cached_narrative(workflow_id: str) -> dict[str, Any] | None:
    return _NARRATIVE_CACHE.get(workflow_id)


def clear_narrative_cache() -> None:
    _NARRATIVE_CACHE.clear()


def produce_narrative(
    workflow_id: str,
    status: dict[str, Any],
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """
    Return cached or freshly generated narrative for a workflow snapshot.
    """
    if not refresh and workflow_id in _NARRATIVE_CACHE:
        return _NARRATIVE_CACHE[workflow_id]

    context = build_incident_context(workflow_id, status)
    terminal = context["status"] in ("resolved", "failed")
    if not terminal:
        raise ValueError(
            "Narrative is available after the incident reaches resolved or failed status.",
        )

    source = "template"
    sections = narrative_from_template(context)
    if OPENAI_API_KEY:
        from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
            narrative_openai_allowed,
            record_narrative_openai_call,
        )

        if narrative_openai_allowed():
            try:
                record_narrative_openai_call()
                sections = generate_narrative_openai(context)
                source = "openai"
            except Exception as exc:
                logger.warning("OpenAI narrative failed, using template: %s", exc)
        else:
            logger.warning("OpenAI daily narrative cap reached — using template.")

    payload = {
        "workflow_id": workflow_id,
        "source": source,
        "sections": sections,
    }
    _NARRATIVE_CACHE[workflow_id] = payload
    return payload
