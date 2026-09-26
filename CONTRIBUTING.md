# Contributing to SRE Swarm

Welcome. This document is everything you need to pick up a task and ship
code that fits cleanly with what's already here.

---

## Table of Contents

1. [System overview](#1-system-overview)
2. [Prerequisites](#2-prerequisites)
3. [Local setup](#3-local-setup)
4. [Project layout](#4-project-layout)
5. [The golden rule — Workflows vs Activities](#5-the-golden-rule--workflows-vs-activities)
6. [Work areas & task assignments](#6-work-areas--task-assignments)
7. [Environment variables](#7-environment-variables)
8. [Running the system end-to-end](#8-running-the-system-end-to-end)
9. [Coding conventions](#9-coding-conventions)
10. [Gotchas & known traps](#10-gotchas--known-traps)

---

## 1. System overview

```
Alert / Webhook
      │
      ▼
IncidentResponseWorkflow  (Temporal — deterministic loop)
      │
      ├─ Activity: call_mcp_tool("get_telemetry_context")
      │       └─ hits MCP server → telemetry pipeline → eBPF mock spans
      │
      ├─ Activity: call_mcp_tool("analyze_root_cause")
      │       └─ hits MCP server → LLM inference → compensation plan
      │
      ├─ [waits for operator Signal: approve_rollback]
      │
      └─ Activity: execute_compensating_transaction  ×N  (Saga fan-out)
              └─ hits mock microservices → /cancelOrder, /refundPayment …
      └─ Activity: verify_service_state
              └─ re-reads orders and payments; resolves only if they match the plan
```

High-confidence plans (confidence ≥ 0.95) run that rollback without waiting.
Lower confidence waits for an operator signal from the dashboard. A follow-up
question is sent back into `analyze_root_cause` as `extra_context`.

---

## 2. Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| Python | 3.11 | `pyenv install 3.11` |
| Docker | any recent | docker.com |
| Temporal CLI | latest | `brew install temporal` or [docs](https://docs.temporal.io/cli) |

---

## 3. Local setup

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd ibmbobhackathon

# 2. Create a virtualenv and install deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Copy env template and fill in values
cp .env.example .env        # (create this file if it doesn't exist yet — see §7)

# 4. Start everything (Temporal, mock services, MCP, worker, dashboard)
docker compose up --build
# Dashboard → http://localhost:7080
# Temporal UI → http://localhost:8080

# Or start each process yourself — see §8.
```

---

## 4. Project layout

```
sre_swarm/
│
├── workflows/
│   └── incident_response.py   ← ✅ COMPLETE. Orchestration only. No I/O.
│
├── activities/
│   ├── mcp_tools.py           ← ✅ COMPLETE. HTTP calls to the MCP server.
│   ├── saga.py                ← ✅ COMPLETE. HTTP calls to mock services.
│   └── verify.py              ← ✅ COMPLETE. Re-reads order/payment state after rollback.
│
├── mcp/                       ← ✅ COMPLETE
│   └── server.py              ← get_telemetry_context + analyze_root_cause
│                                (OpenAI, watsonx, or heuristic). Follow-up
│                                questions arrive as arguments.extra_context.
│
├── dashboard/                 ← ✅ COMPLETE
│   └── app.py                 ← live UI, webhook intake, approve/reject/ask
│
├── telemetry/                 ← ✅ COMPLETE
│   ├── pipeline.py            ← get_spans() + CPU_OVERHEAD_PCT implemented
│   └── fixtures/              ← happy_path, payment_timeout, cascade_failure JSONs
│
├── mock_services/             ← ✅ COMPLETE
│   └── app.py                 ← all 6 endpoints implemented and tested
│
├── scripts/
│   └── trigger_incident.py    ← ✅ COMPLETE. Fires a test incident, useful for manual QA.
│
└── worker.py                  ← ✅ COMPLETE. Register new Activities here when you add them.
```

---

## 5. The golden rule — Workflows vs Activities

This is the most important rule in the codebase.

**Workflows are pure orchestration. They must be 100% deterministic.**

| ✅ Allowed in a Workflow | ❌ Never allowed in a Workflow |
|---|---|
| `workflow.execute_activity(...)` | `httpx.get(...)` |
| `workflow.wait_condition(...)` | `openai.chat.complete(...)` |
| `workflow.now()` | `datetime.now()` |
| `asyncio.gather(...)` over activities | `random.random()` |
| Pure data transformation | Any file / network / env I/O |

If you are unsure whether something is deterministic — put it in an Activity.
Temporal will retry your Activity automatically. It cannot safely retry I/O
inside a Workflow.

---

## 6. Work areas & task assignments

### Area A — MCP Server  `sre_swarm/mcp/`  ✅ COMPLETE

**Status:** `server.py` is a stateless FastAPI app on port 8081.

- `POST /mcp` validates `Mcp-Protocol-Version`, `Mcp-Method`, and `Authorization`
- `get_telemetry_context` calls `sre_swarm.telemetry.pipeline.get_spans`
- `analyze_root_cause` uses OpenAI, then watsonx, then a heuristic fallback
- Operator follow-up questions are read from `arguments.extra_context` and
  included in the LLM prompt. The heuristic appends them to `root_cause`.

`sre_swarm/activities/mcp_tools.py` performs the real HTTP call. There is no
stub path left.

---

### Area B — Telemetry Pipeline  `sre_swarm/telemetry/`  ✅ COMPLETE

**Status:** Fully implemented and tested.

- [`sre_swarm/telemetry/pipeline.py`](sre_swarm/telemetry/pipeline.py) — `get_spans(trace_ids, service)` and `CPU_OVERHEAD_PCT = 2.4`
- `fixtures/` — `happy_path.json`, `payment_timeout.json`, `cascade_failure.json`
- Fixture routing by keyword in trace_id; dynamic generation fallback

**No further work needed.** Wire-up to `get_telemetry_context` is already in
`sre_swarm/mcp/server.py`. The workflow calls that tool again after compensations
so the dashboard can show how many error spans remain on the original traces.
Health of the rollback is decided by order and payment status, not by those
historical spans.

---

### Area C — Mock Microservices  `sre_swarm/mock_services/`  ✅ COMPLETE

**Status:** Fully implemented and tested (13 test cases passing).

- [`sre_swarm/mock_services/app.py`](sre_swarm/mock_services/app.py) — FastAPI app on port 9090
- All 6 endpoints: `POST /placeOrder`, `POST /cancelOrder`, `POST /chargePayment`, `POST /refundPayment`, `GET /orders/{id}`, `GET /payments/{id}`
- All idempotency paths return correct status codes (201 new / 200 duplicate / 409 already done / 404 not found)
- Controlled failure injection via `PAYMENT_FAILURE_RATE` env var

**No further work needed.**

---

### Area D — Workflow behaviour  ✅ COMPLETE

The workflow in `sre_swarm/workflows/incident_response.py` does the following:

- Waits up to 30 minutes for an operator when confidence is below **0.95**,
  and records a FAILED timeout instead of crashing.
- Auto-approves and runs the plan when confidence is **0.95 or higher**.
- `request_more_info(question)` re-runs `analyze_root_cause` with that
  question in `extra_context`.
- Records every compensation step. One failure does not drop the others.
  The incident stays in `COMPENSATING` and the dashboard shows which step failed.
- After the steps, `verify_service_state` reads `/orders/{id}` and
  `/payments/{id}`. An order that is still open, or a payment that is still
  charged, keeps the incident in `COMPENSATING`. It is marked `RESOLVED`
  only when every step succeeded and those resources match the plan.

```
[x] Approval timeout is caught and returned as FAILED.
[x] request_more_info re-enters analysis with the operator's question.
[x] Per-step compensation results are stored on the workflow query.
[x] Post-rollback verification gates RESOLVED.
[x] tests/test_incident_response.py covers happy, approval, rejection,
    timeout, follow-up, auto-approve, partial failure, and failed verification.
[x] tests/test_mcp_server.py, tests/test_dashboard.py, tests/test_verify.py
```

---

## 7. Environment variables

Create a `.env` file at the repo root (never commit it). Here is the full
variable list — fill in what your area needs:

```ini
# Temporal
TEMPORAL_HOST=localhost:7233

# MCP Server (Area A)
MCP_SERVER_URL=http://localhost:8081
MCP_API_KEY=dev-key

# LLM (Area A — analyze_root_cause)
WATSONX_API_KEY=
WATSONX_PROJECT_ID=
# or if using OpenAI:
OPENAI_API_KEY=

# Dashboard
DASHBOARD_PORT=7080

# Mock Services (Area C)
MOCK_SERVICES_URL=http://localhost:9090
PAYMENT_FAILURE_RATE=0.3   # 30% chance of simulated payment failure
```

Load these in code with:
```python
from dotenv import load_dotenv
load_dotenv()
```

---

## 8. Running the system end-to-end

From the repo root:

```bash
docker compose up --build
```

Then open the dashboard at `http://localhost:7080` and click **Trigger incident**.
Temporal's own UI stays on `http://localhost:8080`.

Plans with confidence below 0.95 wait on the dashboard for Approve, Reject, or
Ask. Higher confidence runs the rollback immediately. Either way, the incident
stays in `compensating` until order and payment state match the plan.

To run the processes yourself instead:

```
Terminal 1 — Temporal server
  docker run --rm -p 7233:7233 -p 8080:8080 temporalio/temporal:latest server start-dev --ip 0.0.0.0 --ui-port 8080

Terminal 2 — Mock microservices
  python -m sre_swarm.mock_services.app

Terminal 3 — MCP server
  python -m sre_swarm.mcp.server

Terminal 4 — Temporal worker
  python -m sre_swarm.worker

Terminal 5 — Dashboard  → http://localhost:7080
  python -m sre_swarm.dashboard.app
```

---

## 9. Coding conventions

- **Types everywhere.** All function signatures must have type annotations.
- **Dataclasses for data transfer objects.** Use `@dataclass` (not raw dicts)
  for anything passed between a Workflow and an Activity.
- **Stubs before real code.** Keep the commented-out real implementation
  directly above the stub so teammates can un-comment it. Don't delete stubs
  until the real call is verified working.
- **Activity docstrings** must state: what the activity calls, what it
  returns, and what the retry behaviour is for each failure mode.
- **No print().** Use `activity.logger` inside Activities, `workflow.logger`
  inside Workflows, and the standard `logging` module everywhere else.

---

## 10. Gotchas & known traps

**`datetime.now()` in a Workflow will break replay.**
Use `workflow.now()` instead. Temporal's replay engine must produce the exact
same value it recorded the first time, and `datetime.now()` will differ on
replay.

**Importing a module that does I/O at import time inside a Workflow will crash the sandbox.**
Wrap all activity imports with `with workflow.unsafe.imports_passed_through():`.
This is already done in `incident_response.py` — follow that pattern.

**Activity retries are per-attempt, not per-workflow.**
If `call_mcp_tool` fails 3 times, the *workflow* pauses and waits for the
Activity to succeed. It does not fail the whole workflow. Design your
Activities to be idempotent and retry-safe.

**The MCP server must be stateless.**
Do not store session context between requests on the MCP server. Every
`POST /mcp` must be fully self-describing. Auth is passed via the
`Authorization` header on every single request.

**409 on compensation endpoints means already compensated — treat it as success.**
The Saga fan-out may execute the same compensation twice if the worker
crashes mid-execution. Your mock service endpoints must return 409 for
duplicate compensations, and `execute_compensating_transaction` already
handles that as a success.
