"""
AegisSRE Dashboard
==================
A simple web UI that replaces the Temporal Web UI for this project.

Features:
  - Live incident list (polls every 3 s via SSE)
  - Trigger a new test incident with one click
  - Approve or reject a pending rollback with one click
  - Shows root cause, status, and compensation count

Run with:
    python -m sre_swarm.dashboard.app
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

load_dotenv()

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE    = "sre-swarm"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "7080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AegisSRE Dashboard", version="0.1.0")


# ---------------------------------------------------------------------------
# Temporal client (lazy singleton)
# ---------------------------------------------------------------------------

_client = None


async def _get_client():
    global _client
    if _client is None:
        from temporalio.client import Client  # noqa: PLC0415
        _client = await Client.connect(TEMPORAL_HOST)
    return _client


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AegisSRE Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    background: #0f1117;
    color: #e2e8f0;
    padding: 24px;
  }
  h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; color: #f8fafc; }
  .subtitle { color: #64748b; font-size: 13px; margin-bottom: 24px; }
  .topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }
  button {
    background: #3b82f6;
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
  }
  button:hover { background: #2563eb; }
  button:disabled { background: #334155; color: #64748b; cursor: default; }
  button.danger  { background: #ef4444; }
  button.danger:hover { background: #dc2626; }
  button.success { background: #22c55e; }
  button.success:hover { background: #16a34a; }
  button.small { padding: 4px 10px; font-size: 12px; }

  #status-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; background: #64748b; margin-right: 6px;
    vertical-align: middle;
  }
  #status-dot.connected { background: #22c55e; }
  #status-dot.error     { background: #ef4444; }
  #conn-label { font-size: 12px; color: #64748b; vertical-align: middle; }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left; padding: 8px 12px;
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .05em; color: #64748b;
    border-bottom: 1px solid #1e293b;
  }
  tbody tr { border-bottom: 1px solid #1e293b; }
  tbody tr:last-child { border-bottom: none; }
  tbody td { padding: 10px 12px; vertical-align: top; }
  tbody tr:hover td { background: #1e293b; }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  }
  .badge-detecting   { background: #1e3a5f; color: #60a5fa; }
  .badge-analyzing   { background: #312e81; color: #a5b4fc; }
  .badge-remediating { background: #3b2800; color: #fbbf24; }
  .badge-compensating{ background: #1a2e1a; color: #4ade80; }
  .badge-resolved    { background: #14532d; color: #86efac; }
  .badge-failed      { background: #450a0a; color: #fca5a5; }

  .mono { font-family: "JetBrains Mono", "Fira Code", monospace; font-size: 12px; }
  .muted { color: #64748b; font-size: 12px; }
  .root-cause { max-width: 360px; font-size: 12px; color: #94a3b8; }
  #empty { text-align: center; padding: 48px; color: #64748b; }
  #error-banner {
    display: none; background: #450a0a; color: #fca5a5;
    border: 1px solid #7f1d1d; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 16px; font-size: 13px;
  }
  .actions { display: flex; gap: 6px; }
  #trigger-btn { display: flex; align-items: center; gap: 8px; }
  #trigger-spinner {
    display: none; width: 14px; height: 14px;
    border: 2px solid #fff; border-top-color: transparent;
    border-radius: 50%; animation: spin .6s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<div class="topbar">
  <div>
    <h1>⚡ AegisSRE Dashboard</h1>
    <div class="subtitle">Autonomous incident response · live view</div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;">
    <span><span id="status-dot"></span><span id="conn-label">connecting…</span></span>
    <button id="trigger-btn" onclick="triggerIncident()">
      <div id="trigger-spinner"></div>
      Trigger incident
    </button>
  </div>
</div>

<div id="error-banner"></div>

<table id="incident-table">
  <thead>
    <tr>
      <th>Incident ID</th>
      <th>Service</th>
      <th>Status</th>
      <th>Root cause</th>
      <th>Compensations</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody id="tbody">
    <tr id="empty"><td colspan="6" id="empty">No incidents yet — click "Trigger incident" to start one.</td></tr>
  </tbody>
</table>

<script>
const rows = {};

function badge(status) {
  return `<span class="badge badge-${status}">${status}</span>`;
}

function renderRow(w) {
  const rc = w.root_cause
    ? `<div class="root-cause">${w.root_cause}</div>`
    : '<span class="muted">—</span>';

  let comps = '<span class="muted">—</span>';
  if (w.compensation_count != null && w.compensation_count > 0) {
    const lines = (w.compensations || [])
      .map(c => `${c.method} ${c.endpoint}  ${JSON.stringify(c.payload)}`)
      .join('&#10;');
    comps = `<span title="${lines}" style="cursor:default;border-bottom:1px dotted #64748b">`
          + `${w.compensation_count} step${w.compensation_count !== 1 ? 's' : ''}</span>`;
  } else if (w.compensation_count === 0) {
    comps = '<span class="muted">none</span>';
  }

  let actions = '';
  if (w.status === 'remediating') {
    actions = `
      <div class="actions">
        <button class="small success" onclick="approve('${w.workflow_id}')">✓ Approve</button>
        <button class="small danger"  onclick="reject('${w.workflow_id}')">✗ Reject</button>
      </div>`;
  }

  return `
    <td class="mono">${w.incident_id || w.workflow_id}</td>
    <td>${w.affected_service || '—'}</td>
    <td>${badge(w.status)}</td>
    <td>${rc}</td>
    <td>${comps}</td>
    <td>${actions}</td>`;
}

function upsertRow(w) {
  document.getElementById('empty')?.remove();
  let tr = rows[w.workflow_id];
  if (!tr) {
    tr = document.createElement('tr');
    tr.id = `row-${w.workflow_id}`;
    document.getElementById('tbody').prepend(tr);
    rows[w.workflow_id] = tr;
  }
  tr.innerHTML = renderRow(w);
}

// SSE stream
function connect() {
  const dot = document.getElementById('status-dot');
  const lbl = document.getElementById('conn-label');
  const es = new EventSource('/stream');

  es.onopen = () => {
    dot.className = 'connected';
    lbl.textContent = 'live';
  };
  es.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'snapshot') {
      data.workflows.forEach(upsertRow);
    } else if (data.type === 'update') {
      upsertRow(data.workflow);
    }
  };
  es.onerror = () => {
    dot.className = 'error';
    lbl.textContent = 'reconnecting…';
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();

async function triggerIncident() {
  const btn = document.getElementById('trigger-btn');
  const spinner = document.getElementById('trigger-spinner');
  btn.disabled = true;
  spinner.style.display = 'block';
  try {
    const r = await fetch('/trigger', { method: 'POST' });
    const body = await r.json();
    if (!r.ok) showError(body.detail || 'Failed to trigger incident');
  } catch(e) { showError(String(e)); }
  finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

async function approve(workflowId) {
  const r = await fetch(`/signal/${workflowId}/approve`, { method: 'POST' });
  if (!r.ok) { const b = await r.json(); showError(b.detail || 'Signal failed'); }
}

async function reject(workflowId) {
  const reason = prompt('Rejection reason (optional):') ?? '';
  const r = await fetch(`/signal/${workflowId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  if (!r.ok) { const b = await r.json(); showError(b.detail || 'Signal failed'); }
}

function showError(msg) {
  const b = document.getElementById('error-banner');
  b.textContent = '⚠ ' + msg;
  b.style.display = 'block';
  setTimeout(() => { b.style.display = 'none'; }, 6000);
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_HTML)


# ---------------------------------------------------------------------------
# SSE stream — pushes workflow snapshots every 3 s
# ---------------------------------------------------------------------------

async def _workflow_states() -> list[dict]:
    """Query Temporal for all IncidentResponseWorkflow executions."""
    try:
        client = await _get_client()
        results = []
        async for execution in client.list_workflows(
            query='WorkflowType="IncidentResponseWorkflow"',
        ):
            wf_id = execution.id
            try:
                handle = client.get_workflow_handle(wf_id)
                status_data: dict = await handle.query("get_status")
                results.append({
                    "workflow_id": wf_id,
                    "incident_id": wf_id.replace("incident-", ""),
                    "affected_service": _parse_service(wf_id),
                    "status": str(status_data.get("status", "")).split(".")[-1].lower(),
                    "root_cause": status_data.get("root_cause"),
                    "rollback_approved": status_data.get("rollback_approved", False),
                    "resolution_notes": status_data.get("resolution_notes", ""),
                    "compensation_count": status_data.get("compensation_count"),
                    "compensations": status_data.get("compensations", []),
                })
            except Exception as e:
                logger.debug("Could not query workflow %s: %s", wf_id, e)
                results.append({
                    "workflow_id": wf_id,
                    "incident_id": wf_id.replace("incident-", ""),
                    "affected_service": _parse_service(wf_id),
                    "status": str(execution.status).split(".")[-1].lower(),
                    "root_cause": None,
                    "rollback_approved": False,
                    "resolution_notes": "",
                    "compensation_count": None,
                })
        return results
    except Exception as e:
        logger.warning("Failed to list workflows: %s", e)
        return []


def _parse_service(wf_id: str) -> str:
    """Best-effort: extract service name from workflow ID."""
    # workflow IDs look like: incident-INC-ABCD1234
    return "payment-service"


async def _sse_generator() -> AsyncGenerator[str, None]:
    # Send initial snapshot
    workflows = await _workflow_states()
    yield f"data: {json.dumps({'type': 'snapshot', 'workflows': workflows})}\n\n"

    while True:
        await asyncio.sleep(3)
        workflows = await _workflow_states()
        yield f"data: {json.dumps({'type': 'snapshot', 'workflows': workflows})}\n\n"


@app.get("/stream")
async def stream() -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Trigger a new incident
# ---------------------------------------------------------------------------

@app.post("/trigger")
async def trigger_incident() -> dict:
    from sre_swarm.workflows.incident_response import IncidentInput, IncidentResponseWorkflow  # noqa: PLC0415

    try:
        client = await _get_client()
        incident = IncidentInput(
            incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
            affected_service="payment-service",
            alert_summary="HTTP 500 spike on /checkout — p99 latency > 4 s",
            trace_ids=["trace-timeout-001", "trace-002"],
        )
        handle = await client.start_workflow(
            IncidentResponseWorkflow.run,
            incident,
            id=f"incident-{incident.incident_id}",
            task_queue=TASK_QUEUE,
        )
        logger.info("Triggered workflow id=%s", handle.id)
        return {"workflow_id": handle.id, "incident_id": incident.incident_id}
    except Exception as e:
        from fastapi import HTTPException  # noqa: PLC0415
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Approve / reject signals
# ---------------------------------------------------------------------------

@app.post("/signal/{workflow_id}/approve")
async def approve_rollback(workflow_id: str) -> dict:
    from sre_swarm.workflows.incident_response import IncidentResponseWorkflow  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    try:
        client = await _get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(IncidentResponseWorkflow.approve_rollback)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/signal/{workflow_id}/reject")
async def reject_rollback(workflow_id: str, body: dict = {}) -> dict:
    from sre_swarm.workflows.incident_response import IncidentResponseWorkflow  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    try:
        client = await _get_client()
        handle = client.get_workflow_handle(workflow_id)
        reason: str = body.get("reason", "")
        await handle.signal(IncidentResponseWorkflow.reject_rollback, reason)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "sre_swarm.dashboard.app:app",
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        reload=False,
    )
