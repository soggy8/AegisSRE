"""
Trigger a test incident workflow.

Usage:
    python -m sre_swarm.scripts.trigger_incident
"""

import asyncio
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from temporalio.client import Client

from sre_swarm.workflows.incident_response import IncidentInput, IncidentResponseWorkflow

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE    = "sre-swarm"


async def main() -> None:
    client = await Client.connect(TEMPORAL_HOST)

    incident = IncidentInput(
        incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
        affected_service="payment-service",
        alert_summary="HTTP 500 spike on /checkout — p99 latency > 4 s",
        trace_ids=["trace-001", "trace-002"],
    )

    handle = await client.start_workflow(
        IncidentResponseWorkflow.run,
        incident,
        id=f"incident-{incident.incident_id}",
        task_queue=TASK_QUEUE,
    )

    print(f"Started workflow  id={handle.id}  run_id={handle.result_run_id}")
    print("Send approval signal with:")
    print(f"  temporal workflow signal --workflow-id {handle.id} --name approve_rollback")


if __name__ == "__main__":
    asyncio.run(main())
