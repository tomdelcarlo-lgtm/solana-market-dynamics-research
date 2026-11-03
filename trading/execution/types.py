# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/types.py
───────────────────
Core data types for the execution layer.

These types are the contract between the policy engine (pumpfun_bot.py)
and the execution adapter. They carry intent, not implementation.

Phase 1: types defined. No send path exists yet.
Phase 4: added external_order_id and failure_details to ExecutionResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class OrderAction(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Lifecycle status of an order attempt."""
    PLANNED       = "planned"       # intent created, not yet evaluated
    DENIED        = "denied"        # blocked by risk engine
    DRY_RUN_SENT  = "dry_run_sent"  # would have been sent (dry-run mode)
    FILLED        = "filled"        # live fill confirmed (Phase 4)
    FAILED        = "failed"        # execution error
    CANCELLED     = "cancelled"     # withdrawn before send


# ── Execution intent ──────────────────────────────────────────────────────────

@dataclass
class ExecutionIntent:
    """A buy or sell decision from the policy engine.

    Created by pumpfun_bot.py when a candidate or exit condition is
    triggered. Passed to the execution adapter and risk engine unchanged.

    This is a value object — do not add execution side-effects here.
    """
    action: OrderAction
    token: str                          # mint address
    policy_track: str                   # e.g. "v7_core_8k9k"
    intended_size_usd: float            # position size in USD
    intended_entry_mcap: float          # market cap at signal
    pressure: float                     # buy pressure score (0–1)
    mode: str                           # ExecutionMode value at creation time
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Optional enrichment — filled later by adapter/quote layer
    quote_metadata: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None


# ── Execution result ──────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Outcome of passing an intent through the execution adapter.

    Returned to pumpfun_bot.py after the adapter processes the intent.
    In paper mode this is always a no-op result.
    """
    intent: ExecutionIntent
    status: OrderStatus
    deny_reason: Optional[str] = None   # set when status == DENIED

    # Fill data (Phase 4+)
    realized_size_usd: Optional[float]  = None
    realized_price: Optional[float]     = None
    filled_token_amount: Optional[float] = None          # absolute token amount from tx meta, when available
    slippage_bps_proxy: Optional[int]   = None
    tx_id: Optional[str]                = None           # on-chain tx signature
    external_order_id: Optional[str]    = None           # DEX/router order id if applicable
    failure_details: Optional[str]      = None           # structured failure info (FAILED status)

    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def was_denied(self) -> bool:
        return self.status == OrderStatus.DENIED

    @property
    def was_executed(self) -> bool:
        return self.status in (OrderStatus.DRY_RUN_SENT, OrderStatus.FILLED)


# ── Risk check result ─────────────────────────────────────────────────────────

@dataclass
class RiskDecision:
    """Decision from the risk engine for a given intent.

    allow=True  → proceed to execution (or dry-run)
    allow=False → deny with explicit reason
    """
    allow: bool
    reason: str           # human-readable explanation (always set)
    check_name: str = ""  # which specific rule triggered the deny

    @staticmethod
    def allow_order(reason: str = "all checks passed") -> "RiskDecision":
        return RiskDecision(allow=True, reason=reason)

    @staticmethod
    def deny_order(reason: str, check_name: str = "") -> "RiskDecision":
        return RiskDecision(allow=False, reason=reason, check_name=check_name)
