"""
Temporal Worker Entrypoint
==========================
Registers all Workflows and Activities with a Temporal Worker and starts
listening on the `sre-swarm` task queue.

Run with:
    python -m sre_swarm.worker
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from sre_swarm.workflows.incident_response import IncidentResponseWorkflow
from sre_swarm.activities.mcp_tools import call_mcp_tool
from sre_swarm.activities.saga import execute_compensating_transaction

TEMPORAL_HOST  = "localhost:7233"
TASK_QUEUE     = "sre-swarm"

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    client = await Client.connect(TEMPORAL_HOST)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentResponseWorkflow],
        activities=[call_mcp_tool, execute_compensating_transaction],
    )
    logging.info("Worker started on task queue '%s'. Waiting for workflows…", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
