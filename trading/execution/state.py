# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/state.py
───────────────────
Persistent live position state store.

Manages state/live_positions.json — the restart-safe record of open and
recently-closed dry-run / live positions.

Public API:
    open_position(trade_id, ...)          → None
    close_position(trade_id, status, ...) → bool
    get_open_positions()                  → List[dict]
    get_all_positions()                   → Dict[str, dict]
    mark_flatten_requested(trade_id, ...) → bool
    load_positions()                      → dict  (full state doc)
    save_positions(data)                  → None

    # Pending-exit queue (paper→live race fix, Phase 4)
    queue_pending_exit(trade_id, exit_info) → None
    pop_pending_exit(trade_id)              → Optional[dict]
    get_pending_exits()                     → Dict[str, dict]

Design rules:
- Never raises on read errors — logs and returns empty state (fail-safe read).
- Writes are atomic: write to .tmp then os.replace().
- Paper mode never calls these functions.
- No signing, no wallet access, no network calls.

Phase 3: dry_run_live positions are tracked here.
Phase 4: micro_live fills and real position lifecycle will extend this.
Phase 4 (race fix): pending_exits section added to handle paper-exit-before-
  live-fill race condition. When paper evaluate_exit() fires before the live
  BUY fill has been persisted, the exit intent is queued here instead of being
  silently dropped. The queue is consumed by the adapter the moment the BUY
  fill is confirmed, or by the live-position supervisor on the next sweep.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root
from typing import Any, Dict, List, Optional

from trading.execution.journal import append_state_transition

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = runtime_root()
STATE_DIR  = _REPO_ROOT / "state"
STATE_PATH = STATE_DIR / "live_positions.json"

_STATE_VERSION = 1


# ── Load / save ───────────────────────────────────────────────────────────────

def load_positions() -> Dict[str, Any]:
    """Load the full state document from disk.

    Returns an empty-but-valid state document if the file doesn't exist
    or is unreadable. Never raises.
    """
    if not STATE_PATH.exists():
        return _empty_state()
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("version") != _STATE_VERSION:
            log.warning(
                "state: version mismatch (got %s, expected %s) — returning empty state",
                data.get("version"), _STATE_VERSION,
            )
            return _empty_state()
        return data
    except Exception as exc:  # noqa: BLE001
        log.error(
            "state: failed to load %s: %s — returning empty state",
            STATE_PATH, exc,
        )
        return _empty_state()


