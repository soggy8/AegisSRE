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
```

Every box after the workflow is a stub today. Your job is to fill one of
those boxes in. See §6 for which box belongs to which work area.

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

# 4. Start Temporal (local dev server)
docker run --rm -p 7233:7233 temporalio/auto-setup:latest

# 5. Start the worker (separate terminal)
python -m sre_swarm.worker

# 6. Fire a test incident (separate terminal)
python -m sre_swarm.scripts.trigger_incident
```

After step 6 you should see the workflow appear in the Temporal Web UI at
`http://localhost:8080`.

---

## 4. Project layout

```
sre_swarm/
│
├── workflows/
│   └── incident_response.py   ← THE WORKFLOW. Orchestration only. No I/O.
│
├── activities/
│   ├── mcp_tools.py           ← stub HTTP call to MCP server
│   └── saga.py                ← stub compensating transaction HTTP calls
│
├── mcp/                       ← EMPTY — owner: MCP team
│   └── server.py              (to be created)
│
├── telemetry/                 ← EMPTY — owner: Telemetry team
│   └── pipeline.py            (to be created)
│
├── mock_services/             ← EMPTY — owner: Mock Services team
│   └── app.py                 (to be created)
│
├── scripts/
│   └── trigger_incident.py    ← fires a test incident, useful for manual QA
│
└── worker.py                  ← Temporal worker entrypoint; register new
                                  Activities here when you add them
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

### Area A — MCP Server  `sre_swarm/mcp/`

**What exists:** `sre_swarm/activities/mcp_tools.py` has a stub Activity with
the exact HTTP request shape commented out. The constants `MCP_SERVER_URL`,
`MCP_API_KEY`, and `MCP_PROTOCOL = "2026-07-28"` are already defined there.

**What to build:**

```
[ ] Create sre_swarm/mcp/server.py
      - FastAPI app, single POST route at /mcp
      - Read and validate headers: Mcp-Protocol-Version, Mcp-Method, Authorization
      - Route to tool handlers based on the "tool" field in the JSON body
      - No session state — every request must be fully self-describing

[ ] Implement tool handler: get_telemetry_context
      - Accepts: { trace_ids: list[str], service: str }
      - Calls the telemetry pipeline (sre_swarm/telemetry/) to get OTLP spans
      - Returns: { spans: [...], cpu_overhead_pct: float }

[ ] Implement tool handler: analyze_root_cause
      - Accepts: { alert_summary: str, telemetry_context: dict }
      - Calls an LLM (watsonx / OpenAI) with a structured prompt
      - Returns: { root_cause: str, confidence: float, compensations: [...] }
      - Each compensation must be: { endpoint: str, method: str, payload: dict }

[ ] Implement MCP Tasks extension for long-running tool calls
      - POST /mcp returns a task handle { task_id, status: "working" }
      - GET  /mcp/tasks/{task_id} returns current status
      - Handle states: working | input_required | completed | failed
      - input_required must block and wait for a human response payload

[ ] Add server startup to README Quick Start
[ ] Add MCP_SERVER_URL and MCP_API_KEY to .env.example

[ ] Un-comment the real HTTP block in sre_swarm/activities/mcp_tools.py
      and delete the stub outputs dict once the server responds correctly
```

**Key file to read first:** [`sre_swarm/activities/mcp_tools.py`](sre_swarm/activities/mcp_tools.py) — the commented-out block starting at line 79 is the exact request the server must accept.

---

### Area B — Telemetry Pipeline  `sre_swarm/telemetry/`

**What exists:** Empty package. The MCP `get_telemetry_context` tool handler
(Area A) will call into this package. The workflow passes `trace_ids` and
`affected_service`.

**What to build:**

```
[ ] Create sre_swarm/telemetry/pipeline.py
      - Function: get_spans(trace_ids: list[str], service: str) -> list[dict]
      - Returns OTLP-shaped span dicts (see structure below)

[ ] Implement mock eBPF data generation
      - Simulate network flow capture with ~2.4% CPU overhead annotation
      - Inject realistic error scenarios: HTTP 500, timeout (>4 s latency),
        connection reset, partial saga commit
      - Each span must include: trace_id, span_id, service, status_code,
        latency_ms, error bool, timestamp

[ ] Add a span fixture file: sre_swarm/telemetry/fixtures/
      - happy_path.json    — all services healthy
      - payment_timeout.json — payment-service 500, order partially committed
      - cascade_failure.json — three services failing in sequence

