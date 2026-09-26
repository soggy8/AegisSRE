# Autonomous SRE Orchestration Swarm

A proof-of-concept for an agent-driven Site Reliability Engineering system that autonomously detects distributed microservice failures, identifies root cause, and dynamically orchestrates semantic rollbacks.

## Architecture Overview

```
sre_swarm/
├── workflows/       # Deterministic Temporal Workflow definitions
├── activities/      # Non-deterministic Activities (LLM calls, external I/O)
├── mcp/             # Stateless MCP server (2026-07-28 spec)
├── telemetry/       # Mock eBPF + OpenTelemetry ingestion pipeline
└── worker.py        # Temporal Worker entrypoint
```

## Key Design Pillars

| Pillar | Technology | Role |
|---|---|---|
| Durable Execution | [Temporal.io](https://temporal.io) | Fault-oblivious orchestration loop |
| Tool Calling | MCP (stateless 2026-07-28) | Agent ↔ infrastructure interface |
| Long-running Actions | MCP Tasks extension | Human-in-the-loop + async remediation |
| Observability | eBPF mock + OpenTelemetry | Ground-truth telemetry ingestion |
| Rollbacks | Saga pattern | Compensating transactions, no 2PC |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy env template and fill in values
cp .env.example .env

# 3. Start a local Temporal server (requires Docker)
docker run --rm -p 7233:7233 -p 8080:8080 temporalio/auto-setup:latest

# 4. Start the mock microservices (Terminal 2)
python -m sre_swarm.mock_services.app

# 5. Start the MCP server (Terminal 3)
python -m sre_swarm.mcp.server

# 6. Start the Temporal worker (Terminal 4)
python -m sre_swarm.worker

# 7. Trigger a test incident (Terminal 5)
python -m sre_swarm.scripts.trigger_incident

# 8. Approve the rollback plan — copy the workflow ID printed by step 7
temporal workflow signal --workflow-id <id> --name approve_rollback
```

## Contributing

Each subsystem lives in its own package. Work can proceed independently:

- **`workflows/`** — pure Python, no I/O, only `workflow.*` SDK calls
- **`activities/`** — all side-effects live here; LLM calls, HTTP, mutations
- **`mcp/`** — stateless HTTP server; every request is self-describing
- **`telemetry/`** — mock eBPF ingestion and OTLP span generation

> **Rule:** Workflows must never perform I/O directly. All external calls go through Activities.
