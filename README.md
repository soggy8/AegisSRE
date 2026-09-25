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

# 2. Start a local Temporal server (requires Docker)
docker run --rm -p 7233:7233 temporalio/auto-setup:latest

# 3. Start the Temporal worker
python -m sre_swarm.worker

# 4. Start the MCP server
python -m sre_swarm.mcp.server

# 5. Trigger a test incident
python -m sre_swarm.scripts.trigger_incident
```

## Contributing

Each subsystem lives in its own package. Work can proceed independently:

- **`workflows/`** — pure Python, no I/O, only `workflow.*` SDK calls
- **`activities/`** — all side-effects live here; LLM calls, HTTP, mutations
- **`mcp/`** — stateless HTTP server; every request is self-describing
- **`telemetry/`** — mock eBPF ingestion and OTLP span generation

> **Rule:** Workflows must never perform I/O directly. All external calls go through Activities.
