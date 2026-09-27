"""
AegisSRE Dashboard
==================
A simple web UI that replaces the Temporal Web UI for this project.

Features:
  - Live incident list (polls every 3 s via SSE)
  - Trigger a new test incident with one click
  - Approve or reject a pending rollback with one click
  - Request more info from the SRE agent (follow-up question)
  - Shows root cause, confidence, resolution notes, per-step compensation
    results, and whether post-rollback verification passed
  - Webhook intake: POST /incident accepts PagerDuty and Alertmanager payloads
  - Explain: POST /narrative/{workflow_id} — AI/template postmortem for resolved incidents

Run with:
    python -m sre_swarm.dashboard.app

Serves the marketing landing page at ``/`` and the live incident console at ``/dashboard``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Response, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
import uvicorn

from sre_swarm.landing.page import LANDING_HTML

load_dotenv()

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE    = "sre-swarm"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "7080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AegisSRE Dashboard", version="0.1.0")


def _client_ip(request: Request) -> str:
    from sre_swarm.demo.spend_guard import client_ip_from_headers  # noqa: PLC0415

    peer = request.client.host if request.client else None
    return client_ip_from_headers(request.headers.get("x-forwarded-for"), peer)


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
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --paper: #f3f1ec;
    --paper-2: #e9e6df;
    --ink: #16150f;
    --ink-2: #4a473f;
    --ink-3: #8a867b;
    --rule: #d4d0c6;
    --signal: #e8491d;
    --ok: #2f7d4f;
    --term: #16150f;
    --term-text: #d9d5ca;
    --term-dim: #7d796e;
    --sans: "IBM Plex Sans", -apple-system, "Segoe UI", system-ui, sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.55;
    background: var(--paper);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  a { color: inherit; }
  code, pre, .mono { font-family: var(--mono); }
  ::selection { background: var(--signal); color: var(--paper); }

  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 32px 48px; }

  .bar {
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 22px 0; border-bottom: 1px solid var(--ink);
  }
  .mark { font-family: var(--mono); font-weight: 600; font-size: 15px; letter-spacing: -0.01em; text-decoration: none; }
  .mark i { font-style: normal; color: var(--signal); }
  .bar nav { display: flex; gap: 28px; font-size: 14px; }
  .bar nav a { text-decoration: none; color: var(--ink-2); }
  .bar nav a:hover { color: var(--ink); }
  .bar nav a.here { color: var(--ink); font-weight: 500; }

  .dash-head {
    padding: 48px 0 32px;
    border-bottom: 1px solid var(--rule);
    margin-bottom: 24px;
  }
  .eyebrow {
    font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    text-transform: uppercase; letter-spacing: .08em; margin-bottom: 14px;
  }
  .dash-head h1 {
    font-size: clamp(28px, 3.5vw, 38px); line-height: 1.08; font-weight: 600;
    letter-spacing: -0.03em; margin-bottom: 12px;
  }
  .dash-head h1 em { font-style: normal; color: var(--signal); }
  .dash-head p { color: var(--ink-2); font-size: 16px; max-width: 42em; }

  .toolbar {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .home-link {
    font-size: 13px;
    color: var(--ink-2);
    text-decoration: none;
    padding-bottom: 10px;
  }
  .home-link:hover { color: var(--ink); }
  .trigger-group {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px;
    flex: 1 1 520px;
    justify-content: flex-end;
  }
  .scenario-field { display: flex; flex-direction: column; gap: 6px; }
  .scenario-field label {
    font-family: var(--mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--ink-3);
  }
  .scenario-field select {
    font-family: var(--sans);
    font-size: 14px;
    padding: 10px 12px;
    min-width: 220px;
    border: 1px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    border-radius: 2px;
  }
  #scenario-blurb {
    flex: 1 1 280px;
    max-width: 420px;
    font-size: 13px;
    color: var(--ink-2);
    line-height: 1.45;
    padding-bottom: 10px;
  }
  #demo-passcode-wrap { display: flex; flex-direction: column; gap: 6px; }
  #demo-passcode {
    font-family: var(--mono);
    font-size: 13px;
    padding: 10px 12px;
    border: 1px solid var(--rule);
    min-width: 160px;
  }

  .conn-pill {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
  }
  #status-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--ink-3);
  }
  #status-dot.connected { background: var(--ok); }
  #status-dot.error     { background: var(--signal); }

  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--ink); color: var(--paper);
    padding: 11px 18px; border: 0; border-radius: 2px;
    font-family: var(--sans); font-size: 14px; font-weight: 500;
    cursor: pointer;
  }
  .btn:hover:not(:disabled) { background: var(--signal); }
  .btn:disabled { background: var(--rule); color: var(--ink-3); cursor: default; }

  button.small {
    font-family: var(--mono);
    font-size: 11.5px;
    font-weight: 500;
    padding: 6px 10px;
    border-radius: 2px;
    border: 1px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
  }
  button.small:hover { background: var(--ink); color: var(--paper); }
  button.small.success { border-color: var(--ok); color: var(--ok); }
  button.small.success:hover { background: var(--ok); color: var(--paper); }
  button.small.danger { border-color: var(--signal); color: var(--signal); }
  button.small.danger:hover { background: var(--signal); color: var(--paper); }
  button.small.warning { border-color: var(--ink-2); color: var(--ink-2); }
  button.small.warning:hover { background: var(--ink-2); color: var(--paper); }

  .guide {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: 32px 48px;
    padding: 24px 0 28px;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--rule);
    font-size: 14px;
  }
  .guide h2 {
    font-size: 16px; font-weight: 600; letter-spacing: -0.01em;
    margin-bottom: 8px;
  }
  .guide p { color: var(--ink-2); margin-bottom: 12px; max-width: 36em; }
  .guide a { color: var(--ink); text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--rule); }
  .guide a:hover { text-decoration-color: var(--signal); }
  .lifecycle {
    list-style: none;
    display: grid;
    gap: 0;
    border-top: 1px solid var(--rule);
  }
  .lifecycle li {
    display: grid;
    grid-template-columns: 108px 1fr;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--rule);
    color: var(--ink-2);
    font-size: 13.5px;
  }
  .lifecycle li strong {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
  }
  .lifecycle li.hold strong { color: var(--signal); }

  #phase-hint {
    display: none;
    background: var(--paper-2);
    border-left: 2px solid var(--signal);
    padding: 12px 16px;
    margin-bottom: 16px;
    font-size: 14px;
    color: var(--ink-2);
  }
  #phase-hint b { color: var(--ink); font-weight: 600; }

  .incidents { border-top: 1px solid var(--ink); padding-top: 28px; }
  .incidents .sec-label {
    font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    letter-spacing: .06em; margin-bottom: 16px;
  }
  .status-cell .hint {
    display: block;
    margin-top: 4px;
    font-family: var(--mono);
    font-size: 10.5px;
    color: var(--term-dim);
    max-width: 11em;
    line-height: 1.4;
  }
  .service-cell .mini-label {
    display: block;
    font-family: var(--mono);
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--term-dim);
    margin-bottom: 2px;
  }
  .service-cell .alert-line {
    margin-top: 6px;
    font-size: 12px;
    color: var(--term-dim);
    max-width: 220px;
    line-height: 1.4;
  }
  .service-cell .trace-errors {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed #3a3830;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.5;
    max-width: 240px;
  }
  .service-cell .trace-errors .err-svc {
    color: #ff7a4d;
    white-space: nowrap;
  }
  .plan-step {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--term-dim);
    line-height: 1.45;
  }
  .plan-step.pending { color: #f2c94c; }
  .verify-tag {
    display: inline-block;
    margin-top: 6px;
    font-family: var(--mono);
    font-size: 10.5px;
    letter-spacing: .04em;
    text-transform: uppercase;
  }
  .verify-tag.pass { color: #6fcf97; }
  .verify-tag.fail { color: #ff7a4d; }

  .term {
    background: var(--term); color: var(--term-text);
    border-radius: 4px;
    box-shadow: 0 1px 0 var(--ink), 12px 12px 0 var(--paper-2);
    overflow: hidden;
  }
  .term-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; border-bottom: 1px solid #2c2a23;
    font-family: var(--mono); font-size: 11.5px; color: var(--term-dim);
  }
  .term-head .live { color: var(--term-text); }

  .table-wrap { overflow-x: auto; }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  thead th {
    text-align: left;
    padding: 10px 14px;
    border-bottom: 1px solid #2c2a23;
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--term-dim);
    white-space: nowrap;
  }
  tbody td {
    padding: 12px 14px;
    border-top: 1px solid #2c2a23;
    vertical-align: top;
    color: var(--term-text);
  }
  tbody tr:hover td { background: #1f1d17; }

  .badge {
    display: inline-block;
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .05em;
    padding: 2px 0;
  }
  .badge-detecting    { color: #8eb4ff; }
  .badge-analyzing    { color: #c4a8ff; }
  .badge-remediating  { color: #f2c94c; }
  .badge-compensating { color: #6fcf97; }
  .badge-resolved     { color: #6fcf97; }
  .badge-failed       { color: #ff7a4d; }

  .muted { color: var(--term-dim); font-size: 12px; }
  .root-cause { max-width: 280px; font-size: 12.5px; line-height: 1.5; }
  .resolution { max-width: 280px; font-size: 12px; color: #6fcf97; font-style: italic; }
  .resolution.failed { color: #ff7a4d; }
  .resolution.warn { color: #f2c94c; }
  .step-ok { color: #6fcf97; font-size: 12px; font-family: var(--mono); }
  .step-fail { color: #ff7a4d; font-size: 12px; font-family: var(--mono); }
  .comps-hint {
    cursor: default;
    border-bottom: 1px dotted var(--term-dim);
    font-family: var(--mono);
    font-size: 12px;
  }
  .auto-tag {
    display: inline-block;
    margin-left: 6px;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: #6fcf97;
  }

  #empty-row td {
    text-align: center;
    padding: 48px 16px;
    color: var(--term-dim);
    font-family: var(--mono);
    font-size: 12px;
  }

  #error-banner, #success-banner {
    display: none;
    background: var(--paper-2);
    padding: 12px 16px;
    margin-bottom: 20px;
    font-size: 14px;
  }
  #error-banner {
    color: var(--signal);
    border-left: 2px solid var(--signal);
  }
  #success-banner {
    color: var(--ok);
    border-left: 2px solid var(--ok);
  }

  .follow-up {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed #3a3830;
    font-family: var(--mono);
    font-size: 11px;
    color: #f2c94c;
    line-height: 1.45;
  }
  .follow-up-label {
    display: block;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--term-dim);
    margin-bottom: 4px;
  }

  .ask-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(22, 21, 15, 0.45);
    z-index: 100;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .ask-backdrop.open { display: flex; }
  .ask-panel {
    width: min(480px, 100%);
    background: var(--paper);
    border: 1px solid var(--ink);
    box-shadow: 12px 12px 0 var(--paper-2);
    padding: 24px;
  }
  .ask-panel h3 {
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 8px;
    letter-spacing: -0.01em;
  }
  .ask-panel p {
    font-size: 14px;
    color: var(--ink-2);
    margin-bottom: 16px;
  }
  .ask-panel textarea {
    width: 100%;
    min-height: 96px;
    font-family: var(--sans);
    font-size: 14px;
    padding: 12px;
    border: 1px solid var(--rule);
    background: #fff;
    resize: vertical;
    margin-bottom: 16px;
  }
  .ask-panel textarea:focus {
    outline: 2px solid var(--ink);
    outline-offset: 0;
  }
  .ask-actions { display: flex; gap: 10px; justify-content: flex-end; }
  .ask-actions button.secondary {
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--ink);
  }
  .ask-actions button.secondary:hover { background: var(--paper-2); }

  .narrative-body { font-size: 14px; color: var(--ink-2); }
  .narrative-body .sec { margin-bottom: 20px; }
  .narrative-body h4 {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--ink-3);
    margin-bottom: 8px;
  }
  .narrative-body p { line-height: 1.55; }
  .narrative-meta {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    margin-bottom: 16px;
  }
  .narrative-loading {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink-2);
    padding: 12px 0;
  }

  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  #trigger-spinner {
    display: none; width: 14px; height: 14px;
    border: 2px solid var(--paper); border-top-color: transparent;
    border-radius: 50%; animation: spin .6s linear infinite;
  }

  footer {
    margin-top: 40px;
    border-top: 1px solid var(--ink);
    padding: 24px 0 8px;
    display: flex;
    justify-content: space-between;
    gap: 24px;
    flex-wrap: wrap;
    font-size: 13px;
    color: var(--ink-3);
  }
  footer a { color: var(--ink-2); text-decoration: none; }
  footer a:hover { color: var(--ink); }

  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 900px) {
    .bar nav a:not(.here) { display: none; }
  }
  @media (max-width: 900px) {
    .guide { grid-template-columns: 1fr; }
  }
  @media (max-width: 720px) {
    .wrap { padding: 0 20px 40px; }
    .toolbar { flex-direction: column; align-items: stretch; }
    #trigger-btn { justify-content: center; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header class="bar">
    <a class="mark" href="/">aegis<i>/</i>sre</a>
    <nav>
      <a href="/">Overview</a>
      <a href="https://github.com/soggy8/AegisSRE" target="_blank" rel="noopener">Source</a>
      <a class="here" href="/dashboard">Dashboard</a>
    </nav>
  </header>

  <div class="dash-head">
    <p class="eyebrow">Hackathon · Distributed Systems Track</p>
    <h1>Autonomous SRE Orchestration Swarm</h1>
    <p>Live incident console — trigger rollbacks, approve compensations, and watch verification complete in real time.</p>
  </div>
  <div class="toolbar">
    <a href="/" class="home-link">← Home</a>
    <span class="conn-pill">
      <span id="status-dot"></span>
      <span id="conn-label">connecting…</span>
    </span>
    <div class="trigger-group">
      <div class="scenario-field">
        <label for="scenario-select">Incident type</label>
        <select id="scenario-select" aria-describedby="scenario-blurb"></select>
      </div>
      <div class="scenario-field" id="demo-passcode-wrap">
        <label for="demo-passcode" id="demo-passcode-label">Demo passcode</label>
        <input id="demo-passcode" type="password" autocomplete="off"
               placeholder="Paste passcode from project README"/>
      </div>
      <button class="btn" id="trigger-btn" type="button" onclick="triggerIncident()">
        <div id="trigger-spinner"></div>
        Trigger incident
      </button>
    </div>
  </div>
  <p id="scenario-blurb"></p>

  <div id="error-banner"></div>
  <div id="success-banner"></div>

  <div class="ask-backdrop" id="ask-backdrop" role="dialog" aria-labelledby="ask-title">
    <div class="ask-panel">
      <h3 id="ask-title">Ask the SRE agent</h3>
      <p>Your question is sent to <code>analyze_root_cause</code> as extra context. The row will move to <strong>analyzing</strong>, then return to <strong>remediating</strong> with an updated root cause and plan.</p>
      <textarea id="ask-input" placeholder="e.g. Is the database involved? Should we refund before canceling the order?"></textarea>
      <div class="ask-actions">
        <button type="button" class="btn secondary" onclick="closeAskModal()">Cancel</button>
        <button type="button" class="btn" id="ask-submit" onclick="submitAskModal()">Send question</button>
      </div>
    </div>
  </div>

  <div class="ask-backdrop" id="narrative-backdrop" role="dialog" aria-labelledby="narrative-title">
    <div class="ask-panel narrative-panel">
      <h3 id="narrative-title">Incident explainer</h3>
      <p class="narrative-meta" id="narrative-meta"></p>
      <div id="narrative-body" class="narrative-body">
        <div class="narrative-loading">Generating summary…</div>
      </div>
      <div class="ask-actions">
        <button type="button" class="btn secondary" onclick="closeNarrativeModal()">Close</button>
        <button type="button" class="btn secondary" id="narrative-refresh" onclick="refreshNarrative()">Regenerate</button>
      </div>
    </div>
  </div>

  <div class="guide" id="guide">
    <div>
      <h2>What you are looking at</h2>
      <p>
        Each row is one <strong>Temporal workflow</strong> for an incident: telemetry pull,
        AI/heuristic root-cause analysis, a drafted <strong>saga rollback plan</strong>, optional
        human approval, then compensating HTTP calls and a final verification pass.
      </p>
      <p>
        Click <strong>Trigger incident</strong> to run the demo path end-to-end, or read the full
        story on the <a href="/#lifecycle">overview page</a>.
      </p>
    </div>
    <ol class="lifecycle">
      <li><strong>detecting</strong><span>Pull trace spans for the alert via MCP.</span></li>
      <li><strong>analyzing</strong><span>Infer root cause and draft refund/cancel steps.</span></li>
      <li class="hold"><strong>remediating</strong><span>Plan is visible — Approve runs rollback; Ask re-runs analysis with your question (then back here).</span></li>
      <li><strong>compensating</strong><span>Run rollback activities; ✓/✗ per step.</span></li>
      <li><strong>resolved</strong><span>Mock services and telemetry checked after rollback. Use <strong>Explain</strong> for an AI postmortem.</span></li>
    </ol>
  </div>

  <div id="phase-hint" role="status"></div>

  <section class="incidents">
    <p class="sec-label">01 · Active incidents</p>
    <div class="term">
      <div class="term-head">
        <span class="live">workflow stream</span>
        <span>SSE · 3s poll</span>
      </div>
      <div class="table-wrap">
        <table id="incident-table">
          <thead>
            <tr>
              <th>Incident ID</th>
              <th>Alert &amp; trace errors</th>
              <th>Status</th>
              <th>Root cause</th>
              <th>Resolution</th>
              <th>Compensations</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr id="empty-row"><td colspan="7">No incidents yet — click <strong>Trigger incident</strong> to start one.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <footer>
    <span>AegisSRE · Temporal · MCP · Saga verification</span>
    <a href="/">← Back to overview</a>
  </footer>
</div>

<script>
const rows = {};
let askWorkflowId = null;
let narrativeWorkflowId = null;
let demoScenarios = [];
let demoConfig = { passcode_required: false };

async function loadDemoConfig() {
  const label = document.getElementById('demo-passcode-label');
  const input = document.getElementById('demo-passcode');
  try {
    const r = await fetch('/demo/config');
    demoConfig = await r.json();
  } catch (e) {
    demoConfig = { passcode_required: false };
  }
  if (demoConfig.passcode_required) {
    label.textContent = 'Demo passcode (required)';
    input.required = true;
    input.placeholder = 'Required to trigger on this host';
  } else {
    label.textContent = 'Demo passcode (optional)';
    input.required = false;
    input.placeholder = 'Only needed if the host requires it';
  }
}

async function loadScenarios() {
  try {
    const r = await fetch('/scenarios');
    demoScenarios = await r.json();
    const sel = document.getElementById('scenario-select');
    sel.innerHTML = demoScenarios.map((s) => (
      `<option value="${esc(s.id)}">${esc(s.title)}</option>`
    )).join('');
    sel.addEventListener('change', updateScenarioBlurb);
    updateScenarioBlurb();
  } catch (e) {
    document.getElementById('scenario-blurb').textContent = 'Could not load incident types.';
  }
}
function updateScenarioBlurb() {
  const id = document.getElementById('scenario-select').value;
  const s = demoScenarios.find((x) => x.id === id);
  document.getElementById('scenario-blurb').textContent = s
    ? `${s.description} (alert on ${s.affected_service})`
    : '';
}
loadDemoConfig();
loadScenarios();

const STATUS_HELP = {
  detecting: 'Fetching telemetry for this alert',
  analyzing: 'Root-cause analysis in progress',
  remediating: 'Waiting for you — review plan, then Approve',
  compensating: 'Applying rollback steps',
  resolved: 'Rollback finished; verification passed',
  failed: 'Stopped — rejected, timed out, or activity failed',
};

function badge(status) {
  const hint = STATUS_HELP[status] || '';
  return `<span class="badge badge-${esc(status)}">${esc(status)}</span>`
       + (hint ? `<span class="hint">${esc(hint)}</span>` : '');
}

function renderPlanSteps(compensations, pending) {
  return (compensations || []).map((c) => {
    const payload = c.payload ? ` ${JSON.stringify(c.payload)}` : '';
    const cls = pending ? 'plan-step pending' : 'plan-step';
    return `<div class="${cls}">${esc(c.method)} ${esc(c.endpoint)}${esc(payload)}</div>`;
  }).join('');
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function renderRow(w) {
  let rc = '<span class="muted">—</span>';
  if (w.root_cause) {
    const pct = (w.confidence == null)
      ? ''
      : `<div class="muted">confidence ${Math.round(w.confidence * 100)}%`
        + (w.confidence >= 0.95 ? '' : ' · needs approval')
        + `${w.auto_approved ? '<span class="auto-tag">auto</span>' : ''}</div>`;
    rc = `<div class="root-cause">${esc(w.root_cause)}</div>${pct}`;
  }
  if (w.extra_context && w.extra_context.length) {
    const qs = w.extra_context.map((q) => `<div>↳ ${esc(q)}</div>`).join('');
    rc += `<div class="follow-up"><span class="follow-up-label">Your questions</span>${qs}</div>`;
  }

  let resolution = '<span class="muted">—</span>';
  if (w.resolution_notes || (w.verification && w.verification.findings)) {
    const notes = w.resolution_notes || (w.verification.findings || []).join(' ');
    const cls = w.status === 'failed'
      ? 'resolution failed'
      : (w.status === 'compensating' ? 'resolution warn' : 'resolution');
    const shown = notes.length > 120 ? notes.slice(0, 120) + '…' : notes;
    let verify = '';
    if (w.verification && w.verification.healthy != null) {
      const ok = w.verification.healthy;
      verify = `<span class="verify-tag ${ok ? 'pass' : 'fail'}">verification ${ok ? 'passed' : 'failed'}</span>`;
    }
    resolution = `<div class="${cls}" title="${esc(notes)}">${esc(shown)}</div>${verify}`;
  }

  let comps = '<span class="muted">—</span>';
  if (w.compensation_results && w.compensation_results.length) {
    comps = w.compensation_results.map((c) => {
      const cls = c.ok ? 'step-ok' : 'step-fail';
      const mark = c.ok ? '✓' : '✗';
      return `<div class="${cls}" title="${esc(c.detail)}">${mark} ${esc(c.method)} ${esc(c.endpoint)}</div>`;
    }).join('');
  } else if (w.compensations && w.compensations.length) {
    comps = renderPlanSteps(w.compensations, w.status === 'remediating' || w.status === 'analyzing');
  } else if (w.compensation_count === 0) {
    comps = '<span class="muted">none needed</span>';
  }

  let actions = '';
  if (w.status === 'remediating') {
    actions = `
      <div class="actions">
        <button class="small success" onclick="approve('${w.workflow_id}')">✓ Approve</button>
        <button class="small danger"  onclick="reject('${w.workflow_id}')">✗ Reject</button>
        <button class="small warning" onclick="openAskModal('${w.workflow_id}')">? Ask</button>
      </div>`;
  } else if (w.status === 'resolved' || w.status === 'failed') {
    actions = `
      <div class="actions">
        <button class="small" onclick="openNarrativeModal('${w.workflow_id}')">Explain</button>
      </div>`;
  }

  const svc = esc(w.affected_service || '—');
  const alertLine = w.alert_summary
    ? `<div class="alert-line" title="${esc(w.alert_summary)}">${esc(w.alert_summary.length > 72 ? w.alert_summary.slice(0, 72) + '…' : w.alert_summary)}</div>`
    : '';
  let traceErrors = '';
  if (w.error_spans && w.error_spans.length) {
    const parts = w.error_spans.map((e) => {
      const code = e.status_code != null ? e.status_code : '?';
      const lat = e.latency_ms != null ? ` · ${e.latency_ms}ms` : '';
      return `<span class="err-svc">${esc(e.service)} (${code}${lat})</span>`;
    }).join('<span class="muted">, </span>');
    traceErrors = `<div class="trace-errors"><span class="mini-label">Errors in trace</span>${parts}</div>`;
  }

  return `
    <td class="mono">${w.incident_id || w.workflow_id}</td>
    <td class="service-cell"><span class="mini-label">Alert on</span>${svc}${alertLine}${traceErrors}</td>
    <td class="status-cell">${badge(w.status)}</td>
    <td>${rc}</td>
    <td>${resolution}</td>
    <td>${comps}</td>
    <td>${actions}</td>`;
}

function updatePhaseHint(workflows) {
  const el = document.getElementById('phase-hint');
  if (!workflows.length) {
    el.style.display = 'none';
    return;
  }
  const rem = workflows.filter((w) => w.status === 'remediating');
  const reanalyzing = workflows.filter((w) => w.status === 'analyzing' && w.extra_context && w.extra_context.length);
  if (reanalyzing.length) {
    el.innerHTML = '<b>Re-analyzing:</b> The agent is incorporating your follow-up question(s). '
      + 'Status will return to <b>remediating</b> with an updated root cause — usually within a few seconds.';
    el.style.display = 'block';
    return;
  }
  if (rem.length) {
    el.innerHTML = '<b>Action needed:</b> One or more incidents are in '
      + '<b>remediating</b> — review the rollback plan in the Compensations column, '
      + 'then use <b>Approve</b>, <b>Reject</b>, or <b>Ask</b> in Actions.';
    el.style.display = 'block';
    return;
  }
  const active = workflows.find((w) => !['resolved', 'failed'].includes(w.status));
  if (active) {
    el.innerHTML = `<b>Live run:</b> Latest activity is <b>${esc(active.status)}</b> — ${esc(STATUS_HELP[active.status] || 'updating…')}.`;
    el.style.display = 'block';
    return;
  }
  el.style.display = 'none';
}

function upsertRow(w) {
  document.getElementById('empty-row')?.remove();
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
      updatePhaseHint(data.workflows);
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
    const scenario = document.getElementById('scenario-select').value;
    const passcode = document.getElementById('demo-passcode').value.trim();
    if (demoConfig.passcode_required && !passcode) {
      showError('Enter the demo passcode before triggering.');
      return;
    }
    const payload = { scenario, passcode };
    const r = await fetch('/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
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

function openAskModal(workflowId) {
  askWorkflowId = workflowId;
  document.getElementById('ask-input').value = '';
  document.getElementById('ask-backdrop').classList.add('open');
  document.getElementById('ask-input').focus();
}

function closeAskModal() {
  askWorkflowId = null;
  document.getElementById('ask-backdrop').classList.remove('open');
}

async function submitAskModal() {
  const workflowId = askWorkflowId;
  const question = document.getElementById('ask-input').value.trim();
  if (!workflowId || !question) return;
  const btn = document.getElementById('ask-submit');
  btn.disabled = true;
  try {
    const r = await fetch(`/signal/${workflowId}/request_more_info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    if (!r.ok) {
      const b = await r.json();
      showError(b.detail || 'Could not send question');
      return;
    }
    closeAskModal();
    showSuccess(
      'Question sent. Watch this row: status → analyzing, then remediating with your question under Root cause.',
    );
  } catch (e) {
    showError(String(e));
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('ask-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'ask-backdrop') closeAskModal();
});

function renderNarrativeSections(sections) {
  const order = [
    ['what_happened', 'What happened'],
    ['how_solved', 'How it was solved'],
    ['real_world', 'Why it matters in production'],
  ];
  return order.map(([key, title]) => (
    `<div class="sec"><h4>${title}</h4><p>${esc(sections[key] || '')}</p></div>`
  )).join('');
}

function openNarrativeModal(workflowId) {
  narrativeWorkflowId = workflowId;
  document.getElementById('narrative-meta').textContent = workflowId;
  document.getElementById('narrative-body').innerHTML = '<div class="narrative-loading">Generating summary…</div>';
  document.getElementById('narrative-backdrop').classList.add('open');
  loadNarrative(false);
}

function closeNarrativeModal() {
  narrativeWorkflowId = null;
  document.getElementById('narrative-backdrop').classList.remove('open');
}

async function loadNarrative(refresh) {
  const workflowId = narrativeWorkflowId;
  if (!workflowId) return;
  const refreshBtn = document.getElementById('narrative-refresh');
  refreshBtn.disabled = true;
  try {
    const url = `/narrative/${encodeURIComponent(workflowId)}${refresh ? '?refresh=true' : ''}`;
    const r = await fetch(url, { method: 'POST' });
    const body = await r.json();
    if (!r.ok) {
      showError(body.detail || 'Could not generate explainer');
      closeNarrativeModal();
      return;
    }
    const src = body.source === 'openai' ? 'Summary generated with OpenAI from this incident record.'
      : 'Template summary (set OPENAI_API_KEY on the dashboard for an AI-written narrative).';
    document.getElementById('narrative-meta').textContent = `${workflowId} · ${src}`;
    document.getElementById('narrative-body').innerHTML = renderNarrativeSections(body.sections || {});
  } catch (e) {
    showError(String(e));
    closeNarrativeModal();
  } finally {
    refreshBtn.disabled = false;
  }
}

function refreshNarrative() {
  document.getElementById('narrative-body').innerHTML = '<div class="narrative-loading">Regenerating…</div>';
  loadNarrative(true);
}

document.getElementById('narrative-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'narrative-backdrop') closeNarrativeModal();
});

function showError(msg) {
  document.getElementById('success-banner').style.display = 'none';
  const b = document.getElementById('error-banner');
  b.textContent = '⚠ ' + msg;
  b.style.display = 'block';
  setTimeout(() => { b.style.display = 'none'; }, 6000);
}

function showSuccess(msg) {
  document.getElementById('error-banner').style.display = 'none';
  const b = document.getElementById('success-banner');
  b.textContent = '✓ ' + msg;
  b.style.display = 'block';
  setTimeout(() => { b.style.display = 'none'; }, 10000);
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """Public marketing landing page (see sre_swarm/landing/)."""
    return HTMLResponse(LANDING_HTML)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Live incident console."""
    return HTMLResponse(_HTML)


