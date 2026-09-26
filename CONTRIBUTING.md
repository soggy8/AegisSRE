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
│   └── incident_response.py   ← ✅ COMPLETE. Orchestration only. No I/O.
│
├── activities/
│   ├── mcp_tools.py           ← ✅ COMPLETE (stub). Real HTTP call commented out — un-comment once MCP server exists.
│   └── saga.py                ← ✅ COMPLETE. Makes real HTTP calls to mock services.
│
├── mcp/                       ← ❌ NOT STARTED — owner: MCP team
│   └── server.py              (to be created — see Area A below)
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

### Area A — MCP Server  `sre_swarm/mcp/`  ❌ NOT STARTED

**Status:** The `sre_swarm/mcp/` package exists but `server.py` has not been created.
This is the **primary blocker** for a live end-to-end run. The stub in `mcp_tools.py`
keeps the workflow runnable against Temporal in the meantime.

**What to build:**

```
[ ] Create sre_swarm/mcp/server.py
      - FastAPI app, single POST route at /mcp
      - Read and validate headers: Mcp-Protocol-Version, Mcp-Method, Authorization
      - Route to tool handlers based on the "tool" field in the JSON body
      - No session state — every request must be fully self-describing
      - Run with: python -m sre_swarm.mcp.server  (add __main__ block)

[ ] Implement tool handler: get_telemetry_context
      - Accepts: { trace_ids: list[str], service: str }
      - Import and call: from sre_swarm.telemetry.pipeline import get_spans, CPU_OVERHEAD_PCT
      - Returns: { spans: [...], cpu_overhead_pct: float }

[ ] Implement tool handler: analyze_root_cause
      - Accepts: { alert_summary: str, telemetry_context: dict }
      - Calls an LLM (watsonx / OpenAI) with a structured prompt
      - Returns: { root_cause: str, confidence: float, compensations: [...] }
      - Each compensation must be: { endpoint: str, method: str, payload: dict }
        (these map directly to CompensationRequest in sre_swarm/activities/saga.py)

[ ] (Optional) Implement MCP Tasks extension for long-running tool calls
      - POST /mcp returns a task handle { task_id, status: "working" }
      - GET  /mcp/tasks/{task_id} returns current status
      - Handle states: working | input_required | completed | failed

[ ] Un-comment the real HTTP block in sre_swarm/activities/mcp_tools.py
      (lines 79–96) and delete the stub_outputs dict once the server responds correctly
```

**Key file to read first:** [`sre_swarm/activities/mcp_tools.py`](sre_swarm/activities/mcp_tools.py) — the commented-out block at line 79 is the exact request shape the server must accept. [`sre_swarm/telemetry/TELEMETRY.md`](sre_swarm/telemetry/TELEMETRY.md) has the full integration guide for wiring `get_spans()` into the handler.

---

### Area B — Telemetry Pipeline  `sre_swarm/telemetry/`  ✅ COMPLETE

**Status:** Fully implemented and tested.

- [`sre_swarm/telemetry/pipeline.py`](sre_swarm/telemetry/pipeline.py) — `get_spans(trace_ids, service)` and `CPU_OVERHEAD_PCT = 2.4`
- `fixtures/` — `happy_path.json`, `payment_timeout.json`, `cascade_failure.json`
- Fixture routing by keyword in trace_id; dynamic generation fallback

**No further work needed.** Wire it into Area A's `get_telemetry_context` handler as described above.

---

### Area C — Mock Microservices  `sre_swarm/mock_services/`  ✅ COMPLETE

**Status:** Fully implemented and tested (13 test cases passing).

- [`sre_swarm/mock_services/app.py`](sre_swarm/mock_services/app.py) — FastAPI app on port 9090
- All 6 endpoints: `POST /placeOrder`, `POST /cancelOrder`, `POST /chargePayment`, `POST /refundPayment`, `GET /orders/{id}`, `GET /payments/{id}`
- All idempotency paths return correct status codes (201 new / 200 duplicate / 409 already done / 404 not found)
- Controlled failure injection via `PAYMENT_FAILURE_RATE` env var

**No further work needed.**

---

### Area D — Workflow & Infrastructure Gaps  ⚠️ BUGS + MISSING PIECES

**Status:** The base workflow logic is correct but there are real bugs and missing
wiring that must be fixed before any live run works reliably.

#### Bug: unhandled `asyncio.TimeoutError` on approval timeout

`workflow.wait_condition(..., timeout=timedelta(minutes=30))` raises
`asyncio.TimeoutError` if neither `approve_rollback` nor `reject_rollback` fires
within 30 minutes. There is no `try/except` around it, so the workflow will **crash**
instead of escalating gracefully.

```
[x] FIXED — wrapped wait_condition in try/except asyncio.TimeoutError in
    sre_swarm/workflows/incident_response.py. On timeout the workflow now
    sets status=FAILED and returns a resolution note instead of crashing.
```

#### ~~Bug: stale TODO comment in `saga.py`~~  ✅ FIXED

```
[x] FIXED — removed the misleading TODO from execute_compensating_transaction docstring.
    The real HTTP call to mock services was already present.
```

#### ~~Missing: `load_dotenv()` in worker and trigger script~~  ✅ FIXED

```
[x] FIXED — load_dotenv() added to sre_swarm/worker.py and
    sre_swarm/scripts/trigger_incident.py before any sre_swarm.* imports.
```

#### ~~Missing: `TEMPORAL_HOST` should be read from env~~  ✅ FIXED

```
[x] FIXED — both worker.py and trigger_incident.py now read:
      TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
```

#### Missing: tests

No `tests/` directory exists at all.

```
[ ] Create tests/test_incident_response.py
      - Use temporalio.testing.WorkflowEnvironment to run deterministic tests
      - Test: happy path (no compensations needed → RESOLVED immediately)
      - Test: approval path (compensations present → wait → approve → RESOLVED)
      - Test: rejection path (reject_rollback signal → FAILED with reason)
      - Test: timeout path (no signal within timeout → FAILED, not crash)

[ ] Create tests/test_mock_services.py  (optional — logic already manually verified)
[ ] Create tests/test_telemetry_pipeline.py  (optional — logic already manually verified)
```

#### Enhancement: `request_more_info` signal

```
[ ] Add Signal: request_more_info(question: str)
      - Re-runs analyze_root_cause with the additional context appended
      - The workflow should store the question and re-enter ANALYZING state
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
