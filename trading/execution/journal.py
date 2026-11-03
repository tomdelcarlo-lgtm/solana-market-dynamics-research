# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/journal.py
─────────────────────
Journal I/O for the dry-run / live execution path.

Responsibilities:
- Ensure journal CSV files exist with the correct headers (idempotent).
- Append one row per order attempt (allowed or denied).
- Append one row per risk evaluation event.
- Append one row per execution outcome (live_execution_log.csv — Phase 3+).
- Append one row per explicit state transition (live_state_transitions.csv).

Design rules:
- Never raise on write errors — log and continue (journaling must not block orders).
- Files are created with headers only when they don't yet exist.
- Paper mode never calls these functions.
- No live signing, no wallet access, no network calls.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root
from typing import Any, Dict, Optional

from trading.execution.types import ExecutionIntent, ExecutionResult, RiskDecision, OrderStatus

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = runtime_root()
LOGS_DIR = _REPO_ROOT / "logs"

ORDER_JOURNAL_PATH     = LOGS_DIR / "live_order_journal.csv"
RISK_EVENTS_PATH       = LOGS_DIR / "live_risk_events.csv"
EXECUTION_LOG_PATH     = LOGS_DIR / "live_execution_log.csv"
STATE_TRANSITIONS_PATH = LOGS_DIR / "live_state_transitions.csv"

# ── Schema definitions ────────────────────────────────────────────────────────

ORDER_JOURNAL_HEADERS = [
    "timestamp",
    "mode",
    "action",
    "trade_id",
    "policy_track",
    "token",
    "intended_size_usd",
    "intended_entry_mcap",
    "pressure",
    "quote_metadata",
    "status",
    "deny_reason",
]

RISK_EVENTS_HEADERS = [
    "timestamp",
    "mode",
    "action",
    "token",
    "policy_track",
    "check_name",
    "allow",
    "reason",
]

# Phase 3: execution outcomes (filled, failed, partial, cancelled, flattened)
EXECUTION_LOG_HEADERS = [
    "timestamp",
    "mode",
    "action",
    "token",
    "trade_id",
    "policy_track",
    "intended_size_usd",
    "realized_size_usd",
    "intended_price",
    "realized_price",
    "slippage_bps_proxy",
    "tx_id",
    "status",       # filled | failed | partial | cancelled | flattened | dry_run_sent
    "reason",
    "market_regime",  # DEAD | NORMAL | HOT | UNKNOWN — instrumentation only, BUY intents only
]

STATE_TRANSITIONS_HEADERS = [
    "timestamp",
    "domain",
    "trade_id",
    "token",
    "policy_track",
    "mode",
    "from_state",
    "to_state",
    "event",
    "reason",
    "metadata",
]


# ── Schema initialisation ─────────────────────────────────────────────────────

