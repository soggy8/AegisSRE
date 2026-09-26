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
    MOCK_DB_PATH          SQLite file path (default: mock_services.db). Use
                          ":memory:" in tests to avoid writing a real file.
    PAYMENT_FAILURE_RATE  Float 0.0–1.0, chance /placeOrder triggers a 500 (default: 0.3)
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
import uvicorn

load_dotenv()

PAYMENT_FAILURE_RATE: float = float(os.getenv("PAYMENT_FAILURE_RATE", "0.3"))
PORT: int = int(os.getenv("MOCK_SERVICES_PORT", "9090"))
DB_PATH: str = os.getenv("MOCK_DB_PATH", "mock_services.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock Microservices", version="0.1.0")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

# For :memory: databases every sqlite3.connect() call creates a brand-new
# empty DB, so we keep one persistent connection for that case.
_SHARED_CON: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _SHARED_CON
    if DB_PATH == ":memory:":
        if _SHARED_CON is None:
            _SHARED_CON = sqlite3.connect(":memory:", check_same_thread=False)
            _SHARED_CON.row_factory = sqlite3.Row
        return _SHARED_CON
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _init_db() -> None:
    """Create tables if they don't exist. Called once at module load."""
    con = _connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            items       TEXT NOT NULL,
            status      TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id  TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            amount      REAL NOT NULL,
            status      TEXT NOT NULL
        )
    """)
    con.commit()
    _release(con)


def _release(con: sqlite3.Connection) -> None:
    """Close the connection unless it is the shared :memory: connection."""
    if con is not _SHARED_CON:
        con.close()


_init_db()


def _get_order(order_id: str) -> dict[str, Any] | None:
    con = _connect()
    row = con.execute(
        "SELECT order_id, customer_id, items, status FROM orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    _release(con)
    if row is None:
        return None
    return {"order_id": row["order_id"], "customer_id": row["customer_id"],
            "items": json.loads(row["items"]), "status": row["status"]}


def _save_order(order: dict[str, Any]) -> None:
    con = _connect()
    con.execute(
        "INSERT OR REPLACE INTO orders (order_id, customer_id, items, status) VALUES (?,?,?,?)",
        (order["order_id"], order["customer_id"],
         json.dumps(order["items"]), order["status"]),
    )
    con.commit()
    _release(con)


def _update_order_status(order_id: str, status: str) -> None:
    con = _connect()
    con.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    con.commit()
    _release(con)


def _get_payment(payment_id: str) -> dict[str, Any] | None:
    con = _connect()
    row = con.execute(
        "SELECT payment_id, customer_id, amount, status FROM payments WHERE payment_id=?",
        (payment_id,),
    ).fetchone()
    _release(con)
    if row is None:
        return None
    return {"payment_id": row["payment_id"], "customer_id": row["customer_id"],
            "amount": row["amount"], "status": row["status"]}


def _save_payment(payment: dict[str, Any]) -> None:
    con = _connect()
    con.execute(
        "INSERT OR REPLACE INTO payments (payment_id, customer_id, amount, status) VALUES (?,?,?,?)",
        (payment["payment_id"], payment["customer_id"],
         payment["amount"], payment["status"]),
    )
    con.commit()
    _release(con)


def _update_payment_status(payment_id: str, status: str) -> None:
    con = _connect()
    con.execute("UPDATE payments SET status=? WHERE payment_id=?", (status, payment_id))
    con.commit()
    _release(con)


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
def place_order(req: PlaceOrderRequest, response: Response) -> dict:
    """
    Create a new order with status 'pending'.

    Simulates an occasional payment-step failure controlled by
    PAYMENT_FAILURE_RATE (default 30%).  On failure the order is stored
    with status 'payment_failed' and a 500 is returned so callers can
    observe a partial saga commit.

    Idempotent: re-posting the same order_id returns the existing record
    with a 200 instead of creating a duplicate.
    """
    existing = _get_order(req.order_id)
    if existing:
        logger.info("placeOrder: duplicate order_id=%s, returning existing record", req.order_id)
        response.status_code = 200
        return existing

    order: dict[str, Any] = {
        "order_id": req.order_id,
        "items": req.items,
        "customer_id": req.customer_id,
        "status": "pending",
    }

    if random.random() < PAYMENT_FAILURE_RATE:
        order["status"] = "payment_failed"
        _save_order(order)
        logger.warning("placeOrder: simulated payment failure for order_id=%s", req.order_id)
        raise HTTPException(status_code=500, detail={"error": "payment_failed", "order": order})

    _save_order(order)
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
    order = _get_order(req.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"order {req.order_id!r} not found")

    if order["status"] == "cancelled":
        logger.info("cancelOrder: order_id=%s already cancelled", req.order_id)
        raise HTTPException(status_code=409, detail=f"order {req.order_id!r} already cancelled")

    _update_order_status(req.order_id, "cancelled")
    order["status"] = "cancelled"
    logger.info("cancelOrder: cancelled order_id=%s", req.order_id)
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    """Read-only status endpoint for manual inspection."""
    order = _get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"order {order_id!r} not found")
    return order


# ---------------------------------------------------------------------------
# Payment endpoints
# ---------------------------------------------------------------------------

@app.post("/chargePayment", status_code=201)
def charge_payment(req: ChargePaymentRequest, response: Response) -> dict:
    """
    Record a payment charge with status 'charged'.

    Idempotent: re-posting the same payment_id returns the existing record
    with a 200 instead of creating a duplicate charge.
    """
    existing = _get_payment(req.payment_id)
    if existing:
        logger.info("chargePayment: duplicate payment_id=%s, returning existing record", req.payment_id)
        response.status_code = 200
        return existing

    payment: dict[str, Any] = {
        "payment_id": req.payment_id,
        "amount": req.amount,
        "customer_id": req.customer_id,
        "status": "charged",
    }
    _save_payment(payment)
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
    payment = _get_payment(req.payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {req.payment_id!r} not found")

    if payment["status"] == "refunded":
        logger.info("refundPayment: payment_id=%s already refunded", req.payment_id)
        raise HTTPException(status_code=409, detail=f"payment {req.payment_id!r} already refunded")

    _update_payment_status(req.payment_id, "refunded")
    payment["status"] = "refunded"
    logger.info("refundPayment: refunded payment_id=%s", req.payment_id)
    return payment


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str) -> dict:
    """Read-only status endpoint for manual inspection."""
    payment = _get_payment(payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {payment_id!r} not found")
    return payment


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("sre_swarm.mock_services.app:app", host="0.0.0.0", port=PORT, reload=False)
