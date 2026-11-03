# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
ops/live_control.py
────────────────────
Arm/disarm, safe-mode, and kill-switch helpers.

Design rules:
- Armed state defaults to False. Explicit arm() call required.
- Safe mode defaults to True until cleared. Fail-closed.
- Phase 3: arm/safe-mode state is persisted to state/live_control_state.json
  so a restart does not silently re-arm a previously armed session.
- Paper mode never touches these helpers.

IMPORTANT: Nothing in this file sends, signs, or initiates orders.
This is an operational flag store, not an execution path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root
from typing import Optional

from trading.execution.journal import append_state_transition

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT   = runtime_root()
_STATE_DIR   = _REPO_ROOT / "state"
_CONTROL_PATH = _STATE_DIR / "live_control_state.json"

# ── In-process state ──────────────────────────────────────────────────────────
# Protected by a single lock.

_lock = threading.Lock()

_armed: bool = False                         # True only after explicit arm()
_safe_mode: bool = True                      # Starts in safe mode; cleared explicitly
_armed_at: Optional[datetime] = None
_safe_mode_reason: Optional[str] = "system_startup"
_process_started_at: datetime = datetime.now(timezone.utc)
_last_synced_state_updated_at: Optional[datetime] = None
_BOOTSTRAP_ARM_SYNC_GRACE_S: float = 900.0   # tolerate watchdog-confirmed arm across restarts (15 min)


# ── Persistence helpers ───────────────────────────────────────────────────────

def _parse_iso_utc(raw: str) -> datetime:
    """Parse persisted timestamps, accepting both `Z` and `+00:00` UTC forms."""
    ts = raw.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _persist_control_state() -> None:
    """Write current arm/safe-mode flags to disk (best-effort, never raises)."""
    global _last_synced_state_updated_at
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        doc = {
            "armed":             _armed,
            "armed_at":          _armed_at.isoformat() if _armed_at else None,
            "safe_mode":         _safe_mode,
            "safe_mode_reason":  _safe_mode_reason,
            "last_updated":      now.isoformat(),
        }
        tmp = _CONTROL_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_CONTROL_PATH))
        _last_synced_state_updated_at = now
    except Exception as exc:  # noqa: BLE001
        log.error("live_control: failed to persist state: %s", exc)