@app.get("/console")
async def console_redirect() -> RedirectResponse:
    """Back-compat alias used in older docs."""
    return RedirectResponse(url="/dashboard", status_code=307)


# ---------------------------------------------------------------------------
# Post-incident narrative (OpenAI or template)
# ---------------------------------------------------------------------------

async def _query_workflow_status(workflow_id: str) -> dict:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    return await handle.query("get_status")


@app.get("/demo/config")
async def demo_config() -> dict:
    from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
        DEMO_ASK_MAX_PER_IP_HOUR,
        DEMO_NARRATIVE_MAX_PER_IP_HOUR,
        DEMO_TRIGGER_MAX_PER_IP_HOUR,
        DEMO_TRIGGER_PASSCODE,
        OPENAI_DAILY_NARRATIVE_MAX,
        OPENAI_DAILY_RCA_MAX,
    )

    return {
        "passcode_required": bool(DEMO_TRIGGER_PASSCODE),
        "limits": {
            "trigger_per_ip_hour": DEMO_TRIGGER_MAX_PER_IP_HOUR,
            "narrative_per_ip_hour": DEMO_NARRATIVE_MAX_PER_IP_HOUR,
            "ask_per_ip_hour": DEMO_ASK_MAX_PER_IP_HOUR,
            "openai_rca_per_day": OPENAI_DAILY_RCA_MAX,
            "openai_narrative_per_day": OPENAI_DAILY_NARRATIVE_MAX,
        },
    }


