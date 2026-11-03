# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
#!/usr/bin/env python3
"""
pumpfun_bot.py
──────────────────────────────────────────────────────────────────────────────
Pump.fun paper-trading bot.

Architecture
────────────
  Helius WebSocket  →  parse trades  →  token state manager
  Background thread →  evaluate / exit open positions every 10 s
  All actions logged under this repo's local ./logs/ directory

PAPER TRADING ONLY — no real transactions.

Usage
─────
    pip install websockets joblib pandas scikit-learn numpy
    python pumpfun_bot.py
"""

import asyncio
import base64
import collections
import csv
import fcntl
import hashlib
import json
import logging
import os
import struct
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root, require_network
from typing import Optional, Dict, List, Tuple, Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
import websockets

# ── Data layer instrumentation ────────────────────────────────────────────────
# Function references populated once in main(); None = instrumentation disabled.
# All trading logic is completely independent of these variables.
_dl_write_trade    = None
_dl_snapshot       = None
_dl_write_decision = None

# ── Execution infra ───────────────────────────────────────────────────────────
# Paper mode behavior is completely unchanged.
#
# dry_run_live: each paper BUY/SELL is also shadowed through the real execution
#   pipeline (risk checks + journal) without signing or sending anything.
#   Dry-run intents are journaled to logs/live_order_journal.csv.
#
# micro_live: each paper BUY/SELL is ALSO routed through the guarded micro_live
#   execution path. All risk gates run. The send step is a fail-closed stub
#   until Phase 4.1 implements RPC/wallet infrastructure. Paper positions still
#   run unchanged — micro_live only adds the guarded live attempt alongside.
#
# Pattern:
#   _EXEC_CONFIG        — loaded once at startup; None means import failed (safe)
#   _DRY_RUN_ACTIVE     — True only when mode == dry_run_live AND infra loaded OK
#   _MICRO_LIVE_ACTIVE  — True only when mode == micro_live AND infra loaded OK
#
# IMPORTANT: micro_live is NEVER activated from paper or dry_run_live.
#            It requires TRADING_EXECUTION_MODE=micro_live explicitly.
#            Presence of a wallet path alone does NOT activate micro_live.
_DRY_RUN_ACTIVE:    bool = False
_MICRO_LIVE_ACTIVE: bool = False

try:
    from trading.execution.config import ExecutionMode, load_execution_config
    _EXEC_CONFIG = load_execution_config()

    if _EXEC_CONFIG.mode == ExecutionMode.PAPER:
        pass  # paper mode: no change, nothing to log

    elif _EXEC_CONFIG.mode == ExecutionMode.DRY_RUN_LIVE:
        # Phase 2: wire in dry-run adapter and journal infrastructure.
        # Phase 3: also initialise persistent state store.
        from trading.execution.adapter import execute_buy as _exec_buy, execute_sell as _exec_sell
        from trading.execution.types import ExecutionIntent, OrderAction
        from trading.execution.journal import ensure_journals
        from trading.execution.state import check_state_integrity as _check_state_integrity
        ensure_journals()
        _DRY_RUN_ACTIVE = True
        import logging as _log
        _log.getLogger(__name__).warning(
            "pumpfun_bot: TRADING_EXECUTION_MODE=dry_run_live — "
            "paper positions run as normal; every BUY/SELL is also shadowed "
            "through the dry-run pipeline and journaled. Nothing is sent.",
        )
        # Phase 3: check state integrity at startup. Enter safe mode on issues.
        _integrity = _check_state_integrity()
        if not _integrity["ok"]:
            _log.getLogger(__name__).error(
                "pumpfun_bot: live state integrity issues found at startup — "
                "dry_run_live mode entering safe mode: %s",
                _integrity["issues"],
            )
            try:
                from trading.ops.live_control import enter_safe_mode as _esm
                _esm(
                    reason="state_integrity_failed_at_startup",
                    operator="pumpfun_bot_startup",
                )
            except Exception as _sm_err:
                _log.getLogger(__name__).error(
                    "pumpfun_bot: failed to enter safe mode: %s", _sm_err,
                )
        else:
            _log.getLogger(__name__).info(
                "pumpfun_bot: live state OK at startup — %s",
                _integrity["summary"],
            )

    elif _EXEC_CONFIG.mode == ExecutionMode.MICRO_LIVE:
        # Phase 4: wire micro_live execution path.
        # Paper positions still run unchanged. Each BUY/SELL is also routed
        # through the guarded micro_live path (all risk gates run; send is
        # a fail-closed stub in Phase 4 until RPC/wallet is implemented).
        from trading.execution.adapter import execute_buy as _exec_buy, execute_sell as _exec_sell
        from trading.execution.types import ExecutionIntent, OrderAction
        from trading.execution.journal import ensure_journals
        from trading.execution.state import check_state_integrity as _check_state_integrity
        import logging as _log
        # Import live_control NOW so its module-level _process_started_at is
        # stamped at actual process-boot time, not deferred to first-order
        # evaluation.  Without this eager import the lazy local import in
        # risk/engine._check_armed() stamps _process_started_at at the moment
        # the first order arrives, which is AFTER any operator arm() written to
        # disk at startup — causing _sync_from_persisted_if_newer() to treat
        # the armed state as "stale from a prior session" and ignore it forever.
        import trading.ops.live_control as _live_control  # noqa: F401  (side-effect import)

        ensure_journals()

        # State integrity check at startup — enter safe mode on issues.
        _integrity = _check_state_integrity()
        if not _integrity["ok"]:
            _log.getLogger(__name__).error(
                "pumpfun_bot: live state integrity issues found at startup — "
                "micro_live mode entering safe mode: %s",
                _integrity["issues"],
            )
            try:
                from trading.ops.live_control import enter_safe_mode as _esm
                _esm(
                    reason="state_integrity_failed_at_startup",
                    operator="pumpfun_bot_startup",
                )
            except Exception as _sm_err:
                _log.getLogger(__name__).error(
                    "pumpfun_bot: failed to enter safe mode: %s", _sm_err,
                )
        else:
            _log.getLogger(__name__).info(
                "pumpfun_bot: live state OK at startup — %s",
                _integrity["summary"],
            )

        _MICRO_LIVE_ACTIVE = True
        _backend = getattr(getattr(_EXEC_CONFIG, "live", None), "execution_backend", "trade_api")
        _backend = (_backend or "trade_api").strip().lower()
        if _backend == "pumpfun_cli":
            _mode_detail = (
                "guarded micro_live pipeline active with pumpfun-cli BUY routing; "
                "SELL remains on the existing backend until the first BUY path is verified"
            )
        else:
            _mode_detail = (
                "guarded micro_live pipeline active; trade_api send path remains fail-closed "
                "until the current RPC/wallet implementation is enabled"
            )
        _log.getLogger(__name__).warning(
            "pumpfun_bot: TRADING_EXECUTION_MODE=micro_live — "
            "%s. Paper positions run unchanged. Manual arm required before any order passes the armed gate.",
            _mode_detail,
        )

except Exception as _exec_import_err:  # noqa: BLE001
    import logging as _log
    _log.getLogger(__name__).warning(
        "pumpfun_bot: execution infra import failed (%s). "
        "Continuing in paper mode.",
        _exec_import_err,
    )
    _EXEC_CONFIG = None


def _emit_dry_run_buy(
    mint: str,
    track_name: str,
    stake_usd: float,
    entry_mcap: float,
    pressure: float,
    trade_id: str = "",
) -> None:
    """Shadow a paper BUY through the dry-run pipeline (no-op if not active).

    Phase 3: pass trade_id so the state store cross-references the paper position.
    """
    if not _DRY_RUN_ACTIVE or _EXEC_CONFIG is None:
        return
    try:
        intent = ExecutionIntent(
            action=OrderAction.BUY,
            token=mint,
            policy_track=track_name,
            intended_size_usd=stake_usd,
            intended_entry_mcap=entry_mcap,
            pressure=pressure,
            mode=_EXEC_CONFIG.mode.value,
        )
        _exec_buy(intent, _EXEC_CONFIG, trade_id=trade_id)
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).warning("dry_run BUY shadow failed: %s", _e)


def _emit_dry_run_sell(
    mint: str,
    track_name: str,
    stake_usd: float,
    entry_mcap: float,
    pressure: float,
    trade_id: str = "",
) -> None:
    """Shadow a paper SELL through the dry-run pipeline (no-op if not active).

    Phase 3: pass trade_id so the state store can close the matching position.
    """
    if not _DRY_RUN_ACTIVE or _EXEC_CONFIG is None:
        return
    try:
        intent = ExecutionIntent(
            action=OrderAction.SELL,
            token=mint,
            policy_track=track_name,
            intended_size_usd=stake_usd,
            intended_entry_mcap=entry_mcap,
            pressure=pressure,
            mode=_EXEC_CONFIG.mode.value,
        )
        _exec_sell(intent, _EXEC_CONFIG, trade_id=trade_id)
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).warning("dry_run SELL shadow failed: %s", _e)


# ── Phase 4: micro_live emitters ──────────────────────────────────────────────
# These are no-ops unless TRADING_EXECUTION_MODE=micro_live AND the execution
# infra loaded successfully (_MICRO_LIVE_ACTIVE=True).
#
# They run INDEPENDENTLY of the paper/dry_run emitters above.
# Paper positions are never touched by these functions.
# micro_live is NEVER activated by paper or dry_run_live mode.

# Orphan-supervisor escalation memory.
# First critical alert is immediate; repeated alerts for the same stuck orphan are
# rate-limited so safe-mode containment does not flood the logs every 10 seconds.
_ORPHAN_CRITICAL_LOGGED_AT: Dict[str, float] = {}
_ORPHAN_CRITICAL_REMINDER_S = 300.0

def _micro_live_effective_size_usd(track_name: str, fallback_stake_usd: float) -> float:
    """Return the live-sized notional for micro_live intents.

    The paper policy stake can stay large (`$350` core, etc.) while the live
    lane intentionally uses a much smaller diagnostic size. By default this is
    the global `LIVE_POSITION_SIZE_USD`, but Plan 1 parity work may override
    size per track through `LIVE_POSITION_SIZE_OVERRIDES`.
    """
    if _EXEC_CONFIG is None or _EXEC_CONFIG.live is None:
        return fallback_stake_usd
    live_size = float(_EXEC_CONFIG.live.position_size_for_track(track_name) or 0.0)
    return live_size if live_size > 0 else fallback_stake_usd


def _micro_live_has_open_position(trade_id: str, mint: str) -> bool:
    """Return True if the live state currently has an open position to exit."""
    try:
        from trading.execution.state import get_all_positions
        positions = get_all_positions()
        if trade_id:
            pos = positions.get(trade_id)
            if pos and pos.get('status') == 'open':
                return True
        return any(
            p.get('status') == 'open' and p.get('token') == mint
            for p in positions.values()
        )
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).warning("micro_live state probe failed: %s", _e)
        return False


def _emit_micro_live_buy(
    mint: str,
    track_name: str,
    stake_usd: float,
    entry_mcap: float,
    pressure: float,
    trade_id: str = "",
    signal_price: float = 0.0,
    signal_time_s: Optional[float] = None,
) -> None:
    """Route a BUY through the guarded micro_live execution path.

    No-op if _MICRO_LIVE_ACTIVE is False or _EXEC_CONFIG is None.
    All risk gates run inside the adapter. The send step is a fail-closed
    stub until Phase 4.2 implements RPC tx construction and signing.

    Phase 4.1: quote_metadata is now populated from the signal-time price
    snapshot, enabling the quote_freshness gate to function. The gate
    measures time elapsed between signal observation and order attempt;
    orders older than LIVE_QUOTE_MAX_AGE_MS ms are denied as stale.

    Args:
        signal_price:   Last-trade price at signal time (bonding-curve units).
                        Used to build quote_metadata for the freshness gate.
        signal_time_s:  Unix timestamp (s) of the signal. Defaults to now().
                        Pass the original signal.get("signal_time") so the
                        freshness gate reflects true signal age.
        trade_id:       Should match the paper position's trade_id.
    """
    if not _MICRO_LIVE_ACTIVE or _EXEC_CONFIG is None:
        return

    live_cfg = getattr(_EXEC_CONFIG, 'live', None)
    allowed_tracks = list(getattr(live_cfg, 'allowed_policy_tracks', []) or [])
    if allowed_tracks and track_name not in allowed_tracks:
        log.info(
            "[MICRO_LIVE BUY SKIPPED]  %s  track=%s — track not in live allowlist",
            mint[:8], track_name,
        )
        return
    try:
        from trading.execution.quote import make_signal_quote
        quote_meta = make_signal_quote(
            signal_price  = signal_price,
            signal_time_s = signal_time_s,
            entry_mcap    = entry_mcap,
        )
        live_size_usd = _micro_live_effective_size_usd(track_name, stake_usd)
        intent = ExecutionIntent(
            action               = OrderAction.BUY,
            token                = mint,
            policy_track         = track_name,
            intended_size_usd    = live_size_usd,
            intended_entry_mcap  = entry_mcap,
            pressure             = pressure,
            mode                 = _EXEC_CONFIG.mode.value,
            quote_metadata       = quote_meta,
        )
        _exec_buy(intent, _EXEC_CONFIG, trade_id=trade_id)
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).warning("micro_live BUY failed: %s", _e)