def ensure_journals() -> None:
    """Create journal files with headers if they don't exist yet.

    Idempotent — safe to call at startup or before first write.
    Creates the logs/ directory if needed.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_csv(ORDER_JOURNAL_PATH, ORDER_JOURNAL_HEADERS)
    _ensure_csv(RISK_EVENTS_PATH, RISK_EVENTS_HEADERS)
    _ensure_csv(EXECUTION_LOG_PATH, EXECUTION_LOG_HEADERS)
    _ensure_csv(STATE_TRANSITIONS_PATH, STATE_TRANSITIONS_HEADERS)


def _ensure_csv(path: Path, headers: list[str]) -> None:
    if path.exists():
        try:
            with path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                existing_headers = next(reader, [])
            if existing_headers == headers:
                return
            if existing_headers and all(col in headers for col in existing_headers):
                _migrate_csv_headers(path, existing_headers, headers)
                return
            return
        except OSError as exc:
            log.error("failed to inspect journal %s: %s", path, exc)
            return
    try:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
        log.info("journal created: %s", path)
    except OSError as exc:
        log.error("failed to create journal %s: %s", path, exc)


# ── Order journal writes ──────────────────────────────────────────────────────

def append_order_journal(result: ExecutionResult, trade_id: str = "") -> None:
    """Append one row to live_order_journal.csv for a completed order attempt.

    Called after every dry-run dispatch (allowed or denied).
    Never raises — journal write failures are logged and swallowed.
    """
    intent = result.intent
    try:
        ensure_journals()
        row = {
            "timestamp":            _now_iso(),
            "mode":                 intent.mode,
            "action":               intent.action.value,
            "trade_id":             trade_id,
            "policy_track":         intent.policy_track,
            "token":                intent.token,
            "intended_size_usd":    round(intent.intended_size_usd, 4),
            "intended_entry_mcap":  round(intent.intended_entry_mcap, 2),
            "pressure":             round(intent.pressure, 6),
            "quote_metadata":       _serialise(intent.quote_metadata),
            "status":               result.status.value,
            "deny_reason":          result.deny_reason or "",
        }
        _append_row(ORDER_JOURNAL_PATH, ORDER_JOURNAL_HEADERS, row)
        log.debug(
            "order journal: token=%s action=%s status=%s deny=%s",
            intent.token[:8], intent.action.value, result.status.value,
            result.deny_reason or "-",
        )
    except Exception as exc:  # noqa: BLE001
        log.error("order journal write failed: %s", exc)


# ── Risk events journal writes ────────────────────────────────────────────────

def append_risk_event(
    intent: ExecutionIntent,
    decision: RiskDecision,
) -> None:
    """Append one row to live_risk_events.csv for a risk evaluation.

    Called whenever the risk engine evaluates an intent (Phase 2+).
    """
    try:
        ensure_journals()
        row = {
            "timestamp":    _now_iso(),
            "mode":         intent.mode,
            "action":       intent.action.value,
            "token":        intent.token,
            "policy_track": intent.policy_track,
            "check_name":   decision.check_name,
            "allow":        str(decision.allow).lower(),
            "reason":       decision.reason,
        }
        _append_row(RISK_EVENTS_PATH, RISK_EVENTS_HEADERS, row)
    except Exception as exc:  # noqa: BLE001
        log.error("risk events journal write failed: %s", exc)


# ── Execution log writes (Phase 3) ───────────────────────────────────────────

def append_execution_log(
    result: "ExecutionResult",
    trade_id: str,
    intended_price: Optional[float] = None,
    market_regime: str = "",
) -> None:
    """Append one row to live_execution_log.csv for a completed execution outcome.

    Called after every dry_run_sent or live fill (allowed orders only).
    Never raises — journal write failures are logged and swallowed.

    Fields map to the Phase 3 schema:
        timestamp, mode, action, token, trade_id, policy_track,
        intended_size_usd, realized_size_usd, intended_price, realized_price,
        slippage_bps_proxy, tx_id, status, reason, market_regime

    market_regime is instrumentation only (DEAD/NORMAL/HOT/UNKNOWN), set for
    BUY intents — see execution/market_regime.py.
    """
    intent = result.intent
    try:
        ensure_journals()
        row = {
            "timestamp":          _now_iso(),
            "mode":               intent.mode,
            "action":             intent.action.value,
            "token":              intent.token,
            "trade_id":           trade_id,
            "policy_track":       intent.policy_track,
            "intended_size_usd":  round(intent.intended_size_usd, 4),
            "realized_size_usd":  (
                round(result.realized_size_usd, 4)
                if result.realized_size_usd is not None else ""
            ),
            "intended_price":     (
                round(intended_price, 10) if intended_price is not None else ""
            ),
            "realized_price":     (
                round(result.realized_price, 10)
                if result.realized_price is not None else ""
            ),
            "slippage_bps_proxy": result.slippage_bps_proxy if result.slippage_bps_proxy is not None else "",
            "tx_id":              result.tx_id or "",
            "status":             result.status.value,
            "reason":             result.deny_reason or result.failure_details or "",
            "market_regime":      market_regime,
        }
        _append_row(EXECUTION_LOG_PATH, EXECUTION_LOG_HEADERS, row)
        log.debug(
            "execution log: trade_id=%s token=%s action=%s status=%s",
            trade_id, intent.token[:8], intent.action.value, result.status.value,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("execution log write failed: %s", exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def append_state_transition(
    *,
    domain: str,
    trade_id: str = "",
    token: str = "",
    policy_track: str = "",
    mode: str = "",
    from_state: str = "",
    to_state: str = "",
    event: str = "",
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one row to live_state_transitions.csv.

    This is the canonical append-only log for explicit lifecycle state changes
    across the order path, persistent position state, pending-exit queue, and
    control-plane state.
    """
    try:
        ensure_journals()
        row = {
            "timestamp":    _now_iso(),
            "domain":       domain,
            "trade_id":     trade_id,
            "token":        token,
            "policy_track": policy_track,
            "mode":         mode,
            "from_state":   from_state,
            "to_state":     to_state,
            "event":        event,
            "reason":       reason,
            "metadata":     _serialise(metadata),
        }
        _append_row(STATE_TRANSITIONS_PATH, STATE_TRANSITIONS_HEADERS, row)
    except Exception as exc:  # noqa: BLE001
        log.error("state transition journal write failed: %s", exc)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _append_row(path: Path, headers: list[str], row: Dict[str, Any]) -> None:
    """Append a single dict row to a CSV (file must already exist with headers)."""
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writerow(row)


def _migrate_csv_headers(path: Path, old_headers: list[str], new_headers: list[str]) -> None:
    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append({key: row.get(key, "") for key in new_headers})

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=new_headers)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)
    log.info("journal migrated: %s headers %s -> %s", path, old_headers, new_headers)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialise(obj: Any) -> str:
    if obj is None:
        return ""
    try:
        return json.dumps(obj, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(obj)