@app.post("/narrative/{workflow_id}")
async def incident_narrative(
    workflow_id: str,
    request: Request,
    refresh: bool = False,
) -> dict:
    from fastapi import HTTPException  # noqa: PLC0415

    from sre_swarm.dashboard.narrative import produce_narrative  # noqa: PLC0415
    from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
        DEMO_NARRATIVE_MAX_PER_IP_HOUR,
        check_http_rate_limit,
    )

    ip = _client_ip(request)
    try:
        check_http_rate_limit(
            f"narrative:ip:{ip}",
            max_calls=DEMO_NARRATIVE_MAX_PER_IP_HOUR,
            window_seconds=3600,
        )
        status = await _query_workflow_status(workflow_id)
        return produce_narrative(workflow_id, status, refresh=refresh)
    except ValueError as exc:
        msg = str(exc)
        code = 429 if "rate limit" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    except Exception as exc:
        logger.exception("Narrative generation failed for %s", workflow_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
                    "affected_service": status_data.get("affected_service") or _parse_service(wf_id),
                    "alert_summary": status_data.get("alert_summary"),
                    "status": str(status_data.get("status", "")).split(".")[-1].lower(),
                    "root_cause": status_data.get("root_cause"),
                    "rollback_approved": status_data.get("rollback_approved", False),
                    "resolution_notes": status_data.get("resolution_notes", ""),
                    "confidence": status_data.get("confidence"),
                    "auto_approved": status_data.get("auto_approved", False),
                    "compensation_count": status_data.get("compensation_count"),
                    "compensations": status_data.get("compensations", []),
                    "compensation_results": status_data.get("compensation_results", []),
                    "verification": status_data.get("verification") or {},
                    "extra_context": status_data.get("extra_context") or [],
                    "error_spans": status_data.get("error_spans") or [],
                })
            except Exception as e:
                logger.debug("Could not query workflow %s: %s", wf_id, e)
                results.append({
                    "workflow_id": wf_id,
                    "incident_id": wf_id.replace("incident-", ""),
                    "affected_service": _parse_service(wf_id),
                    "alert_summary": None,
                    "status": str(execution.status).split(".")[-1].lower(),
                    "root_cause": None,
                    "rollback_approved": False,
                    "resolution_notes": "",
                    "confidence": None,
                    "auto_approved": False,
                    "compensation_count": None,
                    "compensation_results": [],
                    "verification": {},
                    "extra_context": [],
                    "error_spans": [],
                })
        return results
    except Exception as e:
        logger.warning("Failed to list workflows: %s", e)
        return []


