# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/quote.py
──────────────────
Signal-price quote snapshot builder for micro_live mode.

Public API:
    make_signal_quote(signal_price, signal_time_s) → dict

This is NOT a Jupiter or DEX quote fetch. Pumpfun tokens on the bonding
curve are priced by on-chain bonding curve state, and each trade event
carries the last-trade price. The "quote" here is a snapshot of that
price at signal time, timestamped so the quote_freshness gate can
measure how stale the signal is by the time the order attempt runs.

The timestamp_ms field drives _check_quote_freshness in risk/engine.py:
  - If (now_ms - timestamp_ms) > LIVE_QUOTE_MAX_AGE_MS → DENIED as stale.
  - This catches late execution: slow event loops, GIL contention, etc.

slippage_bps is intentionally absent because pumpfun bonding curve depth
is not available without a separate RPC call. _check_slippage skips when
slippage_bps is not present in quote_metadata (no false deny).

Phase 4.1 blocker — real pre-trade slippage estimate requires:
  1. Fetch bonding curve account state from LIVE_RPC_URL.
  2. Simulate the buy/sell instruction to compute output amount and
     price impact.
  3. This requires solders or solana-py + pumpfun program ABI — neither
     is available in the current environment.
Until then, slippage gate is effectively disabled (skipped, not denied).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


def make_signal_quote(
    signal_price: float,
    signal_time_s: Optional[float] = None,
    entry_mcap: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a quote_metadata dict from a signal-time price observation.

    The returned dict is attached to ExecutionIntent.quote_metadata before
    the risk engine runs. The timestamp_ms field is the key field: it
    records when the price snapshot was taken so quote_freshness can
    measure signal age.

    Args:
        signal_price:  Token price (bonding-curve units) at signal time.
                       Stored for audit; not used to compute slippage.
        signal_time_s: Unix timestamp (seconds, float) of the signal.
                       Defaults to time.time() if None — i.e. "right now",
                       which means freshness = time from this call to
                       risk evaluation (should be near-zero).
        entry_mcap:    Entry market cap at signal time (USD). Optional;
                       stored for audit only.

    Returns:
        quote_metadata dict:
          timestamp_ms  (int)   — epoch ms; drives _check_quote_freshness
          signal_price  (float) — bonding-curve price at signal time
          source        (str)   — "signal_price_snapshot"
          entry_mcap    (float) — market cap at signal, if provided

    Note:
        slippage_bps is intentionally absent (no bonding curve depth
        data). _check_slippage returns None (pass) when slippage_bps
        is missing, so no spurious denial occurs.
    """
    ts_ms = int(
        (signal_time_s if signal_time_s is not None else time.time()) * 1000
    )
    meta: Dict[str, Any] = {
        "timestamp_ms": ts_ms,
        "signal_price": signal_price,
        "source":       "signal_price_snapshot",
    }
    if entry_mcap is not None:
        meta["entry_mcap"] = entry_mcap
    return meta