def _queue_micro_live_pending_exit(
    mint: str,
    track_name: str,
    stake_usd: float,
    entry_mcap: float,
    pressure: float,
    trade_id: str,
    signal_price: float,
    signal_time_s: Optional[float],
) -> None:
    """Queue a pending exit for a live trade whose BUY fill has not yet arrived.

    Called by _emit_micro_live_sell() when _micro_live_has_open_position()
    returns False — i.e., paper evaluate_exit() fired before the live BUY
    fill was persisted to state.

    The queued intent is consumed (and the live SELL fired) by:
      1. adapter._safe_check_pending_exit() — immediately when the BUY fill
         is confirmed (primary path, fires within seconds).
      2. _supervise_live_orphans() — backup 10-second sweep, handles process
         restarts and any failure in path (1).

    No-op if _MICRO_LIVE_ACTIVE is False or _EXEC_CONFIG is None.
    """
    if not _MICRO_LIVE_ACTIVE or _EXEC_CONFIG is None:
        return

    live_cfg = getattr(_EXEC_CONFIG, 'live', None)
    allowed_tracks = list(getattr(live_cfg, 'allowed_policy_tracks', []) or [])
    if allowed_tracks and track_name not in allowed_tracks:
        log.info(
            "[PENDING EXIT SKIPPED]  trade_id=%s  mint=%s  track=%s — "
            "track not in live allowlist, so no deferred SELL can ever be emitted",
            trade_id or '-', mint[:8], track_name,
        )
        return
    try:
        from trading.execution.state import queue_pending_exit
        queue_pending_exit(
            trade_id  = trade_id,
            exit_info = {
                "trade_id":      trade_id,
                "mint":          mint,
                "track_name":    track_name,
                "stake_usd":     stake_usd,
                "entry_mcap":    entry_mcap,
                "pressure":      pressure,
                "signal_price":  signal_price,
                "signal_time_s": signal_time_s if signal_time_s is not None else time.time(),
                "source":        "paper_exit_before_live_fill",
            },
        )
        log.warning(
            "[PENDING EXIT QUEUED]  trade_id=%s  mint=%s — "
            "paper exit fired before live BUY fill; SELL deferred until fill confirms",
            trade_id or '-', mint[:8],
        )
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).error(
            "micro_live: failed to queue pending exit for trade_id=%s mint=%s: %s — "
            "orphan supervisor will attempt recovery",
            trade_id, mint[:8], _e,
        )


def _emit_micro_live_sell(
    mint: str,
    track_name: str,
    stake_usd: float,
    entry_mcap: float,
    pressure: float,
    trade_id: str = "",
    signal_price: float = 0.0,
    signal_time_s: Optional[float] = None,
) -> None:
    """Route a SELL through the guarded micro_live execution path.

    No-op if _MICRO_LIVE_ACTIVE is False or _EXEC_CONFIG is None.
    trade_id must match the original BUY trade_id for this position.

    Phase 4.1: quote_metadata populated from exit-signal price snapshot.
    For sells, signal_time_s should be the current timestamp (now) since
    the exit fires and executes in the same event loop tick — near-zero
    quote age is expected and correct.

    Race-condition fix (Phase 4): if no open live position is found for
    trade_id/mint, the exit intent is QUEUED (not dropped) via
    _queue_micro_live_pending_exit(). The adapter consumes the queue entry
    when the BUY fill arrives, and the orphan supervisor provides a
    10-second backup sweep.

    Args:
        signal_price:   Current exit price (bonding-curve units).
        signal_time_s:  Unix timestamp (s) of the exit signal.
                        Pass `now` from evaluate_exit's call context.
        trade_id:       Must match the original BUY trade_id.
    """
    if not _MICRO_LIVE_ACTIVE or _EXEC_CONFIG is None:
        return
    if not _micro_live_has_open_position(trade_id=trade_id, mint=mint):
        log.warning(
            "[RACE DETECTED]  micro_live SELL: no open live position for "
            "trade_id=%s mint=%s — paper exit fired before live BUY fill; "
            "queueing pending exit",
            trade_id or '-', mint[:8],
        )
        _queue_micro_live_pending_exit(
            mint          = mint,
            track_name    = track_name,
            stake_usd     = stake_usd,
            entry_mcap    = entry_mcap,
            pressure      = pressure,
            trade_id      = trade_id,
            signal_price  = signal_price,
            signal_time_s = signal_time_s,
        )
        return
    try:
        from trading.execution.quote import make_signal_quote
        quote_meta = make_signal_quote(
            signal_price  = signal_price,
            signal_time_s = signal_time_s,
            entry_mcap    = entry_mcap,
        )
        live_size_usd = _micro_live_effective_size_usd(track_name, stake_usd)
        intent = ExecutionIntent(
            action               = OrderAction.SELL,
            token                = mint,
            policy_track         = track_name,
            intended_size_usd    = live_size_usd,
            intended_entry_mcap  = entry_mcap,
            pressure             = pressure,
            mode                 = _EXEC_CONFIG.mode.value,
            quote_metadata       = quote_meta,
        )
        _exec_sell(intent, _EXEC_CONFIG, trade_id=trade_id)
    except Exception as _e:  # noqa: BLE001
        import logging as _l
        _l.getLogger(__name__).warning("micro_live SELL failed: %s", _e)

# ── Config ────────────────────────────────────────────────────────────────────
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "").strip()
_live_rpc_url   = os.getenv("LIVE_RPC_URL", "").strip()
HELIUS_WS_URL = os.getenv("SOLANA_WS_URL", "").strip()
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

REPO_ROOT = runtime_root()
MODEL_PATH = REPO_ROOT / "models" / "pumpfun_gb_model.joblib"
LOG_PATH = REPO_ROOT / "logs" / "paper_trading_log.csv"
LOG_PATH_V2 = REPO_ROOT / "logs" / "paper_trading_log_v2.csv"
RULE_OBS_PATH = REPO_ROOT / "logs" / "rule_observability.csv"
OUTCOME_V2_TAPE_PATH = REPO_ROOT / "logs" / "outcome_v2_tape.csv"
INSTANCE_LOCK_PATH = REPO_ROOT / "logs" / "pumpfun_bot.lock"

# Portfolio / mode
TRADING_MODE = "rule_based"  # forced: run only the new policy on this host
LOAD_MODEL_DIAGNOSTICS = os.getenv("LOAD_MODEL_DIAGNOSTICS", "false").strip().lower() in {"1", "true", "yes", "on"}
BANKROLL      = float(os.getenv("BANKROLL_USD", "3000"))
POSITION_SIZE = float(os.getenv("POSITION_SIZE_PCT", "0.02"))      # legacy ML mode
FIXED_STAKE_USD = float(os.getenv("FIXED_STAKE_USD", "250"))       # rule-based default
RULE_STAKE_LOW_65_75 = float(os.getenv("RULE_STAKE_LOW_65_75", str(FIXED_STAKE_USD)))
RULE_STAKE_CORE_8K9K = float(os.getenv("RULE_STAKE_CORE_8K9K", "350"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "12"))

# Decision — threshold removed; ranking engine selects top N instead
# P_WIN_THRESHOLD = 0.85  # retired: model outputs are not calibrated probabilities

# Legacy ML filter thresholds
MIN_VOLUME_30MIN   = 2_000
MIN_UNIQUE_BUYERS  = 20
MIN_AVG_TRADE_SIZE = 20
MIN_BUY_SELL_RATIO = 0.8

# Rule-based policy thresholds
RULE_SIGNAL_UNIQUE_BUYERS = int(os.getenv("RULE_SIGNAL_UNIQUE_BUYERS", "60"))
RULE_PRESSURE_MIN = float(os.getenv("RULE_PRESSURE_MIN", "0.70"))
RULE_PRESSURE_MAX = float(os.getenv("RULE_PRESSURE_MAX", "0.90"))
RULE_ENTRY_MCAP_MIN = float(os.getenv("RULE_ENTRY_MCAP_MIN", "14000"))
RULE_ENTRY_MCAP_MAX = float(os.getenv("RULE_ENTRY_MCAP_MAX", "22000"))
RULE_MIN_CLEAN_TRADE_USD = float(os.getenv("RULE_MIN_CLEAN_TRADE_USD", "100"))
RULE_TIMEOUT_MINUTES = int(os.getenv("RULE_TIMEOUT_MINUTES", "10"))
RULE_TP_MULTIPLE = float(os.getenv("RULE_TP_MULTIPLE", "2.0"))

RULE_TRACKS = [
    {
        "name": "v8_core_8k9k",
        "buyers_min": 40,
        "pressure_min": 0.80,
        "pressure_max": 0.90,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 8500.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_CORE_8K9K,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": True,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
        # Policy 8 dawn block: no new entries 02:00–04:59 ART (LOCAL_TZ)
        "blocked_hours": (2, 5),
        # MID group skip: 5-10s minutes_to_signal has 16% WR — trap zone
        "mid_skip_s": (5, 10),
        # Hallazgo 2026-06-10: delay SEEN->signal >=10s tiene EV_lat negativo (n=70,504)
        "token_age_cap_s": 10,
    },
    {
        "name": "v8_core_8k9k_age20s",
        "buyers_min": 40,
        "pressure_min": 0.80,
        "pressure_max": 0.90,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 9000.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_CORE_8K9K,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "signal_age_cap_s": 20,
        "live_entry": False,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
        "blocked_hours": (2, 5),
    },
    {
        "name": "v8_core_8k9k_pressure_high",
        "buyers_min": 40,
        "pressure_min": 0.85,
        "pressure_max": 0.90,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 9000.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_CORE_8K9K,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": False,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "name": "v8_core_8k9k_utc_midnight",
        "buyers_min": 40,
        "pressure_min": 0.80,
        "pressure_max": 0.90,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 9000.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_CORE_8K9K,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": False,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
        "active_hours_utc": (0, 1),
    },
    {
        "name": "v8_core_7080_8k9k",
        "buyers_min": 40,
        "pressure_min": 0.70,
        "pressure_max": 0.80,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 8500.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_CORE_8K9K,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": True,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
        "blocked_hours": (2, 5),
        # fast_stop=-15% is automatic for entry_mcap in [8000,9000) via evaluate_exit()
        # Hallazgo 2026-06-10: delay SEEN->signal >=10s tiene EV_lat negativo (n=70,504)
        "token_age_cap_s": 10,
    },
    {
        "name": "v7_shadow_core_70_80",
        "buyers_min": 40,
        "pressure_min": 0.70,
        "pressure_max": 0.80,
        "entry_mcap_min": 8000.0,
        "entry_mcap_max": 9000.0,
        "clean_trade_usd_min": 100.0,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": False,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "name": "v8_low_65_75",
        "buyers_min": 40,
        # Low micro-probe parity: align paper-emitted live lane with the active
        # live min-pressure gate so paper-vs-real reports compare executable
        # opportunities instead of broad low observation flow.
        "pressure_min": 0.80,
        "pressure_max": 0.90,
        "entry_mcap_min": 6500.0,
        "entry_mcap_max": 7500.0,
        "clean_trade_usd_min": 100.0,
        "stake_usd": RULE_STAKE_LOW_65_75,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": True,
        "active_weekdays": [0, 1, 2, 3, 4, 5],
        # Hallazgo 2026-06-10: delay SEEN->signal >=10s tiene EV_lat negativo (n=70,504)
        "token_age_cap_s": 10,
    },
    {
        "name": "v7_shadow_low_65_75_sunday",
        "buyers_min": 40,
        "pressure_min": 0.70,
        "pressure_max": 0.90,
        "entry_mcap_min": 6500.0,
        "entry_mcap_max": 7500.0,
        "clean_trade_usd_min": 100.0,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": False,
        "active_weekdays": [6],
    },
    {
        "name": "v7_shadow_9k12k",
        "buyers_min": 40,
        "pressure_min": 0.60,
        "pressure_max": 0.90,
        "entry_mcap_min": 9000.0,
        "entry_mcap_max": 12000.0,
        "clean_trade_usd_min": 100.0,
        "tp_multiple": 1.5,
        "timeout_minutes": 8,
        "live_entry": False,
        "active_weekdays": [0, 1, 2, 3, 4, 5, 6],
    },
]

LOCAL_TZ = ZoneInfo("America/Buenos_Aires")


def _log_live_track_alignment() -> None:
    """Log whether the configured live allowlist matches paper-emitted tracks."""
    if not _MICRO_LIVE_ACTIVE or _EXEC_CONFIG is None or _EXEC_CONFIG.live is None:
        return
    live_tracks = set(_EXEC_CONFIG.live.allowed_policy_tracks or [])
    emitted_live_tracks = {
        str(track.get("name") or "")
        for track in RULE_TRACKS
        if track.get("live_entry", True)
    }
    missing = sorted(t for t in live_tracks if t and t not in emitted_live_tracks)
    if missing:
        logging.getLogger(__name__).error(
            "pumpfun_bot: LIVE_ALLOWED_POLICY_TRACKS has tracks the paper engine will never emit: %s | emitted_live_tracks=%s",
            missing,
            sorted(emitted_live_tracks),
        )
    else:
        logging.getLogger(__name__).info(
            "pumpfun_bot: live allowlist aligned with paper-emitted tracks: %s",
            sorted(emitted_live_tracks & live_tracks),
        )


_log_live_track_alignment()

# Timing
EVAL_WIN_LO       = 30   # minutes — start of evaluation window
EVAL_WIN_HI       = 40   # minutes — end   of evaluation window
TIME_EXIT_MINUTES = 90   # legacy ML mode force-close after this many minutes

