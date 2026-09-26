"""
Mock Microservices
==================
Simulates the order and payment services that the Saga compensation steps
call during an incident rollback.

Endpoints:
  POST /placeOrder       — create an order (simulates payment failure)
  POST /cancelOrder      — compensating transaction: cancel an order
  POST /chargePayment    — charge a payment
  POST /refundPayment    — compensating transaction: refund a payment
  GET  /orders/{id}      — inspect order state
  GET  /payments/{id}    — inspect payment state

Run with:
    python -m sre_swarm.mock_services.app

Environment variables:
    MOCK_SERVICES_PORT    Port to listen on (default: 9090)
    PAYMENT_FAILURE_RATE  Float 0.0–1.0, chance /placeOrder triggers a 500 (default: 0.3)
"""

from __future__ import annotations

import logging
import os
import random
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

load_dotenv()

PAYMENT_FAILURE_RATE: float = float(os.getenv("PAYMENT_FAILURE_RATE", "0.3"))
PORT: int = int(os.getenv("MOCK_SERVICES_PORT", "9090"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock Microservices", version="0.1.0")

# ---------------------------------------------------------------------------
# In-memory stores  (dict is fine for a POC)
# ---------------------------------------------------------------------------

orders: dict[str, dict[str, Any]] = {}
payments: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class PlaceOrderRequest(BaseModel):
    order_id: str
    items: list[str]
    customer_id: str


class CancelOrderRequest(BaseModel):
    order_id: str


class ChargePaymentRequest(BaseModel):
    payment_id: str
    amount: float
    customer_id: str


class RefundPaymentRequest(BaseModel):
    payment_id: str


# ---------------------------------------------------------------------------
# Order endpoints
# ---------------------------------------------------------------------------

@app.post("/placeOrder", status_code=201)
def place_order(req: PlaceOrderRequest) -> dict:
    """
    Create a new order with status 'pending'.

    Simulates an occasional payment-step failure controlled by
    PAYMENT_FAILURE_RATE (default 30%).  On failure the order is stored
    with status 'payment_failed' and a 500 is returned so callers can
    observe a partial saga commit.

    Idempotent: re-posting the same order_id returns the existing record
    with a 200 instead of creating a duplicate.
    """
    if req.order_id in orders:
        logger.info("placeOrder: duplicate order_id=%s, returning existing record", req.order_id)
        return orders[req.order_id]

    order: dict[str, Any] = {
        "order_id": req.order_id,
        "items": req.items,
        "customer_id": req.customer_id,
        "status": "pending",
    }

    if random.random() < PAYMENT_FAILURE_RATE:
        order["status"] = "payment_failed"
        orders[req.order_id] = order
        logger.warning("placeOrder: simulated payment failure for order_id=%s", req.order_id)
        raise HTTPException(status_code=500, detail={"error": "payment_failed", "order": order})

    orders[req.order_id] = order
    logger.info("placeOrder: created order_id=%s", req.order_id)
    return order


@app.post("/cancelOrder")
def cancel_order(req: CancelOrderRequest) -> dict:
    """
    Compensating transaction: set order status to 'cancelled'.

    Idempotent: returns 409 if the order is already cancelled so the Saga
    activity can treat it as a successful no-op.
    Raises 404 if the order does not exist.
    """
    order = orders.get(req.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"order {req.order_id!r} not found")

    if order["status"] == "cancelled":
        logger.info("cancelOrder: order_id=%s already cancelled", req.order_id)
        raise HTTPException(status_code=409, detail=f"order {req.order_id!r} already cancelled")

    order["status"] = "cancelled"
    logger.info("cancelOrder: cancelled order_id=%s", req.order_id)
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    """Read-only status endpoint for manual inspection."""
    order = orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"order {order_id!r} not found")
    return order


# ---------------------------------------------------------------------------
# Payment endpoints
# ---------------------------------------------------------------------------

@app.post("/chargePayment", status_code=201)
def charge_payment(req: ChargePaymentRequest) -> dict:
    """
    Record a payment charge with status 'charged'.

    Idempotent: re-posting the same payment_id returns the existing record
    with a 200 instead of creating a duplicate charge.
    """
    if req.payment_id in payments:
        logger.info("chargePayment: duplicate payment_id=%s, returning existing record", req.payment_id)
        return payments[req.payment_id]

    payment: dict[str, Any] = {
        "payment_id": req.payment_id,
        "amount": req.amount,
        "customer_id": req.customer_id,
        "status": "charged",
    }
    payments[req.payment_id] = payment
    logger.info("chargePayment: charged payment_id=%s amount=%.2f", req.payment_id, req.amount)
    return payment


@app.post("/refundPayment")
def refund_payment(req: RefundPaymentRequest) -> dict:
    """
    Compensating transaction: set payment status to 'refunded'.

    Idempotent: returns 409 if the payment is already refunded so the Saga
    activity can treat it as a successful no-op.
    Raises 404 if the payment does not exist.
    """
    payment = payments.get(req.payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {req.payment_id!r} not found")

    if payment["status"] == "refunded":
        logger.info("refundPayment: payment_id=%s already refunded", req.payment_id)
        raise HTTPException(status_code=409, detail=f"payment {req.payment_id!r} already refunded")

    payment["status"] = "refunded"
    logger.info("refundPayment: refunded payment_id=%s", req.payment_id)
    return payment


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str) -> dict:
    """Read-only status endpoint for manual inspection."""
    payment = payments.get(payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {payment_id!r} not found")
    return payment


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("sre_swarm.mock_services.app:app", host="0.0.0.0", port=PORT, reload=False)
