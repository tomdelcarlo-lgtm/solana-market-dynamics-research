# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/market_regime.py
───────────────────────────
Market regime classification (DEAD / NORMAL / HOT) for live instrumentation.

Same definition as the offline phase-space analysis (Hallazgo 2026-06-09 —
"Análisis 5: phase-space del mercado"): candidates/hour in a moving window
+ median buy-pressure of evaluated candidates in that window.

Live reconstruction of the offline metrics from `logs/paper_trading_log_v2.csv`:
- "candidates/hour"  -> rate of rows with action == "CANDIDATE" in the
  last WINDOW_MINUTES, normalised to a per-hour rate.
- "median_pressure"  -> median of `buy_volume_share_to_signal` across
  evaluated rows (action in {CANDIDATE, SKIP, BUY}) with a non-empty value
  in the same window.

Thresholds (from the offline candidate-density regime, p25/p75 of
candidates/hour):
  DEAD:   candidates_per_hour <= DEAD_MAX_CANDIDATES
  HOT:    candidates_per_hour >= HOT_MIN_CANDIDATES and median_pressure >= HOT_MIN_PRESSURE
  NORMAL: everything else

This module is logging/instrumentation only — its output does not gate or
alter any trading decision. Result is cached and recomputed at most every
REGIME_REFRESH_S seconds.
"""

from __future__ import annotations

import csv
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from trading.safety import runtime_root

log = logging.getLogger(__name__)

_REPO_ROOT = runtime_root()
PAPER_LOG_PATH = _REPO_ROOT / "logs" / "paper_trading_log_v2.csv"

WINDOW_MINUTES = 60
REGIME_REFRESH_S = 600  # 10 minutes
TAIL_BYTES = 4_000_000  # generous slice, comfortably > WINDOW_MINUTES of writes

DEAD_MAX_CANDIDATES = 6
HOT_MIN_CANDIDATES = 17
HOT_MIN_PRESSURE = 0.70

_EVAL_ACTIONS = {"CANDIDATE", "SKIP", "BUY"}

_cache = {"ts": 0.0, "regime": "UNKNOWN"}


def get_current_market_regime() -> str:
    """Return the cached/recomputed current market regime: DEAD/NORMAL/HOT/UNKNOWN.

    Recomputed at most once every REGIME_REFRESH_S seconds.
    Never raises — falls back to the last known value (or UNKNOWN) on error.
    """
    now = time.time()
    if now - _cache["ts"] < REGIME_REFRESH_S and _cache["regime"] != "UNKNOWN":
        return _cache["regime"]
    try:
        regime = _compute_regime()
    except Exception as exc:  # noqa: BLE001
        log.error("market_regime: computation failed: %s", exc)
        regime = _cache.get("regime", "UNKNOWN")
    _cache["ts"] = now
    _cache["regime"] = regime
    return regime


def _tail_lines(path: Path, n_bytes: int) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - n_bytes))
        data = fh.read()
    lines = data.decode("utf-8", errors="ignore").splitlines()
    # Drop the first line — it may be a partial row from the seek point.
    return lines[1:] if lines else lines


def _compute_regime() -> str:
    if not PAPER_LOG_PATH.exists():
        return "UNKNOWN"

    with PAPER_LOG_PATH.open("r", encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    i_ts       = header.index("timestamp")
    i_action   = header.index("action")
    i_pressure = header.index("buy_volume_share_to_signal")
    n_cols     = max(i_ts, i_action, i_pressure) + 1

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)

    n_candidates = 0
    pressures: list[float] = []

    for line in _tail_lines(PAPER_LOG_PATH, TAIL_BYTES):
        row = next(csv.reader([line]), None)
        if not row or len(row) < n_cols:
            continue
        try:
            ts = datetime.fromisoformat(row[i_ts])
        except ValueError:
            continue
        if ts < cutoff:
            continue

        action = row[i_action]
        if action == "CANDIDATE":
            n_candidates += 1
        if action in _EVAL_ACTIONS:
            pv = row[i_pressure]
            if pv:
                try:
                    pressures.append(float(pv))
                except ValueError:
                    pass

    candidates_per_hour = n_candidates * (60.0 / WINDOW_MINUTES)
    median_pressure = statistics.median(pressures) if pressures else 0.0

    if candidates_per_hour <= DEAD_MAX_CANDIDATES:
        return "DEAD"
    if candidates_per_hour >= HOT_MIN_CANDIDATES and median_pressure >= HOT_MIN_PRESSURE:
        return "HOT"
    return "NORMAL"