def _parse_service(wf_id: str) -> str:
    """
    Best-effort: extract service name from workflow ID.
    Falls back to 'unknown' — the real value comes from the workflow query.
    """
    return "unknown"


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

@app.get("/scenarios")
async def list_demo_scenarios() -> list[dict]:
    from sre_swarm.incidents.scenarios import list_scenarios  # noqa: PLC0415

    return list_scenarios()


@app.post("/trigger")
async def trigger_incident(request: Request, body: dict | None = None) -> dict:
    from sre_swarm.incidents.scenarios import DEFAULT_SCENARIO_ID  # noqa: PLC0415
    from sre_swarm.scripts.trigger_incident import seed_and_build_incident  # noqa: PLC0415
    from sre_swarm.workflows.incident_response import IncidentResponseWorkflow  # noqa: PLC0415
    from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
        DEMO_TRIGGER_MAX_GLOBAL_DAY,
        DEMO_TRIGGER_MAX_PER_IP_HOUR,
        check_http_rate_limit,
        verify_trigger_passcode,
    )
    from fastapi import HTTPException  # noqa: PLC0415

    payload = body or {}
    scenario_id = str(payload.get("scenario") or DEFAULT_SCENARIO_ID)
    ip = _client_ip(request)

    try:
        verify_trigger_passcode(payload.get("passcode"))
        check_http_rate_limit(
            f"trigger:ip:{ip}",
            max_calls=DEMO_TRIGGER_MAX_PER_IP_HOUR,
            window_seconds=3600,
        )
        check_http_rate_limit(
            "trigger:global",
            max_calls=DEMO_TRIGGER_MAX_GLOBAL_DAY,
            window_seconds=86_400,
        )
        client = await _get_client()
        incident = await seed_and_build_incident(scenario_id)
        handle = await client.start_workflow(
            IncidentResponseWorkflow.run,
            incident,
            id=f"incident-{incident.incident_id}",
            task_queue=TASK_QUEUE,
        )
        logger.info("Triggered workflow id=%s scenario=%s", handle.id, scenario_id)
        return {
            "workflow_id": handle.id,
            "incident_id": incident.incident_id,
            "scenario": scenario_id,
            "affected_service": incident.affected_service,
        }
    except ValueError as e:
        msg = str(e)
        code = 429 if "rate limit" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=msg) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Webhook intake — accepts PagerDuty and Alertmanager payloads