[ ] Wire pipeline.py into the MCP tool handler (coordinate with Area A team)
```

**Expected span shape** (match this exactly so Area A can deserialize it):
```json
{
  "trace_id":   "abc123",
  "span_id":    "def456",
  "service":    "payment-service",
  "status_code": 500,
  "latency_ms":  4500,
  "error":       true,
  "timestamp":   "2025-01-01T12:00:00Z"
}
```

---

### Area C — Mock Microservices  `sre_swarm/mock_services/`

**What exists:** Empty package. `sre_swarm/activities/saga.py` has the
compensating transaction HTTP call commented out at line 62. The constant
`MOCK_SERVICES_BASE_URL` defaults to `http://localhost:9090`.

**What to build:**

```
[ ] Create sre_swarm/mock_services/app.py
      - FastAPI app running on port 9090
      - Stateful in-memory order/payment store (dict is fine for a POC)

[ ] Implement POST /placeOrder
      - Accepts: { order_id, items, customer_id }
      - Persists order with status "pending"
      - Simulates occasional 500 on payment step (env flag: PAYMENT_FAILURE_RATE)

[ ] Implement POST /cancelOrder
      - Accepts: { order_id }
      - Sets order status to "cancelled" — idempotent (409 if already cancelled)

[ ] Implement POST /chargePayment
      - Accepts: { payment_id, amount, customer_id }
      - Simulates charge; returns payment_id

[ ] Implement POST /refundPayment
      - Accepts: { payment_id }
      - Sets payment status to "refunded" — idempotent (409 if already refunded)

[ ] Implement GET /orders/{order_id} and GET /payments/{payment_id}
      - Read-only status endpoints for manual inspection

[ ] Un-comment the real HTTP block in sre_swarm/activities/saga.py
      and delete the stub return once mock services respond correctly

[ ] Add MOCK_SERVICES_URL to .env.example
[ ] Add mock service startup to README Quick Start
```

---

### Area D — Workflow Enhancements  `sre_swarm/workflows/`

**What exists:** A complete working workflow at
[`sre_swarm/workflows/incident_response.py`](sre_swarm/workflows/incident_response.py).

**What to build:**

```
[ ] Add a new Signal: request_more_info(question: str)
      - Allows the operator to ask the agent a follow-up question mid-incident
      - The workflow should re-run analyze_root_cause with the additional context

[ ] Add workflow timeout guard
      - If the entire incident is not resolved within 2 hours, auto-escalate
      - Use workflow.execute_activity to call a notify_escalation Activity

[ ] Create sre_swarm/activities/notifications.py
      - Activity: notify_escalation(incident_id, root_cause, elapsed_minutes)
      - Stub: just log; TODO: wire to Slack / PagerDuty

[ ] Add the new activity to worker.py registration list

[ ] Write a test for the workflow using Temporal's test environment
      - File: tests/test_incident_response.py
      - Use temporalio.testing.WorkflowEnvironment to run deterministic tests
      - Test the happy path, the rejection path, and the approval-timeout path
```

---

## 7. Environment variables

Create a `.env` file at the repo root (never commit it). Here is the full
variable list — fill in what your area needs:

```ini
# Temporal
TEMPORAL_HOST=localhost:7233

# MCP Server (Area A)
MCP_SERVER_URL=http://localhost:8080
MCP_API_KEY=dev-key

# LLM (Area A — analyze_root_cause)
WATSONX_API_KEY=
WATSONX_PROJECT_ID=
# or if using OpenAI:
OPENAI_API_KEY=

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

Each component runs in its own terminal. Start them in this order:

```
Terminal 1 — Temporal server
  docker run --rm -p 7233:7233 -p 8080:8080 temporalio/auto-setup:latest

Terminal 2 — Mock microservices  (Area C)
  python -m sre_swarm.mock_services.app

Terminal 3 — MCP server  (Area A)
  python -m sre_swarm.mcp.server

Terminal 4 — Temporal worker
  python -m sre_swarm.worker

Terminal 5 — Fire a test incident
  python -m sre_swarm.scripts.trigger_incident

Terminal 5 — Approve the rollback plan (copy the workflow ID from Terminal 5 output)
  temporal workflow signal \
    --workflow-id <id-from-trigger-output> \
    --name approve_rollback
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
handles that as a success (see the commented stub in `saga.py`).
