# Live demo script (~90 seconds)

Use this order when presenting **AegisSRE** to judges or reviewers.

## Before you start

```bash
cp .env.example .env
# Optional but recommended for richer RCA text:
#   OPENAI_API_KEY=sk-...
docker compose up --build
```

- Landing: http://localhost:7080  
- Live console: http://localhost:7080/dashboard  
- Temporal UI: http://localhost:8080  

**Note:** Compose runs Temporal with an **in-memory** store. Restarting the stack clears old incidents. If the table looks stale, refresh the page after `docker compose up`.

## Beat 1 — Trigger (15 s)

1. Open the live console at `/dashboard` (link on the landing page).
2. Pick an **Incident type** (payment timeout, cascade, gateway overload, inventory outage, or false alarm), then click **Trigger incident**.
3. Point out the row moving **detecting → analyzing → remediating**.
4. Call out **confidence %** and the proposed **compensation steps** (cancel order / refund payment).

Without an OpenAI key, the heuristic still builds a rollback plan from **order_id** and **payment_id** embedded in telemetry spans.

## Beat 2 — Human in the loop (20 s)

5. With confidence below 95%, the workflow **waits for you** — this is intentional safety, not a hang.
6. Click **Approve** (or **Ask** with a follow-up question, then approve after re-analysis).

## Beat 3 — Saga + verification (25 s)

7. Status moves to **compensating**. Each step shows ✓ or ✗.
8. When verification passes, status becomes **resolved** and resolution notes mention cancelled orders and refunded payments.
9. Optional: open Temporal UI and show the durable workflow history surviving retries.
10. On a **resolved** row, click **Explain** for a three-part narrative (what happened, how it was fixed, production value). Uses `OPENAI_API_KEY` on the dashboard service when set.

## Beat 4 — Why it is real (30 s)

10. One sentence: alert → Temporal workflow → MCP tools (telemetry + LLM/heuristic RCA) → saga compensations → **post-rollback verification** on mock services.
11. Mention tests: `pytest tests/` (no Docker required for the workflow unit tests).
12. Close with: similar agent-SRE concepts exist in slides and prototypes; **this repo runs end-to-end** with one Compose command.

## If something goes wrong

| Symptom | Fix |
|--------|-----|
| Stuck on **analyzing** | Check `docker compose logs mcp` — often missing `openai` package or bad API key. Rebuild: `docker compose up --build`. |
| Empty incident list after restart | Expected — Temporal dev data is in-memory. Trigger a new incident. |
| Worker cannot connect | Ensure `temporal` service is **healthy** (`docker compose ps`). |

## CLI alternative

```bash
python -m sre_swarm.scripts.trigger_incident
# Approve via dashboard or:
# temporal workflow signal --workflow-id incident-<id> --name approve_rollback
```