# ---------------------------------------------------------------------------

def _parse_pagerduty(body: dict) -> dict:
    """
    Extract incident fields from a PagerDuty webhook v3 payload.
    https://developer.pagerduty.com/docs/ZG9jOjQ1MTg4ODQ0-overview

    PagerDuty sends a list of events under body["messages"] or body["event"].
    We extract the first trigger event we find.
    """
    # v3 format: {"messages": [{"event": "incident.trigger", "incident": {...}}]}
    messages = body.get("messages", [])
    if not messages and "event" in body:
        # some integrations send a single event at the top level
        messages = [body]

    for msg in messages:
        if msg.get("event") not in ("incident.trigger", "incident.triggered"):
            continue
        incident_data = msg.get("incident", msg.get("data", {}).get("incident", {}))
        service_name = (
            incident_data.get("service", {}).get("summary")
            or incident_data.get("service", {}).get("name")
            or "unknown-service"
        )
        title = incident_data.get("title") or incident_data.get("description") or "PagerDuty alert"
        return {
            "affected_service": service_name,
            "alert_summary": title,
            "source": "pagerduty",
        }
    return {}


def _parse_alertmanager(body: dict) -> dict:
    """
    Extract incident fields from an Alertmanager webhook payload.
    https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

    Alertmanager sends: {"alerts": [{"labels": {...}, "annotations": {...}}], ...}
    """
    alerts = body.get("alerts", [])
    if not alerts:
        return {}

    # Use the first firing alert
    alert = alerts[0]
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    service_name = (
        labels.get("service")
        or labels.get("job")
        or labels.get("app")
        or "unknown-service"
    )
    summary = (
        annotations.get("summary")
        or annotations.get("description")
        or annotations.get("message")
        or labels.get("alertname", "Alertmanager alert")
    )
    return {
        "affected_service": service_name,
        "alert_summary": summary,
        "source": "alertmanager",
    }


