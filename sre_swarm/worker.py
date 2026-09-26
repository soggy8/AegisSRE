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
import os

from dotenv import load_dotenv

load_dotenv()

from sre_swarm.workflows.incident_response import IncidentResponseWorkflow
from sre_swarm.activities.mcp_tools import call_mcp_tool
from sre_swarm.activities.saga import execute_compensating_transaction
from sre_swarm.activities.verify import verify_service_state

from temporalio.client import Client
from temporalio.worker import Worker

TEMPORAL_HOST  = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE     = "sre-swarm"

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    client = await Client.connect(TEMPORAL_HOST)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentResponseWorkflow],
        activities=[call_mcp_tool, execute_compensating_transaction, verify_service_state],
    )
    logging.info("Worker started on task queue '%s'. Waiting for workflows…", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
