# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
Shadow-mode journal for P3/P4 candidate evaluation.

Appends structured events to logs/shadow_journal_<phase>_<ts>_<session>.csv.
Never reads from or modifies execution state, control state, or the paper log.

Event types:
  WOULD_ENTER       paper BUY passes all candidate gates; would have been sent
  WOULD_EXIT        matched paper exit; would have exited
  BLOCKED_SUNDAY    low-lane BUY on Sunday; blocked by non-Sunday constraint
  BLOCKED_ALLOWLIST track not present in candidate LIVE_ALLOWED_POLICY_TRACKS
  BLOCKED_PRESSURE candidate min-pressure gate failed
  BLOCKED_MCAP     candidate entry-mcap gate failed
"""

import csv
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root

SHADOW_LOG_DIR = runtime_root() / "logs"

SHADOW_COLS = [
    "ts_utc",
    "session_id",
    "phase",
    "lane",            # core | low_lane | unknown
    "track",
    "token",
    "event_type",      # WOULD_ENTER | WOULD_EXIT | BLOCKED_SUNDAY | BLOCKED_ALLOWLIST | BLOCKED_PRESSURE | BLOCKED_MCAP
    "block_reason",    # gate name; empty for WOULD_* events
    "day_of_week",     # Monday..Sunday (UTC)
    "paper_size_usd",  # paper stake as logged (e.g. $350 core, $250 low-lane)
    "candidate_size_usd",  # LIVE_POSITION_SIZE_USD from candidate env
    "mcap",            # entry_mcap_clean (USD); empty on exit events
    "pressure",        # buy_volume_share_to_signal; empty on exit events
    "gross_multiple",  # exit gross_multiple; empty on entry events
    "sim_net_pnl_usd", # (net_multiple_est - 1) * candidate_size_usd; empty on entry events
    "exit_reason",     # paper exit reason string; empty on entry events
    "trade_id",        # paper trade_id for cross-reference
]

_CORE_FRAGMENTS = {"core"}
_LOW_LANE_FRAGMENTS = {"low"}


def _infer_lane(track: str) -> str:
    t = track.lower()
    if "low" in t:
        return "low_lane"
    if "core" in t:
        return "core"
    return "unknown"


class ShadowJournal:
    """Append-only shadow event writer.

    No coupling to execution/, risk/, ops/, or state/. Safe to instantiate
    and call from any context without triggering live-path side effects.
    """

    def __init__(self, session_id: str, phase: str):
        self.session_id = session_id
        self.phase = phase.lower()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        SHADOW_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = SHADOW_LOG_DIR / f"shadow_journal_{self.phase}_{ts}_{session_id}.csv"
        with open(self.path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=SHADOW_COLS).writeheader()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _append(self, row: dict) -> None:
        row.setdefault("ts_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        row["session_id"] = self.session_id
        row["phase"] = self.phase
        row.setdefault("lane", _infer_lane(row.get("track", "")))
        filled = {col: row.get(col, "") for col in SHADOW_COLS}
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=SHADOW_COLS, extrasaction="ignore").writerow(filled)

    # ── Public event writers ───────────────────────────────────────────────────

    def would_enter(
        self,
        *,
        track: str,
        token: str,
        paper_size_usd: float,
        candidate_size_usd: float,
        mcap: float,
        pressure: float,
        day_of_week: str,
        trade_id: str = "",
    ) -> None:
        self._append({
            "track": track,
            "token": token,
            "event_type": "WOULD_ENTER",
            "block_reason": "",
            "day_of_week": day_of_week,
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": f"{candidate_size_usd:.2f}",
            "mcap": f"{mcap:.2f}",
            "pressure": f"{pressure:.6f}",
            "trade_id": trade_id,
        })

    def would_exit(
        self,
        *,
        track: str,
        token: str,
        exit_reason: str,
        gross_multiple: float,
        net_multiple_est: float,
        paper_size_usd: float,
        candidate_size_usd: float,
        day_of_week: str,
        trade_id: str = "",
    ) -> None:
        sim_net_pnl = (net_multiple_est - 1.0) * candidate_size_usd
        self._append({
            "track": track,
            "token": token,
            "event_type": "WOULD_EXIT",
            "block_reason": "",
            "exit_reason": exit_reason,
            "day_of_week": day_of_week,
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": f"{candidate_size_usd:.2f}",
            "gross_multiple": f"{gross_multiple:.6f}",
            "sim_net_pnl_usd": f"{sim_net_pnl:.2f}",
            "trade_id": trade_id,
        })

    def blocked_sunday(
        self,
        *,
        track: str,
        token: str,
        paper_size_usd: float,
        candidate_size_usd: float,
        mcap: float,
        pressure: float,
        trade_id: str = "",
    ) -> None:
        self._append({
            "track": track,
            "token": token,
            "event_type": "BLOCKED_SUNDAY",
            "block_reason": "sunday_low_lane_blocked",
            "day_of_week": "Sunday",
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": f"{candidate_size_usd:.2f}",
            "mcap": f"{mcap:.2f}",
            "pressure": f"{pressure:.6f}",
            "trade_id": trade_id,
        })

    def blocked_allowlist(
        self,
        *,
        track: str,
        token: str,
        paper_size_usd: float,
        day_of_week: str = "",
        trade_id: str = "",
    ) -> None:
        self._append({
            "track": track,
            "token": token,
            "event_type": "BLOCKED_ALLOWLIST",
            "block_reason": "track_not_in_candidate_allowlist",
            "day_of_week": day_of_week,
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": "",
            "trade_id": trade_id,
        })

    def blocked_pressure(
        self,
        *,
        track: str,
        token: str,
        paper_size_usd: float,
        candidate_size_usd: float,
        mcap: float,
        pressure: float,
        minimum_pressure: float,
        day_of_week: str = "",
        trade_id: str = "",
    ) -> None:
        self._append({
            "track": track,
            "token": token,
            "event_type": "BLOCKED_PRESSURE",
            "block_reason": f"pressure<{minimum_pressure:.6f}",
            "day_of_week": day_of_week,
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": f"{candidate_size_usd:.2f}",
            "mcap": f"{mcap:.2f}",
            "pressure": f"{pressure:.6f}",
            "trade_id": trade_id,
        })

    def blocked_mcap(
        self,
        *,
        track: str,
        token: str,
        paper_size_usd: float,
        candidate_size_usd: float,
        mcap: float,
        pressure: float,
        mcap_min: float | None,
        mcap_max: float | None,
        day_of_week: str = "",
        trade_id: str = "",
    ) -> None:
        lower = f"{mcap_min:.2f}" if mcap_min is not None else "-inf"
        upper = f"{mcap_max:.2f}" if mcap_max is not None else "+inf"
        self._append({
            "track": track,
            "token": token,
            "event_type": "BLOCKED_MCAP",
            "block_reason": f"mcap_outside_[{lower},{upper})",
            "day_of_week": day_of_week,
            "paper_size_usd": f"{paper_size_usd:.2f}",
            "candidate_size_usd": f"{candidate_size_usd:.2f}",
            "mcap": f"{mcap:.2f}",
            "pressure": f"{pressure:.6f}",
            "trade_id": trade_id,
        })