@app.post("/incident")
async def intake_incident(request: Request) -> dict:
    """
    Webhook intake endpoint.  Accepts:
      - PagerDuty webhook v3 payload
      - Alertmanager webhook payload
      - Raw AegisSRE IncidentInput JSON (pass-through)

    Detects the format automatically and starts an IncidentResponseWorkflow.
    """
    from sre_swarm.workflows.incident_response import IncidentInput, IncidentResponseWorkflow  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        from fastapi import HTTPException  # noqa: PLC0415
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    # ------------------------------------------------------------------
    # Detect payload format
    # ------------------------------------------------------------------
    parsed: dict = {}

    if "incident_id" in body and "affected_service" in body:
        # Native IncidentInput — pass straight through
        parsed = {
            "affected_service": body["affected_service"],
            "alert_summary": body.get("alert_summary", "Manual trigger"),
            "trace_ids": body.get("trace_ids", []),
        }
    elif "messages" in body or (
        "event" in body and "incident" in body.get("event", "") or "incident" in body
    ):
        # PagerDuty
        parsed = _parse_pagerduty(body)
    elif "alerts" in body:
        # Alertmanager
        parsed = _parse_alertmanager(body)

    if not parsed:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unrecognised payload format. "
                "Expected PagerDuty v3, Alertmanager, or native IncidentInput JSON."
            ),
        )

    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    incident = IncidentInput(
        incident_id=incident_id,
        affected_service=parsed["affected_service"],
        alert_summary=parsed["alert_summary"],
        trace_ids=parsed.get("trace_ids", []),
    )

    try:
        client = await _get_client()
        handle = await client.start_workflow(
            IncidentResponseWorkflow.run,
            incident,
            id=f"incident-{incident.incident_id}",
            task_queue=TASK_QUEUE,
        )
        logger.info(
            "Webhook intake: started workflow id=%s source=%s service=%s",
            handle.id, parsed.get("source", "native"), incident.affected_service,
        )
        return {
            "workflow_id": handle.id,
            "incident_id": incident.incident_id,
            "affected_service": incident.affected_service,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Approve / reject / request_more_info signals
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


@app.post("/signal/{workflow_id}/request_more_info")
async def request_more_info(
    workflow_id: str,
    request: Request,
    body: dict = {},
) -> dict:
    from sre_swarm.workflows.incident_response import IncidentResponseWorkflow  # noqa: PLC0415
    from sre_swarm.demo.spend_guard import (  # noqa: PLC0415
        DEMO_ASK_MAX_PER_IP_HOUR,
        check_http_rate_limit,
    )
    from fastapi import HTTPException  # noqa: PLC0415
    try:
        check_http_rate_limit(
            f"ask:ip:{_client_ip(request)}",
            max_calls=DEMO_ASK_MAX_PER_IP_HOUR,
            window_seconds=3600,
        )
        client = await _get_client()
        handle = client.get_workflow_handle(workflow_id)
        question: str = body.get("question", "")
        if not question:
            raise HTTPException(status_code=422, detail="question is required")
        await handle.signal(IncidentResponseWorkflow.request_more_info, question)
        return {"ok": True}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
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
