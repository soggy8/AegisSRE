# Telemetry Pipeline — Area B

Mock eBPF + OpenTelemetry span ingestion for the AegisSRE system.
This package is **standalone** — no Docker, no Temporal, no network required to run or test it.

---

## What's in here

```
sre_swarm/telemetry/
├── pipeline.py          ← Public API — call get_spans() from here
├── fixtures/
│   ├── happy_path.json       — all services healthy, low latency
│   ├── payment_timeout.json  — payment-service 500 + 4700 ms latency
│   └── cascade_failure.json  — all four services failing across 3 traces
└── TELEMETRY.md         ← you are here
```

---

## Public API

```python
from sre_swarm.telemetry.pipeline import get_spans, CPU_OVERHEAD_PCT

spans = get_spans(trace_ids=["trace-001"], service="payment-service")
# returns list[dict] — see span shape below
```

### `get_spans(trace_ids, service) -> list[dict]`

| Param | Type | Description |
|---|---|---|
| `trace_ids` | `list[str]` | Trace IDs to fetch spans for |
| `service` | `str` | Primary service under investigation |

**Returns** a list of span dicts shaped as:

```json
{
  "trace_id":    "abc123",
  "span_id":     "def456",
  "service":     "payment-service",
  "status_code": 500,
  "latency_ms":  4500,
  "error":       true,
  "timestamp":   "2025-01-01T12:00:00Z"
}
```

### `CPU_OVERHEAD_PCT`

Float constant (`2.4`) representing the simulated eBPF capture overhead.
The MCP server must include this in its `get_telemetry_context` response.

---

## Fixture vs dynamic mode

The function picks the right mode automatically based on the trace IDs passed in:

| Trace ID contains… | Mode | Fixture loaded |
|---|---|---|
| `"happy"` | Fixture | `happy_path.json` |
| `"timeout"` | Fixture | `payment_timeout.json` |
| `"cascade"` | Fixture | `cascade_failure.json` |
| anything else | Dynamic | Spans generated on the fly |

**Dynamic mode** generates one span per known service (`api-gateway`, `order-service`,
`payment-service`, `inventory-service`) per trace ID. The requested `service` always
gets an error injected; other services have a 20% cascade-failure chance.

---

## Quick smoke test (no Docker needed)

```bash
source .venv/bin/activate

python -c "
from sre_swarm.telemetry.pipeline import get_spans, CPU_OVERHEAD_PCT
import json

# Fixture mode
spans = get_spans(['trace-timeout-001'], 'payment-service')
print(json.dumps(spans, indent=2))

# Dynamic mode
spans = get_spans(['trace-001', 'trace-002'], 'payment-service')
for s in spans:
    print(s['service'], s['status_code'], s['error'])

print('CPU overhead:', CPU_OVERHEAD_PCT)
"
```

---

## How to connect to the MCP server (Area A)

The MCP server (built in `sre_swarm/mcp/server.py`) has a tool handler called
`get_telemetry_context`. That handler is the **only place** that calls into this package.

### What the MCP server team needs to add

In `sre_swarm/mcp/server.py`, inside the `get_telemetry_context` handler:

```python
from sre_swarm.telemetry.pipeline import get_spans, CPU_OVERHEAD_PCT

# arguments come from the incoming POST /mcp request body:
# { "tool": "get_telemetry_context", "arguments": { "trace_ids": [...], "service": "..." } }

spans = get_spans(
    trace_ids=arguments["trace_ids"],
    service=arguments["service"],
)

return {
    "spans": spans,
    "cpu_overhead_pct": CPU_OVERHEAD_PCT,
}
```

That's the complete integration. No session, no state — every call is self-contained.

### What the MCP server receives from the Temporal workflow

The workflow calls the MCP tool via the `call_mcp_tool` Activity in
`sre_swarm/activities/mcp_tools.py`. It sends this JSON body:

```json
{
  "tool": "get_telemetry_context",
  "arguments": {
    "trace_ids": ["trace-001", "trace-002"],
    "service": "payment-service"
  }
}
```

With these headers:
```
Mcp-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Authorization: Bearer <MCP_API_KEY>
Content-Type: application/json
```

The MCP server must return JSON that includes `spans` (list) and
`cpu_overhead_pct` (float). The workflow will pass the full response
body into the next tool call (`analyze_root_cause`) as `telemetry_context`.

### Full data flow

```
IncidentResponseWorkflow
  └─ execute_activity(call_mcp_tool, "get_telemetry_context")
       └─ POST http://localhost:8080/mcp
            └─ MCP server: get_telemetry_context handler
                 └─ get_spans(trace_ids, service)   ← this package
                      └─ returns spans list
            └─ MCP server returns { spans, cpu_overhead_pct }
       └─ MCPToolResult.output = { spans, cpu_overhead_pct }
  └─ execute_activity(call_mcp_tool, "analyze_root_cause")
       arguments = { alert_summary, telemetry_context: { spans, cpu_overhead_pct } }
```

---

## Environment variables

This package reads no environment variables. The MCP server URL and API key
are handled by `sre_swarm/activities/mcp_tools.py`:

```ini
MCP_SERVER_URL=http://localhost:8080
MCP_API_KEY=dev-key
```

Add these to your `.env` file (copy from `.env.example` if it exists).
