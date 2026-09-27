"""
In-memory limits for a public hackathon demo (single Compose stack).

- HTTP rate limits on trigger / narrative / Ask (dashboard).
- Daily caps on OpenAI calls with automatic fallback (MCP + dashboard).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import date


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


# --- OpenAI daily caps (0 = unlimited) ------------------------------------

OPENAI_DAILY_RCA_MAX = _int_env("OPENAI_DAILY_RCA_MAX", 40)
OPENAI_DAILY_NARRATIVE_MAX = _int_env("OPENAI_DAILY_NARRATIVE_MAX", 25)

_rca_day: date | None = None
_rca_count = 0
_narrative_day: date | None = None
_narrative_count = 0


def _roll_day(counter_day: date | None, count: int) -> tuple[date, int]:
    today = date.today()
    if counter_day != today:
        return today, 0
    return counter_day, count


def rca_openai_allowed() -> bool:
    global _rca_day, _rca_count
    if not OPENAI_DAILY_RCA_MAX:
        return True
    _rca_day, _rca_count = _roll_day(_rca_day, _rca_count)
    return _rca_count < OPENAI_DAILY_RCA_MAX


def record_rca_openai_call() -> None:
    global _rca_day, _rca_count
    _rca_day, _rca_count = _roll_day(_rca_day, _rca_count)
    _rca_count += 1


def narrative_openai_allowed() -> bool:
    global _narrative_day, _narrative_count
    if not OPENAI_DAILY_NARRATIVE_MAX:
        return True
    _narrative_day, _narrative_count = _roll_day(_narrative_day, _narrative_count)
    return _narrative_count < OPENAI_DAILY_NARRATIVE_MAX


def record_narrative_openai_call() -> None:
    global _narrative_day, _narrative_count
    _narrative_day, _narrative_count = _roll_day(_narrative_day, _narrative_count)
    _narrative_count += 1


def reset_openai_counters_for_tests() -> None:
    global _rca_day, _rca_count, _narrative_day, _narrative_count
    _rca_day = _narrative_day = None
    _rca_count = _narrative_count = 0


# --- HTTP rate limits ------------------------------------------------------

DEMO_TRIGGER_MAX_PER_IP_HOUR = _int_env("DEMO_TRIGGER_MAX_PER_IP_HOUR", 4)
DEMO_TRIGGER_MAX_GLOBAL_DAY = _int_env("DEMO_TRIGGER_MAX_GLOBAL_DAY", 30)
DEMO_NARRATIVE_MAX_PER_IP_HOUR = _int_env("DEMO_NARRATIVE_MAX_PER_IP_HOUR", 6)
DEMO_ASK_MAX_PER_IP_HOUR = _int_env("DEMO_ASK_MAX_PER_IP_HOUR", 6)

DEMO_TRIGGER_PASSCODE = os.getenv("DEMO_TRIGGER_PASSCODE", "").strip()

_http_buckets: dict[str, list[float]] = defaultdict(list)


def _prune(key: str, window_seconds: int) -> None:
    now = time.time()
    _http_buckets[key] = [t for t in _http_buckets[key] if now - t < window_seconds]


def check_http_rate_limit(key: str, *, max_calls: int, window_seconds: int) -> None:
    """Raise ValueError when limit exceeded (map to HTTP 429 in FastAPI)."""
    if max_calls <= 0:
        return
    _prune(key, window_seconds)
    if len(_http_buckets[key]) >= max_calls:
        raise ValueError(
            f"Demo rate limit reached ({max_calls} per {window_seconds // 3600 or 1}h). "
            "Try again later or run locally with your own API key.",
        )
    _http_buckets[key].append(time.time())


def clear_http_rate_limits_for_tests() -> None:
    _http_buckets.clear()


def client_ip_from_headers(forwarded_for: str | None, peer: str | None) -> str:
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    return peer or "unknown"


def verify_trigger_passcode(provided: str | None) -> None:
    if not DEMO_TRIGGER_PASSCODE:
        return
    if (provided or "").strip() != DEMO_TRIGGER_PASSCODE:
        raise ValueError("Invalid demo passcode.")
