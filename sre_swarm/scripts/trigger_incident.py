"""
Trigger a test incident workflow.

Seeds a real order and payment in the mock services first so the LLM has
real IDs to work with when building the compensation plan.

Usage:
    python -m sre_swarm.scripts.trigger_incident
    python -m sre_swarm.scripts.trigger_incident --scenario cascade_failure
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

from temporalio.client import Client

from sre_swarm.incidents.scenarios import DEFAULT_SCENARIO_ID, build_incident_input
from sre_swarm.workflows.incident_response import IncidentInput, IncidentResponseWorkflow

TEMPORAL_HOST     = os.getenv("TEMPORAL_HOST", "localhost:7233")
MOCK_SERVICES_URL = os.getenv("MOCK_SERVICES_URL", "http://localhost:9090")
TASK_QUEUE        = "sre-swarm"


async def _seed_order_and_payment() -> tuple[str, str]:
    order_id    = f"ord-{uuid.uuid4().hex[:8]}"
    payment_id  = f"pay-{uuid.uuid4().hex[:8]}"
    customer_id = "cust-demo-001"

    async with httpx.AsyncClient() as http:
        try:
            await http.post(
                f"{MOCK_SERVICES_URL}/placeOrder",
                json={
                    "order_id": order_id,
                    "items": ["item-a", "item-b"],
                    "customer_id": customer_id,
                },
                timeout=5.0,
            )
        except Exception:
            pass

        await http.post(
            f"{MOCK_SERVICES_URL}/chargePayment",
            json={
                "payment_id": payment_id,
                "amount": 99.99,
                "customer_id": customer_id,
            },
            timeout=5.0,
        )

    return order_id, payment_id


async def seed_and_build_incident(scenario_id: str = DEFAULT_SCENARIO_ID) -> IncidentInput:
    """
    Place a real order and charge a real payment in the mock services,
    then return an IncidentInput for the chosen demo scenario.
    """
    order_id, payment_id = await _seed_order_and_payment()
    print(f"Seeded  order_id={order_id}  payment_id={payment_id}  scenario={scenario_id}")

    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    return build_incident_input(
        scenario_id=scenario_id,
        incident_id=incident_id,
        order_id=order_id,
        payment_id=payment_id,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Trigger an AegisSRE demo incident")
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO_ID,
        help=f"Demo scenario id (default: {DEFAULT_SCENARIO_ID})",
    )
    args = parser.parse_args()

    incident = await seed_and_build_incident(args.scenario)

    client = await Client.connect(TEMPORAL_HOST)
    handle = await client.start_workflow(
        IncidentResponseWorkflow.run,
        incident,
        id=f"incident-{incident.incident_id}",
        task_queue=TASK_QUEUE,
    )

    print(f"Started workflow  id={handle.id}  run_id={handle.result_run_id}")


if __name__ == "__main__":
    asyncio.run(main())