def _sync_from_persisted_if_newer() -> None:
    """Apply operator control changes written after this process started.

    Safety rule:
    - persisted state from BEFORE this process started is normally ignored, so a stale
      file from a prior session cannot silently re-arm this runtime on boot.
    - exception: if the persisted file says armed=true, safe_mode=false, and
      last_updated is within _BOOTSTRAP_ARM_SYNC_GRACE_S seconds before process
      boot, treat it as a watchdog-confirmed active session and apply it.
      last_updated (not armed_at) is used because the watchdog writes it on every
      healthy-bot check, making it the most recent confirmation of arm state.
    - persisted state written AFTER startup is treated as an explicit operator
      command and may update the in-process arm/safe-mode flags.
    """
    global _armed, _armed_at, _safe_mode, _safe_mode_reason, _last_synced_state_updated_at
    try:
        if not _CONTROL_PATH.exists():
            return
        raw = json.loads(_CONTROL_PATH.read_text(encoding="utf-8"))
        last_updated_raw = raw.get("last_updated")
        if not last_updated_raw:
            return
        last_updated = _parse_iso_utc(last_updated_raw)
        if _last_synced_state_updated_at is not None and last_updated <= _last_synced_state_updated_at:
            return
        armed_at_raw = raw.get("armed_at")
        armed_at = None
        if armed_at_raw:
            armed_at = _parse_iso_utc(armed_at_raw)

        armed = bool(raw.get("armed", False))
        safe_mode = bool(raw.get("safe_mode", True))
        bootstrap_grace = False
        if last_updated <= _process_started_at:
            # Use last_updated (not armed_at) as the grace reference.
            # The watchdog writes last_updated on every healthy-bot check, so it
            # represents "when was this arm state last externally confirmed."
            # armed_at can be days old even for a continuously-running session,
            # which caused bootstrap_grace to always fail after the first restart.
            age_s = (_process_started_at - last_updated).total_seconds()
            bootstrap_grace = armed and (not safe_mode) and 0.0 <= age_s <= _BOOTSTRAP_ARM_SYNC_GRACE_S
            if not bootstrap_grace:
                return
        prev_control_state = _control_state_label(_armed, _safe_mode)
        with _lock:
            _armed = armed
            _armed_at = armed_at if armed else None
            _safe_mode = safe_mode
            _safe_mode_reason = raw.get("safe_mode_reason") if _safe_mode else None
            _last_synced_state_updated_at = last_updated
        _safe_transition(
            domain="control.sync",
            mode="micro_live",
            from_state=prev_control_state,
            to_state=_control_state_label(_armed, _safe_mode),
            event="persisted_state_applied",
            metadata={
                "last_updated": last_updated.isoformat(),
                "bootstrap_grace": bootstrap_grace,
            },
        )
        if bootstrap_grace:
            log.warning(
                "live_control: applied recent armed state during startup grace "
                "(armed=%s safe_mode=%s updated_at=%s process_started_at=%s)",
                _armed,
                _safe_mode,
                last_updated.isoformat(),
                _process_started_at.isoformat(),
            )
        else:
            log.warning(
                "live_control: applied operator state from disk (armed=%s safe_mode=%s updated_at=%s)",
                _armed,
                _safe_mode,
                last_updated.isoformat(),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("live_control: failed to sync state from disk: %s", exc)


# ── Arm / disarm ──────────────────────────────────────────────────────────────

def is_armed() -> bool:
    """Return True if live trading is manually armed."""
    _sync_from_persisted_if_newer()
    with _lock:
        return _armed


def arm_live(operator: str = "unknown") -> None:
    """Arm live trading.

    Requires safe mode to be cleared first.
    Logs prominently so every arming event is auditable.
    Persists arm state to state/live_control_state.json.
    """
    global _armed, _armed_at
    with _lock:
        if _safe_mode:
            raise RuntimeError(
                "Cannot arm live trading while safe mode is active. "
                f"Safe mode reason: {_safe_mode_reason!r}. "
                "Call exit_safe_mode() explicitly first."
            )
        prev_state = "armed" if _armed else "disarmed"
        _armed = True
        _armed_at = datetime.now(timezone.utc)
        _persist_control_state()
    _safe_transition(
        domain="control.arm",
        mode="micro_live",
        from_state=prev_state,
        to_state="armed",
        event="arm_live",
        metadata={"operator": operator, "armed_at": _armed_at.isoformat()},
    )
    log.warning(
        "LIVE TRADING ARMED by operator=%r at %s",
        operator,
        _armed_at.isoformat(),
    )


def disarm_live(reason: str = "manual_disarm", operator: str = "unknown") -> None:
    """Disarm live trading.

    Safe to call at any time. Idempotent.
    Persists disarmed state to state/live_control_state.json.
    """
    global _armed, _armed_at
    with _lock:
        was_armed = _armed
        _armed = False
        _armed_at = None
        _persist_control_state()
    _safe_transition(
        domain="control.arm",
        mode="micro_live",
        from_state="armed" if was_armed else "disarmed",
        to_state="disarmed",
        event="disarm_live",
        reason=reason,
        metadata={"operator": operator},
    )
    if was_armed:
        log.warning(
            "LIVE TRADING DISARMED by operator=%r, reason=%r",
            operator,
            reason,
        )
    else:
        log.debug("disarm_live called but was already disarmed (no-op)")


# ── Safe mode ─────────────────────────────────────────────────────────────────

def is_safe_mode() -> bool:
    """Return True if the system is in safe mode (live orders blocked)."""
    _sync_from_persisted_if_newer()
    with _lock:
        return _safe_mode


def enter_safe_mode(reason: str, operator: str = "system") -> None:
    """Enter safe mode, blocking all live orders.

    Also disarms live trading automatically.
    Persists safe-mode state to state/live_control_state.json.
    """
    global _safe_mode, _safe_mode_reason, _armed, _armed_at
    with _lock:
        prev_safe_mode = _safe_mode
        _safe_mode = True
        _safe_mode_reason = reason
        prev_armed = _armed
        _armed = False
        _armed_at = None
        _persist_control_state()
    _safe_transition(
        domain="control.safe_mode",
        mode="micro_live",
        from_state="on" if prev_safe_mode else "off",
        to_state="on",
        event="enter_safe_mode",
        reason=reason,
        metadata={"operator": operator, "prev_armed": prev_armed},
    )
    if prev_armed:
        _safe_transition(
            domain="control.arm",
            mode="micro_live",
            from_state="armed",
            to_state="disarmed",
            event="disarm_via_safe_mode",
            reason=reason,
            metadata={"operator": operator},
        )
    log.warning(
        "SAFE MODE ENTERED by operator=%r, reason=%r (armed was %s)",
        operator,
        reason,
        prev_armed,
    )


def exit_safe_mode(operator: str = "unknown") -> None:
    """Clear safe mode.

    Does NOT automatically arm live trading.
    An explicit arm_live() call is still required.
    Persists cleared-safe-mode state to state/live_control_state.json.
    """
    global _safe_mode, _safe_mode_reason
    with _lock:
        prev_safe_mode = _safe_mode
        _safe_mode = False
        prev_reason = _safe_mode_reason
        _safe_mode_reason = None
        _persist_control_state()
    _safe_transition(
        domain="control.safe_mode",
        mode="micro_live",
        from_state="on" if prev_safe_mode else "off",
        to_state="off",
        event="exit_safe_mode",
        reason=prev_reason or "",
        metadata={"operator": operator},
    )
    log.warning(
        "SAFE MODE CLEARED by operator=%r (was: %r). "
        "Live trading is still disarmed. Call arm_live() explicitly.",
        operator,
        prev_reason,
    )


# ── Status summary ────────────────────────────────────────────────────────────

def status_summary() -> dict:
    """Return a snapshot of the current control state for logging/ops."""
    _sync_from_persisted_if_newer()
    with _lock:
        return {
            "armed": _armed,
            "armed_at": _armed_at.isoformat() if _armed_at else None,
            "safe_mode": _safe_mode,
            "safe_mode_reason": _safe_mode_reason,
        }


# ── Restart recovery ──────────────────────────────────────────────────────────

def _control_state_label(armed: bool, safe_mode: bool) -> str:
    if safe_mode:
        return "safe_mode_on_disarmed"
    return "armed" if armed else "disarmed"



def _safe_transition(**kwargs) -> None:
    try:
        append_state_transition(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("live_control: state transition journal write failed: %s", exc)



def load_persisted_state() -> dict:
    """Read the persisted control state from disk without applying it.

    Returns the raw dict from state/live_control_state.json.
    Does NOT modify in-process state.

    Return semantics:
        {}                          — file does not exist (first run / normal)
        {"_unreadable": True, ...}  — file EXISTS but could not be parsed
                                      (treat as dangerous: corruption or truncation)
        normal dict                 — file exists and is valid JSON

    Callers must distinguish the "_unreadable" sentinel from an empty dict
    so that corruption is never silently treated as a clean first-run state.

    The system always re-initialises to armed=False, safe_mode=True at
    startup regardless of the persisted value — this function only exposes
    what was persisted so operators can decide whether to re-arm.
    """
    if not _CONTROL_PATH.exists():
        return {}
    try:
        return json.loads(_CONTROL_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error(
            "live_control: control state file exists but could not be parsed "
            "(corruption or truncation): %s — treating as UNREADABLE",
            exc,
        )
        return {"_unreadable": True, "_error": str(exc)}
