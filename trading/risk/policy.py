# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
risk/policy.py
───────────────
Risk policy constants and track-allowlist helpers.

Design intent:
- Deny reasons are first-class strings, not magic numbers.
- Track allowlisting is config-driven (LIVE_ALLOWED_POLICY_TRACKS env var).
- This file is policy-agnostic: it enforces whatever tracks are configured,
  not a hard-coded list.

Phase 1: constants and helpers only. No enforcement logic yet.
"""

from __future__ import annotations

from typing import List


# ── Deny reason constants ─────────────────────────────────────────────────────
# Use these strings in RiskDecision.deny_order() calls so log grep is reliable.

class DENY_REASONS:
    MODE_NOT_LIVE          = "mode_not_live"
    NOT_ARMED              = "live_not_armed"
    SAFE_MODE_ACTIVE       = "safe_mode_is_active"
    TRACK_NOT_ALLOWED      = "track_not_in_allowlist"
    SIZE_EXCEEDS_CAP       = "size_exceeds_per_trade_cap"
    NOTIONAL_CAP_EXCEEDED  = "total_notional_cap_exceeded"
    DAILY_LOSS_BREACHED    = "daily_loss_limit_breached"
    INSUFFICIENT_BALANCE   = "insufficient_wallet_balance"
    QUOTE_STALE            = "quote_is_stale"
    SLIPPAGE_TOO_HIGH      = "slippage_exceeds_threshold"
    MAX_POSITIONS_REACHED  = "max_open_positions_reached"
    PREFLIGHT_FAILED       = "preflight_check_failed"
    SELLS_ONLY_MODE        = "sell_only_mode_active"
    PRESSURE_BELOW_MIN     = "pressure_below_minimum"
    ENTRY_MCAP_OUT_OF_RANGE= "entry_mcap_out_of_range"
    LOW_LANE_SUNDAY        = "low_lane_sunday_not_allowed"
    NOT_IMPLEMENTED        = "execution_path_not_implemented"


# ── Track allowlist helpers ───────────────────────────────────────────────────

def is_track_allowed(track_name: str, allowed_tracks: List[str]) -> bool:
    """Return True if track_name is in the configured allowlist.

    An empty allowlist denies everything (fail-closed).
    """
    if not allowed_tracks:
        return False
    return track_name in allowed_tracks


def assert_track_allowed(track_name: str, allowed_tracks: List[str]) -> None:
    """Raise ValueError if the track is not allowlisted.

    Convenience for preflight checks.
    """
    if not is_track_allowed(track_name, allowed_tracks):
        raise ValueError(
            f"Policy track {track_name!r} is not in the live allowlist. "
            f"Allowed: {allowed_tracks}"
        )