# Exit thresholds (legacy ML mode)
STOP_LOSS_PCT    = -0.30
TAKE_PROFIT_1    =  1.00   # sell 50 % of remaining
TAKE_PROFIT_2    =  2.00   # sell rest

# WebSocket
RECONNECT_DELAY = 5   # seconds between reconnect attempts

# Ranking
RANKING_INTERVAL    = 10    # seconds between candidate ranking windows
RANKING_TOP_N       = 1     # max positions opened per ranking window
EDGE_RATIO          = 5.0   # top1 must be >= EDGE_RATIO × top2 (relative conviction)
MIN_SCORE_THRESHOLD = 1e-5  # top1 must exceed noise floor (absolute minimum)

# EV instrumentation (observation only — not used in any decision)
B_MEDIAN = 2.84   # median payoff multiple on a graduated token
LOSS     = 0.85   # fraction lost on a non-graduated token
# ev = score * B_MEDIAN - (1 - score) * LOSS  (logged per row, never gated on)

# Rolling score window — keeps last N scores for percentile logging
_score_window: List[float] = []
_score_window_lock = threading.Lock()
_SCORE_WINDOW_SIZE = 500

# ── WebSocket event deduplication ─────────────────────────────────────────────
# Helius logsSubscribe can redeliver the same tx on reconnects or buffered
# retransmissions.  We deduplicate by Solana tx signature (globally unique).
# OrderedDict used as a bounded FIFO: evict oldest when over capacity.
_DEDUP_MAXSIZE = 50_000   # ~4 h headroom at peak pump.fun volume (~200 tx/min)
_seen_tx_sigs: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_seen_tx_sigs_lock = threading.Lock()


def _is_duplicate_sig(sig: str) -> bool:
    """Return True if sig was already processed this session; record it if new."""
    if not sig:
        return False
    with _seen_tx_sigs_lock:
        if sig in _seen_tx_sigs:
            return True
        _seen_tx_sigs[sig] = None
        if len(_seen_tx_sigs) > _DEDUP_MAXSIZE:
            _seen_tx_sigs.popitem(last=False)   # evict oldest entry
    return False


# ── Content-based trade deduplication ────────────────────────────────────────
# Second line of defence against Helius re-delivering the same transaction when
# the WS message is missing or has an empty 'signature' field.
# Key: (mint, wallet, side, blockchain_ts_int, token_amount) — these five fields
# are set by the Solana runtime and are globally unique per pump.fun trade.
_DEDUP_TRADE_MAXSIZE = 20_000
_seen_trade_keys: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
_seen_trade_keys_lock = threading.Lock()


def _is_duplicate_trade(trade: dict) -> bool:
    """Return True if this exact trade content was already processed this session."""
    key = (
        trade.get("mint", ""),
        trade.get("wallet", ""),
        trade.get("side", ""),
        int(trade.get("ts", 0)),          # blockchain timestamp (seconds, integer)
        trade.get("token_amount", 0),     # raw token units — unique per tx
    )
    with _seen_trade_keys_lock:
        if key in _seen_trade_keys:
            return True
        _seen_trade_keys[key] = None
        if len(_seen_trade_keys) > _DEDUP_TRADE_MAXSIZE:
            _seen_trade_keys.popitem(last=False)
    return False

# SOL/USD price — fetched from CoinGecko, refreshed every 5 minutes.
_sol_price_cache: Dict[str, float] = {"price": 150.0, "fetched_at": 0.0}
_SOL_PRICE_REFRESH = 300   # seconds