def save_positions(data: Dict[str, Any]) -> None:
    """Atomically save the state document to disk.

    Uses write-to-tmp-then-replace for crash safety.
    Never raises — logs on failure.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = _now_iso()
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(STATE_PATH))
        log.debug(
            "state: saved %d positions to %s",
            len(data.get("positions", {})), STATE_PATH,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("state: failed to save %s: %s", STATE_PATH, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Position lifecycle ────────────────────────────────────────────────────────

def open_position(
    trade_id: str,
    token: str,
    policy_track: str,
    size_usd: float,
    entry_price: float,
    entry_mcap: float,
    pressure: float,
    mode: str,
    token_amount: Optional[float] = None,
    buy_tx_id: Optional[str] = None,
) -> None:
    """Record a new open position in the state store.

    Called after a BUY intent passes risk checks (dry_run_sent or filled).
    buy_tx_id is the BUY transaction signature; it must be set whenever the
    tx confirmed on-chain so that tx_id is never null for a live open position.
    """
    data = load_positions()
    positions: Dict[str, Any] = data.setdefault("positions", {})

    if trade_id in positions:
        log.warning(
            "state: open_position called for already-tracked trade_id=%s", trade_id,
        )

    previous_status = str(positions.get(trade_id, {}).get("status") or "")

    positions[trade_id] = {
        "trade_id":           trade_id,
        "token":              token,
        "policy_track":       policy_track,
        "size_usd":           size_usd,
        "token_amount":        token_amount,
        "entry_price":        entry_price,
        "entry_mcap":         entry_mcap,
        "pressure":           pressure,
        "mode":               mode,
        "status":             "open",
        "opened_at":          _now_iso(),
        "closed_at":          None,
        "realized_price":     None,
        "realized_size_usd":  None,
        "slippage_bps_proxy": None,
        "tx_id":              buy_tx_id,   # BUY tx signature; overwritten by SELL on close
        "close_reason":       None,
        "flatten_requested":  False,
    }
    save_positions(data)
    _safe_transition(
        domain="position",
        trade_id=trade_id,
        token=token,
        policy_track=policy_track,
        mode=mode,
        from_state=previous_status,
        to_state="open",
        event="position_opened",
        metadata={
            "size_usd": round(size_usd, 4),
            "token_amount": token_amount,
            "entry_price": entry_price,
            "entry_mcap": entry_mcap,
            "pressure": pressure,
        },
    )
    log.info(
        "state: opened position trade_id=%s token=%s track=%s size=$%.2f mode=%s",
        trade_id, token[:8], policy_track, size_usd, mode,
    )


def close_position(
    trade_id: str,
    status: str,                           # "closed", "flattened", "failed"
    realized_price: Optional[float] = None,
    realized_size_usd: Optional[float] = None,
    slippage_bps_proxy: Optional[int] = None,
    tx_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    """Mark a position as closed in the state store.

    Returns True if the position was found and updated, False otherwise.
    """
    data = load_positions()
    positions: Dict[str, Any] = data.get("positions", {})

    if trade_id not in positions:
        log.warning(
            "state: close_position called for unknown trade_id=%s", trade_id,
        )
        return False

    pos = positions[trade_id]
    previous_status = str(pos.get("status") or "")
    if pos.get("status") != "open":
        log.warning(
            "state: close_position called for non-open trade_id=%s (status=%s)",
            trade_id, pos.get("status"),
        )
        # Still update — allows re-closing in error-recovery paths.

    pos.update({
        "status":             status,
        "closed_at":          _now_iso(),
        "realized_price":     realized_price,
        "realized_size_usd":  realized_size_usd,
        "slippage_bps_proxy": slippage_bps_proxy,
        "tx_id":              tx_id,
        "close_reason":       reason,
    })
    save_positions(data)
    _safe_transition(
        domain="position",
        trade_id=trade_id,
        token=str(pos.get("token") or ""),
        policy_track=str(pos.get("policy_track") or ""),
        mode=str(pos.get("mode") or ""),
        from_state=previous_status,
        to_state=status,
        event="position_closed",
        reason=reason or "",
        metadata={
            "realized_price": realized_price,
            "realized_size_usd": realized_size_usd,
            "slippage_bps_proxy": slippage_bps_proxy,
            "tx_id": tx_id,
        },
    )
    log.info(
        "state: closed position trade_id=%s status=%s reason=%s",
        trade_id, status, reason or "-",
    )
    return True


def mark_flatten_requested(
    trade_id: str,
    reason: str = "flatten_all_requested",
) -> bool:
    """Flag a position as pending flatten.

    Does NOT close the position or send any order.
    Used by flatten_all_live.py to record intent without execution.
    Returns True if the trade_id was found.
    """
    data = load_positions()
    positions: Dict[str, Any] = data.get("positions", {})

    if trade_id not in positions:
        log.warning(
            "state: mark_flatten_requested for unknown trade_id=%s", trade_id,
        )
        return False

    pos = positions[trade_id]
    positions[trade_id]["flatten_requested"]    = True
    positions[trade_id]["flatten_requested_at"] = _now_iso()
    positions[trade_id]["flatten_reason"]       = reason
    save_positions(data)
    _safe_transition(
        domain="position",
        trade_id=trade_id,
        token=str(pos.get("token") or ""),
        policy_track=str(pos.get("policy_track") or ""),
        mode=str(pos.get("mode") or ""),
        from_state=str(pos.get("status") or ""),
        to_state=str(pos.get("status") or ""),
        event="flatten_requested",
        reason=reason,
    )
    log.info("state: flatten_requested marked for trade_id=%s reason=%s", trade_id, reason)
    return True


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_open_positions() -> List[Dict[str, Any]]:
    """Return a list of all currently-open positions (status == 'open')."""
    data = load_positions()
    return [
        p for p in data.get("positions", {}).values()
        if p.get("status") == "open"
    ]


def get_all_positions() -> Dict[str, Any]:
    """Return the full positions dict (open + closed)."""
    return load_positions().get("positions", {})


def count_open_positions() -> int:
    """Return the number of currently-open positions."""
    return len(get_open_positions())


def is_token_open(token: str) -> bool:
    """Return True if there is an open position for this token (any track)."""
    return any(p["token"] == token for p in get_open_positions())


# ── File probe (separate from load, raises instead of swallowing) ─────────────

def probe_state_file() -> Dict[str, Any]:
    """Check whether the state file is readable and structurally valid.

    Returns a dict with:
        readable  : bool  — True if the file is absent (expected) or fully valid
        exists    : bool  — True if the file exists on disk
        error     : str   — non-empty description when readable=False
        version   : int | None — version found in the file (None if unreadable)

    Unlike load_positions(), this function does NOT swallow errors.
    Use it in ops scripts to distinguish:
        - File absent   → readable=True, exists=False  (first-run, normal)
        - File valid    → readable=True, exists=True   (normal)
        - File corrupt  → readable=False, exists=True  (dangerous — act on this)
        - Version mismatch → readable=False, exists=True (schema incompatibility)
    """
    if not STATE_PATH.exists():
        return {"readable": True, "exists": False, "error": "", "version": None}
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "readable": False,
            "exists": True,
            "error": f"OS read error: {exc}",
            "version": None,
        }
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "readable": False,
            "exists": True,
            "error": f"JSON parse error: {exc}",
            "version": None,
        }
    if not isinstance(data, dict):
        return {"readable": False, "exists": True, "error": "state root is not a mapping", "version": None}
    version = data.get("version")
    if version != _STATE_VERSION:
        return {
            "readable": False,
            "exists": True,
            "error": (
                f"version mismatch: file has version={version!r}, "
                f"expected {_STATE_VERSION}"
            ),
            "version": version,
        }
    if not isinstance(data.get("positions"), dict):
        return {
            "readable": False,
            "exists": True,
            "error": "'positions' field missing or not a dict",
            "version": version,
        }
    return {"readable": True, "exists": True, "error": "", "version": version}


# ── Integrity check ───────────────────────────────────────────────────────────

def check_state_integrity() -> Dict[str, Any]:
    """Run basic integrity checks on the state file.

    Returns a report dict with:
        ok       : bool   — True if no dangerous inconsistencies found
        issues   : list   — list of issue description strings
        summary  : str    — short human-readable summary
    """
    # Publication safety fix: a forgiving read must not hide a corrupt file.
    probe = probe_state_file()
    if not probe["readable"]:
        return {"ok": False, "issues": [probe["error"]], "summary": "unreadable state", "open_count": 0}
    issues: List[str] = []
    data = load_positions()
    positions = data.get("positions", {})

    for trade_id, pos in positions.items():
        # Required fields present
        for field in ("token", "status", "opened_at", "mode", "policy_track"):
            if not pos.get(field):
                issues.append(f"trade_id={trade_id} missing required field '{field}'")

        # Open positions must not have a closed_at
        if pos.get("status") == "open" and pos.get("closed_at") is not None:
            issues.append(
                f"trade_id={trade_id} status=open but closed_at is set: "
                f"{pos['closed_at']}"
            )

        # Closed positions must have a closed_at
        if pos.get("status") in ("closed", "flattened") and not pos.get("closed_at"):
            issues.append(
                f"trade_id={trade_id} status={pos['status']} but closed_at is missing"
            )

    ok = len(issues) == 0
    open_count   = sum(1 for p in positions.values() if p.get("status") == "open")
    closed_count = len(positions) - open_count

    summary = (
        f"state integrity: {'OK' if ok else 'ISSUES FOUND'}  "
        f"total={len(positions)}  open={open_count}  closed={closed_count}  "
        f"issues={len(issues)}"
    )

    if not ok:
        log.warning("state: %s", summary)
        for issue in issues:
            log.warning("state: issue: %s", issue)

    return {"ok": ok, "issues": issues, "summary": summary, "open_count": open_count}


# ── Pending-exit queue (paper→live race fix) ──────────────────────────────────
#
# When paper evaluate_exit() fires before the live BUY fill has been confirmed
# and persisted, _emit_micro_live_sell() would find no open live position and
# previously dropped the exit silently — orphaning the live trade.
#
# Instead, the exit intent is queued here under the trade_id key.  The adapter
# consumes (pops) the entry the moment the BUY fill is confirmed.  The live-
# position supervisor (position_monitor_loop) provides a 10-second backup sweep.
#
# The pending_exits section is stored in the same live_positions.json file so
# it survives process restarts.

def queue_pending_exit(trade_id: str, exit_info: Dict[str, Any]) -> None:
    """Queue a pending exit intent for a trade that has not yet live-filled.

    Called by _emit_micro_live_sell() (via _queue_micro_live_pending_exit in
    pumpfun_bot.py) when _micro_live_has_open_position() returns False —
    meaning the live BUY fill has not yet been persisted to state.

    exit_info must contain at minimum:
        trade_id, mint, track_name, stake_usd, entry_mcap, pressure,
        signal_price, signal_time_s, source

    The queued entry is consumed by pop_pending_exit() when the BUY fill
    arrives (adapter._safe_check_pending_exit) or by the orphan supervisor.
    """
    data = load_positions()
    pending: Dict[str, Any] = data.setdefault("pending_exits", {})

    was_pending = trade_id in pending
    if was_pending:
        log.warning(
            "state: queue_pending_exit overwriting existing pending exit "
            "for trade_id=%s (previous queued_at=%s)",
            trade_id, pending[trade_id].get("queued_at", "?"),
        )

    pending[trade_id] = {**exit_info, "queued_at": _now_iso()}
    save_positions(data)
    _safe_transition(
        domain="pending_exit",
        trade_id=trade_id,
        token=str(exit_info.get("mint") or ""),
        policy_track=str(exit_info.get("track_name") or ""),
        mode=str(exit_info.get("mode") or "micro_live"),
        from_state="queued" if was_pending else "",
        to_state="queued",
        event="pending_exit_queued" if not was_pending else "pending_exit_overwritten",
        reason=str(exit_info.get("source") or ""),
        metadata={"queued_from": exit_info.get("source"), "signal_time_s": exit_info.get("signal_time_s")},
    )
    log.info(
        "state: pending exit queued  trade_id=%s  mint=%s  source=%s",
        trade_id,
        str(exit_info.get("mint", "?"))[:8],
        exit_info.get("source", "?"),
    )


def pop_pending_exit(trade_id: str) -> Optional[Dict[str, Any]]:
    """Atomically retrieve and remove a pending exit for trade_id.

    Returns the exit_info dict if a pending exit was found, None otherwise.
    The entry is removed from the state file on retrieval (consumed once).

    Atomic file replacement only: load → pop → save. Concurrent writers require external serialization.
    """
    data = load_positions()
    pending: Dict[str, Any] = data.get("pending_exits", {})

    if trade_id not in pending:
        return None

    exit_info = pending.pop(trade_id)
    save_positions(data)
    _safe_transition(
        domain="pending_exit",
        trade_id=trade_id,
        token=str(exit_info.get("mint") or ""),
        policy_track=str(exit_info.get("track_name") or ""),
        mode=str(exit_info.get("mode") or "micro_live"),
        from_state="queued",
        to_state="consumed",
        event="pending_exit_consumed",
        reason=str(exit_info.get("source") or ""),
        metadata={"queued_at": exit_info.get("queued_at")},
    )
    log.info(
        "state: pending exit consumed  trade_id=%s  queued_at=%s",
        trade_id,
        exit_info.get("queued_at", "?"),
    )
    return exit_info


def get_pending_exits() -> Dict[str, Any]:
    """Return a snapshot of all pending exits (for supervisor/monitoring use).

    Returns an empty dict if no pending exits or on read error.
    Does NOT mutate state — use pop_pending_exit to consume an entry.
    """
    return load_positions().get("pending_exits", {})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _empty_state() -> Dict[str, Any]:
    return {
        "version":       _STATE_VERSION,
        "last_updated":  _now_iso(),
        "positions":     {},
        "pending_exits": {},
    }



def _safe_transition(**kwargs) -> None:
    try:
        append_state_transition(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("state: state transition journal write failed: %s", exc)



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