def _track_is_active_today(track: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
    _ts = now_ts if now_ts is not None else time.time()
    dt = datetime.fromtimestamp(_ts, tz=LOCAL_TZ)
    active_weekdays = track.get("active_weekdays")
    if active_weekdays and dt.weekday() not in active_weekdays:
        return False
    # blocked_hours: (start_hour, end_hour) in LOCAL_TZ — no entries during [start, end)
    blocked_hours = track.get("blocked_hours")
    if blocked_hours:
        block_start, block_end = blocked_hours
        if block_start <= dt.hour < block_end:
            return False
    # active_hours_utc: (start_hour, end_hour) — only active during [start, end) UTC
    active_hours_utc = track.get("active_hours_utc")
    if active_hours_utc:
        utc_hour = datetime.fromtimestamp(_ts, tz=timezone.utc).hour
        ah_start, ah_end = active_hours_utc
        if not (ah_start <= utc_hour < ah_end):
            return False
    return True


def get_sol_price() -> float:
    """Return cached SOL/USD price, refreshing from CoinGecko if stale."""
    if os.getenv("SOLANA_ALLOW_NETWORK") != "1": return float(os.getenv("SOLANA_FIXTURE_SOL_USD", "150"))
    now = time.time()
    if now - _sol_price_cache["fetched_at"] < _SOL_PRICE_REFRESH:
        return _sol_price_cache["price"]
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        price = float(data["solana"]["usd"])
        _sol_price_cache["price"]      = price
        _sol_price_cache["fetched_at"] = now
        log.info(f"SOL/USD refreshed: ${price:.2f}")
        return price
    except Exception as exc:
        log.warning(f"SOL price fetch failed ({exc}), using cached ${_sol_price_cache['price']:.2f}")
        return _sol_price_cache["price"]

# Feature order must match the training artifact.
DEFAULT_FEATURES = [
    "n_trades_30min",
    "volume_first_30min",
    "unique_buyers_30min",
    "unique_sellers_30min",
    "buy_sell_ratio_30min",
    "max_single_buy_30min",
    "avg_trade_size_30min",
    "top1_wallet_pct_30min",
    "launch_hour_utc",
    "launch_dayofweek",
    "launch_month",
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pumpfun_bot")

# ── Singleton instance guard ─────────────────────────────────────────────────
# Prevent accidental double-starts, which otherwise duplicate every incoming tx
# into the CSV / V2 tape and also conflict on the DuckDB instrumentation layer.
_instance_lock_fh = None


def _acquire_singleton_lock() -> None:
    global _instance_lock_fh
    if _instance_lock_fh is not None:
        return

    INSTANCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(INSTANCE_LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        owner = fh.read().strip() or "unknown"
        log.error(
            f"Another pumpfun_bot.py instance is already running, refusing to start. "
            f"lock={INSTANCE_LOCK_PATH} owner={owner}"
        )
        raise SystemExit(1)

    fh.seek(0)
    fh.truncate()
    fh.write(
        f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()} cwd={os.getcwd()}\n"
    )
    fh.flush()
    _instance_lock_fh = fh

# ── Token state ───────────────────────────────────────────────────────────────
# tokens[mint] = {
#     "start_time": float,   # unix timestamp of first observed trade
#     "trades":     list,    # list of trade dicts
#     "evaluated":  bool,
#     "position":   None | dict
# }
tokens: dict       = {}
tokens_lock        = threading.Lock()

# Separate index for quick iteration of open positions
open_positions: dict = {}
positions_lock       = threading.Lock()

# Candidates pool — tokens that passed all gates, waiting for ranking
candidates: list = []
candidates_lock  = threading.Lock()

# ── Model ─────────────────────────────────────────────────────────────────────
model          = None
model_features = DEFAULT_FEATURES


def load_model() -> None:
    global model, model_features
    if not MODEL_PATH.exists():
        log.warning(
            f"Model not found at {MODEL_PATH}. "
            "Bot will log all filter-passing coins but skip ML scoring."
        )
        return
    artifact = joblib.load(MODEL_PATH)
    if isinstance(artifact, dict):
        model = artifact["model"]
        if "features" in artifact:
            model_features = artifact["features"]
    else:
        model = artifact
    log.info(f"Model loaded  ({len(model_features)} features)")


# ── CSV logger ────────────────────────────────────────────────────────────────
CSV_COLUMNS = [
    # ── Identity ──────────────────────────────────────────────────────────────
    "timestamp",
    "token",
    "action",
    "reason",
    "trade_id",
    "trading_mode",
    "policy_track",
    # ── Model / diagnostics ───────────────────────────────────────────────────
    "score",
    "p_win",
    "ev",
    # ── Features / signal stats ───────────────────────────────────────────────
    "volume_first_30min",
    "unique_buyers",
    "avg_trade_size",
    "buy_sell_ratio",
    "buy_volume_share_to_signal",
    "entry_mcap_clean",
    "concurrent_open_positions",
    # ── Filter flags ──────────────────────────────────────────────────────────
    "passed_volume",
    "passed_buyers",
    "passed_avg_size",
    "passed_buy_sell",
    "all_filters_passed",
    # ── Trade tracking ────────────────────────────────────────────────────────
    "entry_price",
    "exit_price",
    "gross_multiple",
    "net_multiple_est",
    "pnl_pct",
    "pnl_usd",
    "position_size_usd",
    # ── Ranking metadata ──────────────────────────────────────────────────────
    "ranking_position",
    "ranking_gap",
    # ── Phase-space axes (structure quality + activity density + breadth) ────
    "top1_wallet_pct_30min",
    "top3_wallet_pct_30min",
    "trades_per_minute",
    "trade_size_cv",
    "buyers_velocity",
    "buyers_acceleration",
    # ── Timing ────────────────────────────────────────────────────────────────
    "age_min",
    "signal_time",
    "signal_hour_utc",
    "entry_time",
    "exit_time",
    # ── Score window stats — non-empty only on RANKING_STATS rows ─────────────
    "score_min",
    "score_max",
    "score_mean",
    "score_p50",
    "score_p75",
    "score_p90",
    "score_p95",
]
# Total: 32 columns. Every row written by log_csv() always has all 32.
# holding_time_sec was removed (v1 schema); age_min replaces it.
# v1 historical data is preserved in paper_trading_log.csv (LOG_PATH).
# v2 schema writes to paper_trading_log_v2.csv (LOG_PATH_V2).
_csv_lock = threading.Lock()
_tape_lock = threading.Lock()

RULE_OBS_COLUMNS = [
    "timestamp",
    "token",
    "stage",
    "track",
    "reason",
    "token_age_min",
    "signal_age_min",
    "signal_time",
    "buyers_threshold",
    "unique_buyers_30min",
    "signal_unique_buyers",
    "volume_first_30min",
    "avg_trade_size",
    "buy_sell_ratio",
    "first_price_sol",
    "signal_price_sol",
    "last_price_sol",
    "chg_first_to_signal_pct",
    "chg_first_to_last_pct",
    "chg_signal_to_last_pct",
    "pressure",
    "pressure_min",
    "pressure_max",
    "entry_mcap_usd",
    "entry_mcap_min",
    "entry_mcap_max",
    "clean_trade_usd_min",
    "clean_trade_count",
    "score_hint",
    "open_positions",
]

OUTCOME_V2_TAPE_COLUMNS = [
    "timestamp",
    "mint",
    "tx_sig",    # Solana transaction signature (canonical event identity; empty if absent)
    "tx_slot",   # Solana slot number (ordering anchor; empty if absent)
    "side",
    "wallet",
    "trade_usd",
    "price_sol",
    "sol_usd",
    "price_usd",
    "mcap_usd",
    "token_age_sec",
    "total_trades_seen",
    "unique_buyers_seen",
    "unique_sellers_seen",
    "buy_trades_seen",
    "sell_trades_seen",
    "buy_volume_usd_seen",
    "sell_volume_usd_seen",
    "buy_volume_share_seen",
]


def _ensure_csv() -> None:
    """Ensure the log directories exist. Headers are written by log_csv() on first write."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)      # v1 directory (historical)
    LOG_PATH_V2.parent.mkdir(parents=True, exist_ok=True)   # v2 directory (new schema)
    RULE_OBS_PATH.parent.mkdir(parents=True, exist_ok=True) # rule observability log
    OUTCOME_V2_TAPE_PATH.parent.mkdir(parents=True, exist_ok=True)


def make_trade_id(mint: str, entry_ts: float) -> str:
    """Stable unique key linking a BUY row to its SELL row(s)."""
    ts_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{mint[:8]}_{ts_str}"


def _push_score(s: float) -> None:
    """Append score to the rolling window (thread-safe, capped at _SCORE_WINDOW_SIZE)."""
    with _score_window_lock:
        _score_window.append(s)
        if len(_score_window) > _SCORE_WINDOW_SIZE:
            del _score_window[0]


def _score_percentiles() -> Optional[Dict[str, float]]:
    """Return p50/p75/p90/p95 of the rolling score window, or None if empty."""
    with _score_window_lock:
        snap = list(_score_window)
    if not snap:
        return None
    arr = np.array(snap, dtype=float)
    return {
        "score_p50": float(np.percentile(arr, 50)),
        "score_p75": float(np.percentile(arr, 75)),
        "score_p90": float(np.percentile(arr, 90)),
        "score_p95": float(np.percentile(arr, 95)),
    }


def log_outcome_v2_tape(trade: Dict[str, Any], state: Dict[str, Any], sol_usd: float) -> None:
    trades = state.get("trades", [])
    if not trades:
        return

    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    buy_wallets = {t.get("wallet") for t in buys if t.get("wallet")}
    sell_wallets = {t.get("wallet") for t in sells if t.get("wallet")}
    buy_volume_usd = sum(float(t.get("usd", 0.0)) for t in buys)
    sell_volume_usd = sum(float(t.get("usd", 0.0)) for t in sells)
    total_volume_usd = buy_volume_usd + sell_volume_usd
    price_sol = float(trade.get("price", 0.0) or 0.0)
    price_usd = price_sol * sol_usd if price_sol > 0 else 0.0
    row = {
        "timestamp": datetime.fromtimestamp(float(trade.get("ts", time.time())), tz=timezone.utc).isoformat(),
        "mint": trade.get("mint", ""),
        "tx_sig":  trade.get("_sig", ""),   # Solana tx signature — set by listen() when available
        "tx_slot": trade.get("_slot", ""),  # Solana slot         — set by listen() when available
        "side": trade.get("side", ""),
        "wallet": trade.get("wallet", ""),
        "trade_usd": round(float(trade.get("usd", 0.0) or 0.0), 6),
        "price_sol": round(price_sol, 12) if price_sol > 0 else "",
        "sol_usd": round(sol_usd, 6),
        "price_usd": round(price_usd, 12) if price_usd > 0 else "",
        "mcap_usd": round(price_usd * 1e9, 2) if price_usd > 0 else "",
        "token_age_sec": round(float(trade.get("ts", 0.0)) - float(state.get("start_time", trade.get("ts", 0.0))), 3),
        "total_trades_seen": len(trades),
        "unique_buyers_seen": len(buy_wallets),
        "unique_sellers_seen": len(sell_wallets),
        "buy_trades_seen": len(buys),
        "sell_trades_seen": len(sells),
        "buy_volume_usd_seen": round(buy_volume_usd, 6),
        "sell_volume_usd_seen": round(sell_volume_usd, 6),
        "buy_volume_share_seen": round((buy_volume_usd / total_volume_usd), 6) if total_volume_usd > 0 else "",
    }

    with _tape_lock:
        exists = OUTCOME_V2_TAPE_PATH.exists()
        with OUTCOME_V2_TAPE_PATH.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTCOME_V2_TAPE_COLUMNS)
            if not exists:
                writer.writeheader()
            writer.writerow(row)


def log_csv(**kwargs) -> None:
    """Write one row to paper_trading_log_v2.csv (v2 fixed schema).

    Every row always contains all 32 columns in CSV_COLUMNS order.
    Columns not relevant to a given action are written as empty string
    (pandas reads them as NaN).

    Column mapping:
      "timestamp"  → always set to current UTC ISO string
      "token"      → taken from kwarg "mint"
      all others   → taken from kwarg of the same name; empty when absent

    Extra kwargs not in CSV_COLUMNS are silently discarded (extrasaction="ignore").
    This means call sites that still pass holding_time_sec or engineered feature
    columns (feat_kw) are unaffected — those values are just not written.
    """
    # Build a complete row with every column defaulting to empty string,
    # then overwrite with whatever kwargs were provided.
    row = {col: "" for col in CSV_COLUMNS}
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    row["token"]     = kwargs.get("mint", "")
    for col in CSV_COLUMNS:
        if col in ("timestamp", "token"):
            continue
        val = kwargs.get(col)
        if val is not None:
            row[col] = val

    with _csv_lock:
        file_exists = LOG_PATH_V2.exists()
        with open(LOG_PATH_V2, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


def log_rule_obs(**kwargs) -> None:
    row = {col: "" for col in RULE_OBS_COLUMNS}
    row["timestamp"] = kwargs.get("timestamp") or datetime.now(timezone.utc).isoformat()
    row["token"] = kwargs.get("token") or kwargs.get("mint", "")
    for col in RULE_OBS_COLUMNS:
        if col in ("timestamp", "token"):
            continue
        val = kwargs.get(col)
        if val is not None:
            row[col] = val

    with _csv_lock:
        file_exists = RULE_OBS_PATH.exists()
        with open(RULE_OBS_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RULE_OBS_COLUMNS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


# ── Feature engine ─────────────────────────────────────────────────────────────
def compute_features(mint: str, start_time: float, trades: List[Dict]) -> Optional[Dict]:
    """Return feature dict computed from trades in the first 30 minutes."""
    cutoff = start_time + 30 * 60
    early  = [t for t in trades if t["ts"] <= cutoff]
    if not early:
        return None

    buys  = [t for t in early if t["side"] == "buy"]
    sells = [t for t in early if t["side"] == "sell"]

    total_vol  = sum(t["usd"] for t in early)
    n_trades   = len(early)
    buy_vol    = sum(t["usd"] for t in buys)
    sell_vol   = sum(t["usd"] for t in sells)
    buy_sell_r = buy_vol / sell_vol if sell_vol > 0 else float(buy_vol)
    max_buy    = max((t["usd"] for t in buys), default=0.0)
    avg_size   = total_vol / n_trades if n_trades else 0.0

    wallet_vol: dict = {}
    for t in early:
        wallet_vol[t["wallet"]] = wallet_vol.get(t["wallet"], 0.0) + t["usd"]
    sorted_wallet_vols = sorted(wallet_vol.values(), reverse=True)
    top1_pct = sorted_wallet_vols[0] / total_vol if total_vol > 0 and sorted_wallet_vols else 0.0
    top3_pct = sum(sorted_wallet_vols[:3]) / total_vol if total_vol > 0 and sorted_wallet_vols else 0.0

    trade_sizes = [t["usd"] for t in early if t["usd"] > 0]
    if len(trade_sizes) > 1:
        _mean_sz = sum(trade_sizes) / len(trade_sizes)
        _var = sum((x - _mean_sz) ** 2 for x in trade_sizes) / len(trade_sizes)
        trade_size_cv = (_var ** 0.5) / _mean_sz if _mean_sz > 0 else 0.0
    else:
        trade_size_cv = 0.0

    dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
    feats = {
        "volume_first_30min":    total_vol,
        "n_trades_30min":        n_trades,
        "unique_buyers_30min":   len({t["wallet"] for t in buys}),
        "unique_sellers_30min":  len({t["wallet"] for t in sells}),
        "buy_sell_ratio_30min":  buy_sell_r,
        "max_single_buy_30min":  max_buy,
        "avg_trade_size_30min":  avg_size,
        "top1_wallet_pct_30min": top1_pct,
        "top3_wallet_pct_30min": top3_pct,
        "trade_size_cv_30min":   trade_size_cv,
        "launch_hour_utc":       dt.hour,
        "launch_dayofweek":      dt.weekday(),
        "launch_month":          dt.month,
    }

    # Engineered features (same as training pipeline)
    buyers  = feats["unique_buyers_30min"]
    n_tr    = feats["n_trades_30min"]
    volume  = feats["volume_first_30min"]
    max_buy = feats["max_single_buy_30min"]

    feats["buyers_per_trade"]    = buyers / (n_tr + 1)
    feats["trades_per_buyer"]    = n_tr / (buyers + 1)
    feats["volume_per_buyer"]    = volume / (buyers + 1)
    feats["volume_per_trade"]    = volume / (n_tr + 1)
    feats["whale_dominance"]     = max_buy / (volume + 1)
    feats["buyer_seller_ratio"]  = buyers / (feats["unique_sellers_30min"] + 1)
    feats["buyers_per_volume"]   = buyers / (volume + 1)
    feats["trades_per_min"]      = n_tr / 30
    feats["volume_per_min"]      = volume / 30
    feats["log_trades"]          = np.log1p(n_tr)
    feats["log_buyers"]          = np.log1p(buyers)
    feats["log_volume"]          = np.log1p(volume)
    feats["log_max_buy"]         = np.log1p(max_buy)

    return feats


# ── Filter layer ───────────────────────────────────────────────────────────────
def passes_filters(feats: Dict) -> Tuple[bool, str, str]:
    """Returns (passed, short_code, detail_message)."""
    if feats["volume_first_30min"] < MIN_VOLUME_30MIN:
        return False, "low_volume",    f"volume={feats['volume_first_30min']:.0f}<{MIN_VOLUME_30MIN}"
    if feats["unique_buyers_30min"] < MIN_UNIQUE_BUYERS:
        return False, "low_buyers",    f"buyers={feats['unique_buyers_30min']}<{MIN_UNIQUE_BUYERS}"
    if feats["avg_trade_size_30min"] < MIN_AVG_TRADE_SIZE:
        return False, "low_avg_size",  f"avg_size={feats['avg_trade_size_30min']:.1f}<{MIN_AVG_TRADE_SIZE}"
    if feats["buy_sell_ratio_30min"] < MIN_BUY_SELL_RATIO:
        return False, "low_buy_sell",  f"buy_sell={feats['buy_sell_ratio_30min']:.2f}<{MIN_BUY_SELL_RATIO}"
    return True, "ok", "ok"


# ── ML scoring ─────────────────────────────────────────────────────────────────
def score(feats: dict) -> float:
    if model is None:
        return 0.0
    X = pd.DataFrame([feats])[model_features].fillna(0)
    return float(model.predict_proba(X)[0][1])


# ── PnL ───────────────────────────────────────────────────────────────────────
def calc_pnl(
    entry_price: float, current_price: float, size_usd: float
) -> Tuple[float, float]:
    pnl_pct = (current_price - entry_price) / entry_price
    pnl_usd = pnl_pct * size_usd
    return pnl_pct, pnl_usd


def compute_rule_signal_metrics(trades: List[Dict], signal_unique_buyers: int = RULE_SIGNAL_UNIQUE_BUYERS) -> Optional[Dict[str, float]]:
    buys = [t for t in trades if t["side"] == "buy" and t.get("wallet")]
    if not buys:
        return None

    first_buy_by_wallet: Dict[str, Dict] = {}
    for t in buys:
        w = t["wallet"]
        prev = first_buy_by_wallet.get(w)
        if prev is None or t["ts"] < prev["ts"]:
            first_buy_by_wallet[w] = t

    ordered = sorted(first_buy_by_wallet.values(), key=lambda x: x["ts"])
    if len(ordered) < signal_unique_buyers:
        return None

    signal_trade = ordered[signal_unique_buyers - 1]
    signal_time = signal_trade["ts"]

    to_signal = [t for t in trades if t["ts"] <= signal_time]
    total_vol = sum(float(t.get("usd", 0.0)) for t in to_signal)
    buy_vol = sum(float(t.get("usd", 0.0)) for t in to_signal if t.get("side") == "buy")
    buy_volume_share_to_signal = (buy_vol / total_vol) if total_vol > 0 else 0.0

    clean_post_signal_buys = [
        t for t in buys
        if t["ts"] >= signal_time and float(t.get("usd", 0.0)) >= RULE_MIN_CLEAN_TRADE_USD and float(t.get("price", 0.0)) > 0
    ]
    if not clean_post_signal_buys:
        return None

    entry_trade = sorted(clean_post_signal_buys, key=lambda x: x["ts"])[0]
    entry_price = float(entry_trade["price"])
    # `price` is stored as SOL/token, so convert the implied 1B-token market cap to USD
    # before comparing against USD-calibrated policy thresholds.
    entry_mcap_clean = entry_price * 1e9 * get_sol_price()

    minutes_to_signal = (signal_time - trades[0]["ts"]) / 60.0 if trades else 0.0
    return {
        "signal_time": signal_time,
        "buy_volume_share_to_signal": buy_volume_share_to_signal,
        "entry_trade_time": entry_trade["ts"],
        "entry_price": entry_price,
        "entry_mcap_clean": entry_mcap_clean,
        "minutes_to_signal": minutes_to_signal,
        "unique_buyers_at_signal": signal_unique_buyers,
    }


# ── Exit logic ─────────────────────────────────────────────────────────────────
def evaluate_exit(mint: str, pos: dict, current_price: float, now: float) -> None:
    """Check exit conditions for an open position; act if one is triggered."""
    if pos.get("closed"):
        return

    entry_price      = pos["entry_price"]
    size_usd         = pos["size_usd"] * pos.get("remaining_pct", 1.0)
    age_min          = (now - pos["start_time"]) / 60
    trade_id         = pos.get("trade_id", "")

    # Resolve fill_age_min from on-chain BUY confirm time (opened_at in live state).
    # Cached in pos["opened_at_s"] after first successful lookup so file I/O happens once.
    if "opened_at_s" not in pos:
        try:
            from trading.execution.state import get_all_positions as _get_live_positions
            _lpos = _get_live_positions().get(trade_id)
            if _lpos and _lpos.get("opened_at"):
                from datetime import datetime as _dt
                pos["opened_at_s"] = _dt.fromisoformat(_lpos["opened_at"]).timestamp()
        except Exception:
            pass
    _opened_at_s = pos.get("opened_at_s")
    fill_age_min = (now - _opened_at_s) / 60 if _opened_at_s else age_min
    entry_ts         = pos.get("entry_ts", pos["start_time"])
    pnl_pct, pnl_usd = calc_pnl(entry_price, current_price, size_usd)
    gross_multiple   = current_price / entry_price if entry_price > 0 else 0.0
    net_multiple_est = (current_price * 0.95) / (entry_price * 1.05) if entry_price > 0 else 0.0
    concurrent_open_positions = len(open_positions)
    entry_mcap_clean = float(pos.get("entry_mcap_clean", 0.0) or 0.0)
    fast_stop_loss_pct = (-0.15 if entry_mcap_clean >= 8000 else -0.25) if (pos.get("trading_mode") == "rule_based" and entry_mcap_clean > 0 and entry_mcap_clean < 9000) else None

    if pos.get("trading_mode") == "rule_based":
        action = reason = None
        tp_multiple = float(pos.get("tp_multiple", RULE_TP_MULTIPLE))
        timeout_minutes = float(pos.get("timeout_minutes", RULE_TIMEOUT_MINUTES))
        mcap_lt8k = entry_mcap_clean > 0 and entry_mcap_clean < 8000
        mcap_8k_9k = entry_mcap_clean >= 8000 and entry_mcap_clean < 9000

        lt8k_early_90s = mcap_lt8k and fill_age_min >= 1.5 and gross_multiple < 1.08
        lt8k_early_180s = mcap_lt8k and fill_age_min >= 3.0 and gross_multiple < 1.16
        lt8k_early_300s = mcap_lt8k and fill_age_min >= 5.0 and gross_multiple < 1.22

        core_8k9k_early_180s = mcap_8k_9k and fill_age_min >= 3.0 and gross_multiple < 1.05
        core_8k9k_early_300s = mcap_8k_9k and fill_age_min >= 5.0 and gross_multiple < 1.12
        core_8k9k_early_420s = mcap_8k_9k and fill_age_min >= 7.0 and gross_multiple < 1.20

        if fast_stop_loss_pct is not None and pnl_pct <= fast_stop_loss_pct:
            action, reason = "SELL_ALL", f"fast_stop_low_mcap pnl={pnl_pct:.2%}"
        elif lt8k_early_300s:
            action, reason = "SELL_ALL", f"lt8k_early_failure_5m gm={gross_multiple:.3f}"
        elif core_8k9k_early_420s:
            action, reason = "SELL_ALL", f"core_8k9k_early_failure_7m gm={gross_multiple:.3f}"
        elif lt8k_early_180s:
            action, reason = "SELL_ALL", f"lt8k_early_failure_3m gm={gross_multiple:.3f}"
        elif core_8k9k_early_300s:
            action, reason = "SELL_ALL", f"core_8k9k_early_failure_5m gm={gross_multiple:.3f}"
        elif lt8k_early_90s:
            action, reason = "SELL_ALL", f"lt8k_early_failure_90s gm={gross_multiple:.3f}"
        elif core_8k9k_early_180s:
            action, reason = "SELL_ALL", f"core_8k9k_early_failure_3m gm={gross_multiple:.3f}"
        elif gross_multiple >= tp_multiple:
            action, reason = "SELL_ALL", f"tp_{tp_multiple:.2f}x"
        elif age_min >= timeout_minutes:
            action, reason = "SELL_ALL", f"timeout_{int(timeout_minutes)}m"

        if action is None:
            return

        realized_usd = pnl_pct * pos["size_usd"]
        log.info(f"[EXIT] {mint[:8]}  {action}  {reason}  pnl_usd={realized_usd:+.2f}")
        log_csv(
            mint=mint,
            action=action,
            reason=reason,
            trade_id=trade_id,
            trading_mode="rule_based",
            policy_track=pos.get("policy_track", "baseline_070"),
            entry_price=entry_price,
            exit_price=current_price,
            gross_multiple=round(gross_multiple, 6),
            net_multiple_est=round(net_multiple_est, 6),
            pnl_pct=round(pnl_pct, 6),
            pnl_usd=round(realized_usd, 2),
            position_size_usd=pos["size_usd"],
            buy_volume_share_to_signal=round(pos.get("buy_volume_share_to_signal", 0.0), 6),
            entry_mcap_clean=round(pos.get("entry_mcap_clean", 0.0), 2),
            concurrent_open_positions=concurrent_open_positions,
            signal_time=datetime.fromtimestamp(pos.get("signal_time", entry_ts), tz=timezone.utc).isoformat(),
            entry_time=datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
            exit_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            score=round(pos.get("score", 0.0), 6),
            p_win=round(pos.get("p_win", 0.0), 6),
            ev=round(pos.get("ev", 0.0), 6),
        )
        pos["closed"] = True
        with positions_lock:
            open_positions.pop(mint, None)

        # ── Phase 2/3: dry-run shadow (no-op in paper mode) ──────────────────
        # Pass trade_id so Phase 3 state store can close the matching position.
        _emit_dry_run_sell(
            mint=mint,
            track_name=pos.get("policy_track", "baseline_070"),
            stake_usd=pos.get("size_usd", 0.0),
            entry_mcap=float(pos.get("entry_mcap_clean", 0.0)),
            pressure=float(pos.get("buy_volume_share_to_signal", 0.0)),
            trade_id=pos.get("trade_id", ""),
        )

        # ── Phase 4: micro_live guarded path (no-op unless explicitly configured)
        # Phase 4.1: pass current_price as signal_price and now as signal_time_s.
        # Exit signals fire and execute in the same tick so quote age ≈ 0 ms.
        _emit_micro_live_sell(
            mint          = mint,
            track_name    = pos.get("policy_track", "baseline_070"),
            stake_usd     = pos.get("size_usd", 0.0),
            entry_mcap    = float(pos.get("entry_mcap_clean", 0.0)),
            pressure      = float(pos.get("buy_volume_share_to_signal", 0.0)),
            trade_id      = pos.get("trade_id", ""),
            signal_price  = current_price,
            signal_time_s = now,
        )
        return

    # legacy ML mode below
    action = reason = None
    if pnl_pct <= STOP_LOSS_PCT:
        action, reason = "SELL_ALL",    f"stop_loss pnl={pnl_pct:.2%}"
    elif pnl_pct >= TAKE_PROFIT_2 and pos["tp1_hit"]:
        action, reason = "SELL_REST",   f"take_profit_2 pnl={pnl_pct:.2%}"
    elif pnl_pct >= TAKE_PROFIT_1 and not pos["tp1_hit"]:
        action, reason = "SELL_50_PCT", f"take_profit_1 pnl={pnl_pct:.2%}"
    elif age_min >= TIME_EXIT_MINUTES:
        action, reason = "SELL_ALL",    f"time_exit age={age_min:.1f}min"

    if action is None:
        return

    s_pos = pos.get("score", pos.get("p_win", 0.0))
    ev_pos = s_pos * B_MEDIAN - (1 - s_pos) * LOSS

    if action == "SELL_50_PCT":
        sold_frac            = pos["remaining_pct"] * 0.50
        realized_usd         = pnl_pct * pos["size_usd"] * sold_frac
        pos["remaining_pct"] -= sold_frac
        pos["tp1_hit"]        = True
        log.info(f"[EXIT] {mint[:8]}  {action}  {reason}  pnl_usd={realized_usd:+.2f}")
        log_csv(
            mint=mint, action=action, reason=reason,
            trade_id=trade_id, trading_mode="ml_ranking",
            entry_price=entry_price, exit_price=current_price,
            gross_multiple=round(gross_multiple, 6),
            net_multiple_est=round(net_multiple_est, 6),
            pnl_pct=round(pnl_pct, 6), pnl_usd=round(realized_usd, 2),
            position_size_usd=pos["size_usd"],
            concurrent_open_positions=concurrent_open_positions,
            score=s_pos, p_win=s_pos, ev=round(ev_pos, 6),
            entry_time=datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
            exit_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        )
    else:
        realized_usd = pnl_pct * pos["size_usd"] * pos["remaining_pct"]
        log.info(f"[EXIT] {mint[:8]}  {action}  {reason}  pnl_usd={realized_usd:+.2f}")
        log_csv(
            mint=mint, action=action, reason=reason,
            trade_id=trade_id, trading_mode="ml_ranking",
            entry_price=entry_price, exit_price=current_price,
            gross_multiple=round(gross_multiple, 6),
            net_multiple_est=round(net_multiple_est, 6),
            pnl_pct=round(pnl_pct, 6), pnl_usd=round(realized_usd, 2),
            position_size_usd=pos["size_usd"],
            concurrent_open_positions=concurrent_open_positions,
            score=s_pos, p_win=s_pos, ev=round(ev_pos, 6),
            entry_time=datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
            exit_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        )
        pos["closed"] = True
        with positions_lock:
            open_positions.pop(mint, None)


# ── Live-position orphan supervisor (paper→live race fix) ─────────────────────

def _live_position_age_s(opened_at_iso: str, now_s: float) -> float:
    """Return seconds since the live position was opened. Safe — returns 0 on error."""
    try:
        opened_at = datetime.fromisoformat(opened_at_iso)
        return now_s - opened_at.timestamp()
    except Exception:
        return 0.0


def _supervise_live_orphans(now: float) -> None:
    """Detect and resolve live open positions that have no paper position shadow.

    These "orphans" arise from the paper→live race condition: paper
    evaluate_exit() fires and removes the paper position before the live BUY
    fill is confirmed, leaving the live position with no supervisor.

    This function runs every 10 seconds as part of position_monitor_loop() and
    serves as a backup to the primary resolution path
    (adapter._safe_check_pending_exit, which fires synchronously on BUY fill).

    Actions taken per orphaned position:
      • pending exit found  → pop it atomically and re-emit the SELL (handles
                               process restarts and callback failures).
      • no pending exit, age < 30 s  → likely fill propagation; log only.
      • no pending exit, age 30-600 s → log WARNING (callback may have fired
                                         already and SELL is in-flight or done).
      • no pending exit, age > 600 s → log ERROR; manual intervention required.

    Repeated CRITICAL alerts for the same orphan are rate-limited so a known
    safe-mode blocker does not drown fresher signals in the log stream.

    Stale pending exits (queued > 2 min, no matching open position — e.g. BUY
    was DENIED/FAILED) are cleaned up to prevent unbounded growth.
    """
    if not _MICRO_LIVE_ACTIVE or _EXEC_CONFIG is None:
        return

    live_open_trade_ids: set[str] = set()

    try:
        from trading.execution.state import (
            get_open_positions as _get_live_open,
            get_pending_exits  as _get_pending_exits,
            get_all_positions  as _get_all_positions,
            pop_pending_exit   as _pop_pending_exit,
        )
    except Exception as _e:
        log.warning("[ORPHAN SUPERVISOR] state import failed: %s", _e)
        return

    # ── 1. Resolve orphaned open positions ────────────────────────────────────
    try:
        live_open = _get_live_open()
    except Exception as _e:
        log.warning("[ORPHAN SUPERVISOR] failed to load live open positions: %s", _e)
        return

    if live_open:
        with positions_lock:
            paper_mints = set(open_positions.keys())

        try:
            pending_exits = _get_pending_exits()
        except Exception as _e:
            log.warning("[ORPHAN SUPERVISOR] failed to load pending exits: %s", _e)
            pending_exits = {}

        for lpos in live_open:
            trade_id = lpos["trade_id"]
            mint     = lpos["token"]
            live_open_trade_ids.add(trade_id)

            if mint in paper_mints:
                continue  # Paper shadow is active — normal, skip.

            age_s = _live_position_age_s(lpos.get("opened_at", ""), now)

            if trade_id in pending_exits:
                # Primary path missed the pending exit (e.g. process restarted
                # between BUY fill and callback). Pop it atomically and re-emit.
                pending = _pop_pending_exit(trade_id)
                if pending is None:
                    # Another thread just consumed it — BUY fill callback fired.
                    log.info(
                        "[ORPHAN SUPERVISOR] trade_id=%s: pending exit consumed "
                        "concurrently by BUY fill callback — no action needed",
                        trade_id,
                    )
                    continue

                log.warning(
                    "[ORPHAN SUPERVISOR] trade_id=%s  mint=%s  age=%.1fs — "
                    "pending exit found (queued_at=%s), re-emitting deferred SELL",
                    trade_id, mint[:8], age_s, pending.get("queued_at", "?"),
                )
                _emit_micro_live_sell(
                    mint          = mint,
                    track_name    = lpos.get("policy_track",
                                             pending.get("track_name", "unknown")),
                    stake_usd     = float(lpos.get("size_usd",
                                                   pending.get("stake_usd", 0.0))),
                    entry_mcap    = float(lpos.get("entry_mcap",
                                                   pending.get("entry_mcap", 0.0))),
                    pressure      = float(lpos.get("pressure",
                                                   pending.get("pressure", 0.0))),
                    trade_id      = trade_id,
                    signal_price  = float(pending.get("signal_price", 0.0)),
                    signal_time_s = now,  # refresh — quote must not be stale
                )

            elif age_s > 600:
                last_critical_at = _ORPHAN_CRITICAL_LOGGED_AT.get(trade_id)
                if (
                    last_critical_at is None
                    or (now - last_critical_at) >= _ORPHAN_CRITICAL_REMINDER_S
                ):
                    _ORPHAN_CRITICAL_LOGGED_AT[trade_id] = now
                    log.error(
                        "[ORPHAN SUPERVISOR] CRITICAL: trade_id=%s  mint=%s  "
                        "age=%.0fs — live position open with no paper shadow and no "
                        "pending exit. BUY fill callback should have resolved this. "
                        "MANUAL INTERVENTION REQUIRED. "
                        "(repeat reminders every %.0fs while unresolved)",
                        trade_id, mint[:8], age_s, _ORPHAN_CRITICAL_REMINDER_S,
                    )
            elif age_s > 30:
                log.warning(
                    "[ORPHAN SUPERVISOR] trade_id=%s  mint=%s  age=%.0fs — "
                    "live position open, no paper shadow, no pending exit. "
                    "BUY fill callback likely already consumed pending exit "
                    "and SELL is in-flight or completed.",
                    trade_id, mint[:8], age_s,
                )
            else:
                log.info(
                    "[ORPHAN SUPERVISOR] trade_id=%s  mint=%s  age=%.0fs — "
                    "live position open, no paper shadow yet "
                    "(BUY fill in-flight, expected)",
                    trade_id, mint[:8], age_s,
                )

    # Forget escalation memory for positions that are no longer open.
    for known_trade_id in list(_ORPHAN_CRITICAL_LOGGED_AT):
        if known_trade_id not in live_open_trade_ids:
            _ORPHAN_CRITICAL_LOGGED_AT.pop(known_trade_id, None)

    # ── 2. Clean up stale pending exits with no matching open position ─────────
    # Handles the case where the live BUY was DENIED/FAILED — the pending exit
    # would otherwise sit in state forever.
    try:
        all_pending = _get_pending_exits()
        if not all_pending:
            return
        all_positions = _get_all_positions()
    except Exception as _e:
        log.warning("[ORPHAN SUPERVISOR] stale-exit cleanup: state read failed: %s", _e)
        return

    for pending_tid, pending_info in list(all_pending.items()):
        live_pos = all_positions.get(pending_tid)
        if live_pos is not None and live_pos.get("status") == "open":
            continue  # open position exists — will be resolved above

        queued_at_iso = pending_info.get("queued_at", "")
        try:
            queued_age_s = _live_position_age_s(queued_at_iso, now)
        except Exception:
            queued_age_s = 0.0

        if queued_age_s > 120:
            cleaned = _pop_pending_exit(pending_tid)  # noqa: F841 — side-effect
            if cleaned is not None:
                log.warning(
                    "[ORPHAN SUPERVISOR] cleaned stale pending exit  "
                    "trade_id=%s  mint=%s  age=%.0fs — "
                    "no matching open live position (BUY likely DENIED/FAILED)",
                    pending_tid,
                    str(pending_info.get("mint", "?"))[:8],
                    queued_age_s,
                )


# ── Position monitor (background thread) ──────────────────────────────────────
def position_monitor_loop() -> None:
    """Poll open positions every 10 seconds and evaluate exit conditions.

    Also runs the live-position orphan supervisor (_supervise_live_orphans)
    on each tick to detect and recover from the paper→live race condition.
    """
    log.info("Position monitor started.")
    while True:
        time.sleep(10)
        now = time.time()
        with positions_lock:
            mints = list(open_positions.keys())

        for mint in mints:
            with tokens_lock:
                state = tokens.get(mint)
            if not state:
                continue
            pos = state.get("position")
            if not pos or pos.get("closed"):
                continue

            trades = state["trades"]
            if not trades:
                continue

            # Use the most recent trade price as current price proxy
            current_price = trades[-1].get("price", pos["entry_price"])
            if current_price > 0:
                evaluate_exit(mint, pos, current_price, now)

        # Orphan supervisor: catch live positions that lost their paper shadow
        # (paper→live race condition) and re-fire any pending exits.
        _supervise_live_orphans(now)


# ── Ranking engine ─────────────────────────────────────────────────────────────
def _execute_buy(candidate: dict) -> None:
    """Open a paper position for a single candidate selected by the ranker."""
    mint             = candidate["token"]
    s                = candidate.get("score", candidate.get("p_win", 0.0))
    ev               = s * B_MEDIAN - (1 - s) * LOSS
    ranking_position = candidate.get("ranking_position", 1)
    ranking_gap      = candidate.get("ranking_gap")
    now              = time.time()

    with positions_lock:
        if len(open_positions) >= MAX_POSITIONS:
            log.info(f"[SKIP]  {mint[:8]}  max_positions at ranking time ({MAX_POSITIONS})")
            log_csv(mint=mint, score=s, p_win=s, ev=round(ev, 6), action="SKIP",
                    trading_mode="ml_ranking",
                    reason="ranking:max_positions",
                    ranking_position=ranking_position, ranking_gap=ranking_gap)
            return

    with tokens_lock:
        state = tokens.get(mint)
    if not state:
        log.warning(f"[SKIP]  {mint[:8]}  token state missing at ranking time")
        return

    trades      = state["trades"]
    entry_price = trades[-1].get("price", 0.0) if trades else 0.0
    if entry_price <= 0:
        log.warning(f"[SKIP]  {mint[:8]}  no valid entry price at ranking time")
        return

    size_usd = BANKROLL * POSITION_SIZE
    trade_id = make_trade_id(mint, now)
    pos = {
        "mint":          mint,
        "entry_price":   entry_price,
        "size_usd":      size_usd,
        "remaining_pct": 1.0,
        "tp1_hit":       False,
        "closed":        False,
        "start_time":    state["start_time"],
        "score":         s,
        "p_win":         s,
        "ev":            ev,
        "entry_ts":      now,
        "trade_id":      trade_id,
        "trading_mode":  "ml_ranking",
    }

    with tokens_lock:
        tokens[mint]["position"] = pos
    with positions_lock:
        open_positions[mint] = pos
        concurrent_open_positions = len(open_positions)

    log.info(
        f"[BUY ]  {mint[:8]}  selected via ranking  score={s:.3f}  ev={ev:+.4f}  "
        f"entry={entry_price:.8f}  size=${size_usd:.2f}"
    )
    log_csv(
        mint=mint, score=s, p_win=s, ev=round(ev, 6), action="BUY",
        trading_mode="ml_ranking",
        reason="ranking:selected",
        trade_id=trade_id, entry_price=entry_price, position_size_usd=size_usd,
        concurrent_open_positions=concurrent_open_positions,
        entry_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        ranking_position=ranking_position, ranking_gap=ranking_gap,
    )


def execute_rule_based_buy(mint: str, state: dict, signal: dict, score_hint: float = 0.0, track: Optional[Dict[str, Any]] = None) -> None:
    now = time.time()
    with positions_lock:
        if len(open_positions) >= MAX_POSITIONS:
            log.info(f"[SKIP]  {mint[:8]}  max_positions reached ({MAX_POSITIONS})")
            log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=(track or {}).get("name", "baseline_070"),
                    reason="rule:max_positions",
                    buy_volume_share_to_signal=round(signal.get("buy_volume_share_to_signal", 0.0), 6),
                    entry_mcap_clean=round(signal.get("entry_mcap_clean", 0.0), 2))
            return
        if mint in open_positions:
            return

    entry_price = float(signal.get("entry_price", 0.0))
    if entry_price <= 0:
        log.info(f"[SKIP]  {mint[:8]}  invalid rule-based entry price")
        return

    stake_usd = float((track or {}).get("stake_usd", FIXED_STAKE_USD))
    trade_id = make_trade_id(mint, now)
    pos = {
        "mint": mint,
        "entry_price": entry_price,
        "size_usd": stake_usd,
        "remaining_pct": 1.0,
        "tp1_hit": False,
        "closed": False,
        "start_time": signal.get("entry_trade_time", now),
        "signal_time": signal.get("signal_time", now),
        "entry_ts": now,
        "trade_id": trade_id,
        "trading_mode": "rule_based",
        "policy_track": (track or {}).get("name", "baseline_070"),
        "buy_volume_share_to_signal": signal.get("buy_volume_share_to_signal", 0.0),
        "entry_mcap_clean": signal.get("entry_mcap_clean", 0.0),
        "score": score_hint,
        "p_win": score_hint,
        "ev": 0.0,
        "tp_multiple": (track or {}).get("tp_multiple", RULE_TP_MULTIPLE),
        "timeout_minutes": (track or {}).get("timeout_minutes", RULE_TIMEOUT_MINUTES),
    }

    with tokens_lock:
        tokens[mint]["position"] = pos
    with positions_lock:
        open_positions[mint] = pos
        concurrent_open_positions = len(open_positions)

    log.info(
        f"[BUY ]  {mint[:8]}  rule_based  pressure={signal.get('buy_volume_share_to_signal', 0.0):.3f}  "
        f"entry_mcap={signal.get('entry_mcap_clean', 0.0):.0f}  size=${stake_usd:.2f}"
    )
    log_csv(
        mint=mint,
        action="BUY",
        reason="rule:selected",
        trade_id=trade_id,
        trading_mode="rule_based",
        policy_track=(track or {}).get("name", "baseline_070"),
        score=round(score_hint, 6),
        p_win=round(score_hint, 6),
        ev=0.0,
        entry_price=entry_price,
        position_size_usd=stake_usd,
        buy_volume_share_to_signal=round(signal.get("buy_volume_share_to_signal", 0.0), 6),
        entry_mcap_clean=round(signal.get("entry_mcap_clean", 0.0), 2),
        concurrent_open_positions=concurrent_open_positions,
        age_min=round((now - state["start_time"]) / 60, 2),
        signal_time=datetime.fromtimestamp(signal.get("signal_time", now), tz=timezone.utc).isoformat(),
        signal_hour_utc=datetime.fromtimestamp(signal.get("signal_time", now), tz=timezone.utc).hour,
        entry_time=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    )

    # ── Phase 2/3: dry-run shadow (no-op in paper mode) ──────────────────────
    # Pass trade_id so Phase 3 state store cross-references the paper position.
    _emit_dry_run_buy(
        mint=mint,
        track_name=(track or {}).get("name", "baseline_070"),
        stake_usd=stake_usd,
        entry_mcap=float(signal.get("entry_mcap_clean", 0.0)),
        pressure=float(signal.get("buy_volume_share_to_signal", 0.0)),
        trade_id=trade_id,
    )

    # ── Phase 4: micro_live guarded path (no-op unless explicitly configured)
    # Phase 4.1: pass signal_price and the timestamp of that price snapshot so
    # quote_metadata reflects the actual entry quote, not the earlier buyer-count
    # signal threshold. Using entry_trade_time trims false quote_stale denials
    # caused by the gap between "signal formed" and "clean entry trade seen".
    _emit_micro_live_buy(
        mint          = mint,
        track_name    = (track or {}).get("name", "baseline_070"),
        stake_usd     = stake_usd,
        entry_mcap    = float(signal.get("entry_mcap_clean", 0.0)),
        pressure      = float(signal.get("buy_volume_share_to_signal", 0.0)),
        trade_id      = trade_id,
        signal_price  = entry_price,
        signal_time_s = signal.get("entry_trade_time", signal.get("signal_time", now)),
    )


def _run_ranking() -> None:
    """Drain the candidate pool, rank by score DESC, apply edge filter, buy top N."""

    # ── Data layer: write one inference log row per candidate at final decision ─
    def _dl_log(c: dict, dec: str, reason: str) -> None:
        if _dl_write_decision is not None:
            try:
                _dl_write_decision(
                    c["token"], c.get("_snap_id"), c.get("_feats", {}),
                    c.get("score", 0.0), c.get("_ev", 0.0), dec, reason,
                )
            except Exception as _e:
                log.warning(f"[dl] write_final_decision: {_e}")

    with candidates_lock:
        if not candidates:
            return
        pool = list(candidates)
        candidates.clear()

    ranked = sorted(pool, key=lambda c: c.get("score", c.get("p_win", 0.0)), reverse=True)

    scores_this_window = [c.get("score", c.get("p_win", 0.0)) for c in ranked]
    score_top1 = scores_this_window[0]
    score_top2 = scores_this_window[1] if len(ranked) >= 2 else None

    # ── Window stats ──────────────────────────────────────────────────────────
    win_min  = min(scores_this_window)
    win_max  = max(scores_this_window)
    win_mean = sum(scores_this_window) / len(scores_this_window)
    pct      = _score_percentiles() or {}
    log.info(
        f"--- RANKING STATS  n={len(ranked)}  "
        f"min={win_min:.6f}  max={win_max:.6f}  mean={win_mean:.6f}  "
        f"p50={pct.get('score_p50', float('nan')):.4f}  "
        f"p75={pct.get('score_p75', float('nan')):.4f}  "
        f"p90={pct.get('score_p90', float('nan')):.4f}  "
        f"p95={pct.get('score_p95', float('nan')):.4f} ---"
    )
    log_csv(
        mint="__stats__", action="RANKING_STATS", reason="window_summary",
        score_min=round(win_min, 6), score_max=round(win_max, 6),
        score_mean=round(win_mean, 6),
        **{k: round(v, 6) for k, v in pct.items()},
    )

    # ── Sorted ranking ────────────────────────────────────────────────────────
    log.info("--- RANKING ---")
    for i, c in enumerate(ranked, 1):
        s_i = c.get("score", c.get("p_win", 0.0))
        log.info(f"  {i}. {c['token'][:8]}  →  {s_i:.6f}")

    # ── Edge check ────────────────────────────────────────────────────────────
    log.info("--- EDGE CHECK ---")

    if score_top2 is None:
        # Only one candidate — no comparison possible, skip
        log.info("  only 1 candidate — no edge comparison possible")
        log.info("--- DECISION ---")
        log.info(f"  SKIP → reason: only 1 candidate in window")
        for c in ranked:
            s_c = c.get("score", c.get("p_win", 0.0))
            ev_c = s_c * B_MEDIAN - (1 - s_c) * LOSS
            log_csv(mint=c["token"], score=s_c, p_win=s_c, ev=round(ev_c, 6),
                    action="SKIP", reason="ranking:only_one_candidate",
                    ranking_position=1, ranking_gap=None)
            _dl_log(c, "SKIP", "ranking:only_one_candidate")
        return

    ratio          = score_top1 / score_top2 if score_top2 > 0 else float("inf")
    passes_ratio   = ratio >= EDGE_RATIO
    passes_min     = score_top1 >= MIN_SCORE_THRESHOLD

    log.info(f"  score_top1      = {score_top1:.6f}")
    log.info(f"  score_top2      = {score_top2:.6f}")
    log.info(f"  ratio           = {ratio:.2f}  (need >= {EDGE_RATIO})")
    log.info(f"  passes_ratio    = {passes_ratio}")
    log.info(f"  passes_min_score= {passes_min}  (need >= {MIN_SCORE_THRESHOLD:.0e})")

    # ── Decision ──────────────────────────────────────────────────────────────
    log.info("--- DECISION ---")

    if passes_ratio and passes_min:
        log.info(f"  BUY {ranked[0]['token'][:8]}")
        ranked[0]["ranking_position"] = 1
        ranked[0]["ranking_gap"]      = round(ratio, 4)
        _dl_log(ranked[0], "BUY", "ranking:selected")
        _execute_buy(ranked[0])
        for i, c in enumerate(ranked[1:], 2):
            s_c  = c.get("score", c.get("p_win", 0.0))
            ev_c = s_c * B_MEDIAN - (1 - s_c) * LOSS
            log.info(f"[SKIP]  {c['token'][:8]}  not selected  score={s_c:.6f}")
            log_csv(mint=c["token"], score=s_c, p_win=s_c, ev=round(ev_c, 6),
                    action="SKIP", reason=f"ranking:not_in_top_{RANKING_TOP_N}",
                    ranking_position=i, ranking_gap=round(ratio, 4))
            _dl_log(c, "SKIP", f"ranking:not_in_top_{RANKING_TOP_N}")
    else:
        if not passes_ratio:
            skip_reason = f"ranking:low_conviction  ratio={ratio:.2f}<{EDGE_RATIO}"
        else:
            skip_reason = f"ranking:below_min_score  score={score_top1:.2e}<{MIN_SCORE_THRESHOLD:.0e}"
        log.info(f"  SKIP → reason: {skip_reason}")
        for i, c in enumerate(ranked, 1):
            s_c  = c.get("score", c.get("p_win", 0.0))
            ev_c = s_c * B_MEDIAN - (1 - s_c) * LOSS
            log_csv(mint=c["token"], score=s_c, p_win=s_c, ev=round(ev_c, 6),
                    action="SKIP", reason=skip_reason,
                    ranking_position=i, ranking_gap=round(ratio, 4))
            _dl_log(c, "SKIP", skip_reason)


def ranking_loop() -> None:
    """Background thread: trigger ranking every RANKING_INTERVAL seconds."""
    log.info("Ranking loop started.")
    while True:
        time.sleep(RANKING_INTERVAL)
        _run_ranking()


# ── Evaluation & entry ─────────────────────────────────────────────────────────
def maybe_evaluate(mint: str) -> None:
    """Evaluate a token for entry using the active trading mode."""
    with tokens_lock:
        state = tokens.get(mint)
    if state is None or state["evaluated"]:
        return

    now     = time.time()
    age_min = (now - state["start_time"]) / 60
    feats = compute_features(mint, state["start_time"], state["trades"])
    _snap_id = None

    def _dl_eval(dec: str, reason: str, score: float = 0.0, ev: float = 0.0) -> None:
        if _dl_write_decision is not None:
            try:
                _dl_write_decision(mint, _snap_id, feats or {}, score, ev, dec, reason)
            except Exception as _e:
                log.warning(f"[dl] write_final_decision: {_e}")

    if feats is None:
        return

    if _dl_snapshot is not None:
        try:
            _snap_id = _dl_snapshot(mint, state["start_time"], state["trades"])
        except Exception as _e:
            log.warning(f"[dl] snapshot: {_e}")

    feature_kw = {
        "unique_buyers": feats["unique_buyers_30min"],
        "avg_trade_size": round(feats["avg_trade_size_30min"], 2),
        "buy_sell_ratio": round(feats["buy_sell_ratio_30min"], 4),
        "volume_first_30min": round(feats["volume_first_30min"], 2),
        "top1_wallet_pct_30min": round(feats.get("top1_wallet_pct_30min", 0.0), 4),
        "top3_wallet_pct_30min": round(feats.get("top3_wallet_pct_30min", 0.0), 4),
        "trades_per_minute": round(feats.get("trades_per_min", 0.0), 2),
        "trade_size_cv": round(feats.get("trade_size_cv_30min", 0.0), 4),
    }

    # Breadth velocity: unique buyers per minute in [-2min,-1min] and [-1min,now]
    _b_2_1 = len({t["wallet"] for t in state["trades"]
                  if (now - 120) <= t["ts"] < (now - 60) and t["side"] == "buy"})
    _b_1_0 = len({t["wallet"] for t in state["trades"]
                  if (now - 60) <= t["ts"] <= now and t["side"] == "buy"})
    _buyers_velocity = _b_1_0
    _buyers_acceleration = _b_1_0 - _b_2_1

    first_price = float(state["trades"][0].get("price", 0.0)) if state.get("trades") else 0.0
    last_price = float(state["trades"][-1].get("price", 0.0)) if state.get("trades") else 0.0

    obs_base = {
        "mint": mint,
        "token_age_min": round(age_min, 2),
        "unique_buyers_30min": feats["unique_buyers_30min"],
        "volume_first_30min": round(feats["volume_first_30min"], 2),
        "avg_trade_size": round(feats["avg_trade_size_30min"], 2),
        "buy_sell_ratio": round(feats["buy_sell_ratio_30min"], 4),
        "first_price_sol": round(first_price, 12) if first_price > 0 else "",
        "last_price_sol": round(last_price, 12) if last_price > 0 else "",
        "chg_first_to_last_pct": round(((last_price / first_price) - 1.0) * 100.0, 4) if first_price > 0 and last_price > 0 else "",
        "open_positions": len(open_positions),
    }

    # Hour/day exclusions based on paper performance analysis (v8_core_8k9k, n=395):
    #   UTC 04-07 any day:    WR=33%, EV≈0    (original H9 with larger dataset: EV negative)
    #   Fri 00-03 UTC:        WR=11.1%, EV=-$70  (n=9, catastrophic)
    #   Mon 20-23 UTC:        WR=28.6%, EV=-$16  (n=7, negative EV)
    _now_utc   = datetime.now(timezone.utc)
    _now_utc_hour = _now_utc.hour
    _now_utc_dow  = _now_utc.weekday()  # 0=Mon, 4=Fri

    _hour_day_excluded = (
        (4 <= _now_utc_hour <= 7) or
        (_now_utc_dow == 4 and 0 <= _now_utc_hour <= 3) or
        (_now_utc_dow == 0 and 20 <= _now_utc_hour <= 23)
    )
    if _hour_day_excluded:
        log_rule_obs(stage="token_skip", reason="rule:hour_day_excluded",
                     token_hour_utc=_now_utc_hour, token_dow=_now_utc_dow, **obs_base)
        return

    if TRADING_MODE == "rule_based":
        score_hint = score(feats) if model is not None else 0.0
        any_track_hit = False
        first_skip_reason = None

        log_rule_obs(stage="rule_eval_started", reason="rule:entered_evaluation", score_hint=round(score_hint, 6), **obs_base)

        any_signal_seen = False

        for track in RULE_TRACKS:
            signal = compute_rule_signal_metrics(state["trades"], signal_unique_buyers=track["buyers_min"])
            if signal is None:
                log_rule_obs(stage="track_no_signal", track=track["name"], reason="rule:signal_not_ready",
                             buyers_threshold=track["buyers_min"], pressure_min=track["pressure_min"], pressure_max=track["pressure_max"],
                             entry_mcap_min=track["entry_mcap_min"], entry_mcap_max=track["entry_mcap_max"],
                             clean_trade_usd_min=track["clean_trade_usd_min"], score_hint=round(score_hint, 6), **obs_base)
                continue

            any_signal_seen = True
            signal_age_min = (now - signal["signal_time"]) / 60.0
            pressure = signal["buy_volume_share_to_signal"]
            entry_mcap = signal["entry_mcap_clean"]
            signal_time_iso = datetime.fromtimestamp(signal['signal_time'], tz=timezone.utc).isoformat()
            clean_trades = [t for t in state["trades"] if t["ts"] >= signal["signal_time"] and float(t.get("usd", 0.0)) >= track["clean_trade_usd_min"] and float(t.get("price", 0.0)) > 0]
            signal_price = float(signal.get("entry_price", 0.0))
            obs_row = {
                **obs_base,
                "track": track["name"],
                "buyers_threshold": track["buyers_min"],
                "signal_unique_buyers": signal.get("unique_buyers_at_signal", track["buyers_min"]),
                "signal_age_min": round(signal_age_min, 2),
                "signal_time": signal_time_iso,
                "signal_price_sol": round(signal_price, 12) if signal_price > 0 else "",
                "chg_first_to_signal_pct": round(((signal_price / first_price) - 1.0) * 100.0, 4) if first_price > 0 and signal_price > 0 else "",
                "chg_signal_to_last_pct": round(((last_price / signal_price) - 1.0) * 100.0, 4) if signal_price > 0 and last_price > 0 else "",
                "pressure": round(pressure, 6),
                "pressure_min": track["pressure_min"],
                "pressure_max": track["pressure_max"],
                "entry_mcap_usd": round(entry_mcap, 2),
                "entry_mcap_min": track["entry_mcap_min"],
                "entry_mcap_max": track["entry_mcap_max"],
                "clean_trade_usd_min": track["clean_trade_usd_min"],
                "clean_trade_count": len(clean_trades),
                "score_hint": round(score_hint, 6),
            }

            if signal_age_min < 0 or signal_age_min > track["timeout_minutes"]:
                log_rule_obs(stage="track_signal_expired", reason="rule:signal_out_of_time", **obs_row)
                continue

            age_cap_s = track.get("signal_age_cap_s")
            if age_cap_s is not None and signal_age_min * 60 > age_cap_s:
                log_rule_obs(stage="track_skip", reason="rule:signal_age_cap_exceeded", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:signal_age_cap_exceeded"
                continue

            # Hallazgo 2026-06-10: cap on delay SEEN->signal (token_age at the
            # buyers-threshold trigger), distinct from signal_age_cap_s above
            # (which measures staleness of the signal at evaluation time).
            token_age_cap_s = track.get("token_age_cap_s")
            if token_age_cap_s is not None and signal["minutes_to_signal"] * 60.0 >= token_age_cap_s:
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="signal_age_exceeded", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso,
                        signal_hour_utc=datetime.fromtimestamp(signal["signal_time"], tz=timezone.utc).hour,
                        buyers_velocity=_buyers_velocity, buyers_acceleration=_buyers_acceleration,
                        **feature_kw)
                log_rule_obs(stage="track_skip", reason="signal_age_exceeded", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "signal_age_exceeded"
                continue

            if not (track["pressure_min"] <= pressure < track["pressure_max"]):
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="rule:pressure_out_of_range", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso, **feature_kw)
                log_rule_obs(stage="track_skip", reason="rule:pressure_out_of_range", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:pressure_out_of_range"
                continue
            if not (track["entry_mcap_min"] <= entry_mcap < track["entry_mcap_max"]):
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="rule:entry_mcap_out_of_range", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso, **feature_kw)
                log_rule_obs(stage="track_skip", reason="rule:entry_mcap_out_of_range", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:entry_mcap_out_of_range"
                continue
            if not clean_trades:
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="rule:clean_trade_not_found", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso, **feature_kw)
                log_rule_obs(stage="track_skip", reason="rule:clean_trade_not_found", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:clean_trade_not_found"
                continue
            mid_skip = track.get("mid_skip_s")
            if mid_skip is not None:
                mts_s = signal["minutes_to_signal"] * 60
                skip_lo, skip_hi = mid_skip
                if skip_lo <= mts_s <= skip_hi:
                    log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                            reason="rule:mid_group_skip", age_min=round(age_min, 2),
                            buy_volume_share_to_signal=round(pressure, 6),
                            entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                            signal_time=signal_time_iso, **feature_kw)
                    log_rule_obs(stage="track_skip", reason="rule:mid_group_skip", **obs_row)
                    if first_skip_reason is None:
                        first_skip_reason = "rule:mid_group_skip"
                    continue
            if not _track_is_active_today(track, now):
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="rule:track_inactive_today", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso, **feature_kw)
                log_rule_obs(stage="track_inactive", reason="rule:track_inactive_today", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:track_inactive_today"
                continue
            log_csv(mint=mint, action="CANDIDATE", trading_mode="rule_based", policy_track=track["name"],
                    reason="rule:candidate", age_min=round(age_min, 2),
                    buy_volume_share_to_signal=round(pressure, 6),
                    entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                    signal_time=signal_time_iso,
                    signal_hour_utc=datetime.fromtimestamp(signal["signal_time"], tz=timezone.utc).hour,
                    buyers_velocity=_buyers_velocity,
                    buyers_acceleration=_buyers_acceleration,
                    **feature_kw)
            log_rule_obs(stage="track_candidate", reason="rule:candidate", **obs_row)

            if not track.get("live_entry", True):
                log_csv(mint=mint, action="SKIP", trading_mode="rule_based", policy_track=track["name"],
                        reason="rule:shadow_only_track", age_min=round(age_min, 2),
                        buy_volume_share_to_signal=round(pressure, 6),
                        entry_mcap_clean=round(entry_mcap, 2), score=round(score_hint, 6), p_win=round(score_hint, 6),
                        signal_time=signal_time_iso, **feature_kw)
                log_rule_obs(stage="track_shadow", reason="rule:shadow_only_track", **obs_row)
                if first_skip_reason is None:
                    first_skip_reason = "rule:shadow_only_track"
                continue

            execute_rule_based_buy(mint, state, signal, score_hint=score_hint, track=track)
            any_track_hit = True
            break

        if not any_track_hit:
            final_reason = first_skip_reason or ("rule:no_signal_ready" if not any_signal_seen else "rule:no_track_matched")
            log_rule_obs(stage="rule_eval_finished", reason=final_reason, score_hint=round(score_hint, 6), **obs_base)
            if any_signal_seen:
                with tokens_lock:
                    if mint in tokens:
                        tokens[mint]["evaluated"] = True
            _dl_eval("SKIP", final_reason, score_hint, 0.0)
        else:
            with tokens_lock:
                if mint in tokens:
                    tokens[mint]["evaluated"] = True
            log_rule_obs(stage="rule_eval_finished", reason="rule:selected", score_hint=round(score_hint, 6), **obs_base)
            _dl_eval("BUY", "rule:selected", score_hint, 0.0)
        return

    # legacy ML ranking mode
    if not (EVAL_WIN_LO <= age_min <= EVAL_WIN_HI):
        return

    with tokens_lock:
        tokens[mint]["evaluated"] = True

    print(f"⚖️  EVALUATING: {mint[:8]}... | volume=${feats['volume_first_30min']:.0f} | buyers={feats['unique_buyers_30min']}")

    filter_flags = {
        "passed_volume":   feats["volume_first_30min"] >= MIN_VOLUME_30MIN,
        "passed_buyers":   feats["unique_buyers_30min"] >= MIN_UNIQUE_BUYERS,
        "passed_avg_size": feats["avg_trade_size_30min"] >= MIN_AVG_TRADE_SIZE,
        "passed_buy_sell": feats["buy_sell_ratio_30min"] >= MIN_BUY_SELL_RATIO,
    }
    filter_flags["all_filters_passed"] = all(filter_flags.values())
    feat_kw = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in feats.items()}

    ok, short_code, detail = passes_filters(feats)
    if not ok:
        log.info(f"[SKIP]  {mint[:8]}  filter: {detail}")
        log_csv(mint=mint, action="SKIP", trading_mode="ml_ranking", reason=f"filter:{short_code}",
                age_min=round(age_min, 2), **feature_kw, **filter_flags, **feat_kw)
        return

    s  = score(feats)
    ev = s * B_MEDIAN - (1 - s) * LOSS
    _push_score(s)

    queued = False
    with candidates_lock:
        if not any(c["token"] == mint for c in candidates):
            candidates.append({
                "token": mint,
                "score": s,
                "p_win": s,
                "_snap_id": _snap_id,
                "_feats": feats,
                "_ev": ev,
            })
            queued = True

    if queued:
        log.info(f"[CANDIDATE] {mint[:8]}  score={s:.6f}  ev={ev:+.4f}  queued for ranking")
        log_csv(mint=mint, score=s, p_win=s, ev=round(ev, 6), action="CANDIDATE",
                trading_mode="ml_ranking", reason="queued_for_ranking", age_min=round(age_min, 2),
                **feature_kw, **filter_flags, **feat_kw)


# ── Anchor event discriminators ────────────────────────────────────────────────
# discriminator = first 8 bytes of sha256("event:<EventName>")
def _discriminator(event_name: str) -> bytes:
    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]

DISC_TRADE  = _discriminator("TradeEvent")
DISC_CREATE = _discriminator("CreateEvent")

def _pubkey_str(data: bytes, offset: int) -> str:
    """Decode a 32-byte Solana public key as a base58-like hex string."""
    return base58_encode(data[offset : offset + 32])

# Minimal base58 encoder (no external deps)
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = []
    while n:
        n, rem = divmod(n, 58)
        result.append(_B58_ALPHABET[rem : rem + 1])
    result.extend(b"1" * (len(data) - len(data.lstrip(b"\x00"))))
    return b"".join(reversed(result)).decode()


def _parse_trade_event(payload: bytes) -> Optional[Dict]:
    """
    Decode a pump.fun TradeEvent from raw Anchor event bytes (after discriminator).

    Layout (little-endian):
      mint                  32 bytes  pubkey
      sol_amount             8 bytes  u64   (lamports)
      token_amount           8 bytes  u64
      is_buy                 1 byte   bool
      user                  32 bytes  pubkey
      timestamp              8 bytes  i64
      virtual_sol_reserves   8 bytes  u64   (lamports)
      virtual_token_reserves 8 bytes  u64
    Total: 105 bytes (publication correction: original minimum-length check used 97)
    """
    if len(payload) < 105:
        return None
    offset = 0

    mint_bytes          = payload[offset:offset+32]; offset += 32
    sol_amount_lam,     = struct.unpack_from("<Q", payload, offset); offset += 8
    token_amount,       = struct.unpack_from("<Q", payload, offset); offset += 8
    is_buy              = payload[offset] != 0;                       offset += 1
    user_bytes          = payload[offset:offset+32]; offset += 32
    timestamp,          = struct.unpack_from("<q", payload, offset); offset += 8
    virt_sol,           = struct.unpack_from("<Q", payload, offset); offset += 8
    virt_tok,           = struct.unpack_from("<Q", payload, offset); offset += 8

    mint_str = base58_encode(mint_bytes)
    user_str = base58_encode(user_bytes)
    sol_amount = sol_amount_lam / 1e9          # lamports → SOL

    sol_price  = get_sol_price()
    usd        = sol_amount * sol_price

    # Spot price in SOL per token (bonding curve ratio)
    price_sol = (virt_sol / 1e9) / (virt_tok / 1e6) if virt_tok > 0 else sol_amount

    return {
        "mint":         mint_str,
        "side":         "buy" if is_buy else "sell",
        "usd":          usd,
        "price":        price_sol,
        "wallet":       user_str,
        "ts":           float(timestamp) if timestamp > 1_000_000_000 else time.time(),
        "sol_amount":   sol_amount,
        "token_amount": token_amount,
        "virt_sol":     virt_sol,
        "virt_tok":     virt_tok,
    }


# ── Trade parser ───────────────────────────────────────────────────────────────
def parse_trade(msg: Dict) -> Optional[Dict]:
    """
    Parse a Helius logsSubscribe notification into a normalised trade dict.

    Pump.fun emits Anchor CPI events encoded as base64 in "Program data:" log
    lines.  Each event starts with an 8-byte discriminator:
      sha256("event:TradeEvent")[:8]   → trade
      sha256("event:CreateEvent")[:8]  → token creation (ignored here)

    Returns a dict with keys: mint, side, usd, price, wallet, ts
    or None if no parseable pump.fun TradeEvent is found.
    """
    try:
        value = msg.get("params", {}).get("result", {}).get("value", {})
        logs  = value.get("logs", [])

        # Gate: must involve the pump.fun program
        if not any(PUMPFUN_PROGRAM in l for l in logs):
            return None

        for line in logs:
            # Anchor events appear on "Program data: <base64>" lines
            if not line.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(line[len("Program data: "):])
            except Exception:
                continue

            if len(raw) < 8:
                continue

            disc = raw[:8]
            payload = raw[8:]

            if disc == DISC_TRADE:
                return _parse_trade_event(payload)
            # CreateEvent — not handled as a trade, skip

        return None

    except Exception as exc:
        log.debug(f"parse_trade error: {exc}")
        return None


# ── Trade handler ─────────────────────────────────────────────────────────────
def handle_trade(trade: dict) -> None:
    # Content-based dedup: guard against Helius re-delivering the same tx when
    # the message has no signature (sig-level dedup in listen() would have missed it).
    if _is_duplicate_trade(trade):
        log.debug(
            f"dedup(content): dropped duplicate trade "
            f"{trade.get('mint','')[:8]} wallet={trade.get('wallet','')[:8]} "
            f"ts={int(trade.get('ts',0))} amt={trade.get('token_amount',0)}"
        )
        return

    mint = trade["mint"]

    with tokens_lock:
        is_new = mint not in tokens
        if is_new:
            tokens[mint] = {
                "start_time": trade["ts"],
                "trades":     [],
                "evaluated":  False,
                "position":   None,
            }
        tokens[mint]["trades"].append(trade)
        state = tokens[mint]

    sol_usd = get_sol_price()

    if _dl_write_trade is not None:
        try:
            _dl_write_trade(trade, sol_usd)
        except Exception as _e:
            log.warning(f"[dl] write_trade: {_e}")

    try:
        log_outcome_v2_tape(trade, state, sol_usd)
    except Exception as _e:
        log.warning(f"[v2_tape] write_trade: {_e}")

    if is_new:
        print(f"👁 NEW TOKEN: {mint[:8]}... | total monitored: {len(tokens)}")
        log_csv(mint=mint, action="SEEN", reason="new_token_detected")

    # Check evaluation window for potential entry
    maybe_evaluate(mint)

    # Update exit conditions for any existing open position on this mint
    with positions_lock:
        pos = open_positions.get(mint)
    if pos and not pos.get("closed") and trade.get("price", 0) > 0:
        evaluate_exit(mint, pos, trade["price"], time.time())


# ── WebSocket listener ─────────────────────────────────────────────────────────
async def listen() -> None:
    """Connect to Helius, subscribe to pump.fun logs, and process messages."""
    require_network()
    subscribe_payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMPFUN_PROGRAM]},
            {"commitment": "confirmed"},
        ],
    })

    while True:
        try:
            log.info("Connecting to Helius WebSocket…")
            async with websockets.connect(
                HELIUS_WS_URL,
                ping_interval=30,
                ping_timeout=60,
                max_size=10 * 1024 * 1024,
            ) as ws:
                await ws.send(subscribe_payload)
                ack = await ws.recv()
                log.info(f"Subscribed  ack={ack[:120]}")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Skip subscription confirmations
                    if "id" in msg and "result" in msg:
                        continue

                    trade = parse_trade(msg)
                    if trade:
                        result = msg.get("params", {}).get("result", {})
                        sig  = result.get("value", {}).get("signature", "")
                        slot = result.get("context", {}).get("slot")   # int | None
                        if _is_duplicate_sig(sig):
                            log.debug(f"dedup: dropped duplicate tx {sig[:20]}…")
                            continue
                        # Carry sig/slot through the pipeline for auditability.
                        # Prefixed with '_' to distinguish metadata from parsed fields.
                        trade["_sig"]  = sig
                        trade["_slot"] = slot if slot is not None else ""
                        handle_trade(trade)

        except (websockets.ConnectionClosed, websockets.WebSocketException, OSError) as exc:
            exc_str = str(exc)
            if "429" in exc_str:
                retry_delay = 60
                log.warning(f"WebSocket rate-limited (HTTP 429) — backing off {retry_delay}s before retry")
            else:
                retry_delay = RECONNECT_DELAY
                log.warning(f"WebSocket disconnected: {exc} — retrying in {retry_delay}s")
            await asyncio.sleep(retry_delay)
        except Exception as exc:
            log.error(f"Unexpected error: {exc}", exc_info=True)
            await asyncio.sleep(RECONNECT_DELAY)


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    require_network()
    if TRADING_MODE != "rule_based":
        raise RuntimeError(f"Unsupported TRADING_MODE={TRADING_MODE!r}. This bot is pinned to rule_based only.")
    _acquire_singleton_lock()
    log.info("═" * 62)
    try:
        _git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True,
        ).strip()
    except Exception:
        _git_hash = "unknown"
    log.info(f"  git commit: {_git_hash}")
    if _MICRO_LIVE_ACTIVE:
        log.info("  pump.fun hybrid bot  |  PAPER engine + guarded MICRO_LIVE path")
    elif _DRY_RUN_ACTIVE:
        log.info("  pump.fun hybrid bot  |  PAPER engine + DRY_RUN_LIVE shadow path")
    else:
        log.info("  pump.fun paper-trading bot  |  PAPER ONLY — no real txs")
    if TRADING_MODE == "rule_based":
        log.info(f"  mode=rule_based  fixed_stake=${FIXED_STAKE_USD:.0f}  max_pos={MAX_POSITIONS}  tracks={len(RULE_TRACKS)}")
        for _t in RULE_TRACKS:
            _stake = _t.get('stake_usd', FIXED_STAKE_USD)
            log.info(f"    track={_t['name']} buyers>={_t['buyers_min']} pressure=[{_t['pressure_min']:.2f},{_t['pressure_max']:.2f}) entry_mcap=[{_t['entry_mcap_min']:.0f},{_t['entry_mcap_max']:.0f}) clean>={_t['clean_trade_usd_min']:.0f} stake=${_stake:.0f} tp={_t['tp_multiple']:.2f}x timeout={_t['timeout_minutes']}m")
    else:
        log.info(f"  mode=ml_ranking  bankroll=${BANKROLL:.0f}  pos_size={POSITION_SIZE*100:.0f}%  max_pos={MAX_POSITIONS}  ranking_top_n={RANKING_TOP_N}  ranking_interval={RANKING_INTERVAL}s")
    log.info("═" * 62)

    _ensure_csv()
    if TRADING_MODE == "ml_ranking" or LOAD_MODEL_DIAGNOSTICS:
        load_model()
    else:
        log.info("Model loading skipped (rule_based mode, diagnostics off).")

    # ── Data layer: import bridge and connect once at startup ─────────────────
    global _dl_write_trade, _dl_snapshot, _dl_write_decision
    log.info("Private data bridge excluded; CSV observability retained")

    # Background position monitor
    monitor = threading.Thread(target=position_monitor_loop, daemon=True, name="pos-monitor")
    monitor.start()

    # Background ranking engine (legacy ML mode only)
    if TRADING_MODE == "ml_ranking":
        ranker = threading.Thread(target=ranking_loop, daemon=True, name="ranker")
        ranker.start()

    # Run WebSocket listener (blocks until interrupted)
    try:
        asyncio.run(listen())
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
