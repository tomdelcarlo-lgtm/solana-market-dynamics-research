# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/adapter.py
─────────────────────
Execution adapter.

Public surface:
    execute_buy(intent, config, trade_id)  → ExecutionResult
    execute_sell(intent, config, trade_id) → ExecutionResult

Phase 1: both functions were pure no-ops (paper pass-through).
Phase 2: dry-run path wired in — evaluates risk, journals every attempt,
         never signs or sends anything.
Phase 3: dry_run_sent results update the persistent live-state store
         (state/live_positions.json) and live_execution_log.csv.
Phase 4:   micro_live path implemented behind explicit mode guard.
           All risk gates are real. quote_metadata threaded through.
Phase 4.1: wallet balance check is real (RPC call via risk/engine.py).
           quote_metadata populated from signal-price snapshot
           (execution/quote.py). quote_freshness gate now functional.
Phase 4.2: _send_live_order uses solders for local signing and the
           configured unsigned-tx builder endpoint for pump.fun order
           construction. Signed tx is broadcast and confirmed via the
           configured Solana RPC. State is still updated only on FILLED.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from trading.safety import runtime_root
from typing import Any, Callable, Optional

from trading.execution.config import ExecutionConfig, ExecutionMode
from trading.safety import live_execution_disabled as send_pumpfun_native_buy_transaction, live_execution_disabled as send_pumpfun_native_sell_transaction
from trading.execution.errors import EntryPremiumAbort
from trading.execution.pumpfun_cli_shim import build_buy_command
from trading.execution.journal import (
    append_execution_log,
    append_order_journal,
    append_risk_event,
    append_state_transition,
)
from trading.execution.types import (
    ExecutionIntent,
    ExecutionResult,
    OrderAction,
    OrderStatus,
)
from trading.risk import engine as risk_engine

log = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_PROGRAM_IDS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)

# Latency regression watchdogs — logging only, baseline ~800ms SELL / ~2s BUY (2026-06-09).
SELL_LATENCY_WARN_MS = 5_000
BUY_LATENCY_WARN_MS = 6_000


# ── Public API ────────────────────────────────────────────────────────────────

def execute_buy(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: Optional[str] = None,
) -> ExecutionResult:
    """Route a BUY intent through the appropriate execution path.

    `trade_id` should be the same trade_id used in pumpfun_bot.py's paper
    position so the state store cross-references correctly. A UUID is
    generated as fallback if not provided.
    """
    return _dispatch(intent, config, trade_id=trade_id or _new_trade_id())


def execute_sell(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: Optional[str] = None,
) -> ExecutionResult:
    """Route a SELL intent through the appropriate execution path.

    `trade_id` should match the original BUY trade_id for this position.
    A UUID is generated as fallback if not provided (results in orphaned
    state entry — caller should always pass the matching id).
    """
    return _dispatch(intent, config, trade_id=trade_id or _new_trade_id())


# ── Internal dispatch ─────────────────────────────────────────────────────────

def _dispatch(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str = "",
) -> ExecutionResult:
    if config.mode == ExecutionMode.PAPER:
        return _paper_noop(intent)

    if config.mode == ExecutionMode.DRY_RUN_LIVE:
        return _dry_run_execute(intent, config, trade_id)

    if config.mode == ExecutionMode.MICRO_LIVE:
        return _micro_live_execute(intent, config, trade_id)

    raise RuntimeError(f"Unhandled execution mode: {config.mode!r}")


# ── Dry-run execution path (Phase 2 / Phase 3) ───────────────────────────────

def _dry_run_execute(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str = "",
) -> ExecutionResult:
    """Run a BUY or SELL intent through the full dry-run pipeline.

    Steps:
    1. Evaluate risk (sequential checks).
    2. Journal the risk event.
    3. If denied → journal denied order, return DENIED result.
    4. If allowed → journal dry_run_sent order, update live-state store,
       append to execution log, return DRY_RUN_SENT result.

    No signing, sending, or wallet access at any point.
    """
    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state="",
        to_state=OrderStatus.PLANNED.value,
        event="intent_created",
        metadata={"path": "dry_run"},
    )

    # 1. Risk evaluation
    if intent.action == OrderAction.BUY:
        decision = risk_engine.evaluate_entry(intent, live_state=None, config=config)
    else:
        decision = risk_engine.evaluate_exit(intent, live_state=None, config=config)

    # 2. Journal the risk event
    _safe_risk_event(intent, decision)

    # 3. Handle denial
    if not decision.allow:
        log.info(
            "[DRY_RUN DENIED]  %s %s  track=%s  reason=%s",
            intent.action.value, intent.token[:8],
            intent.policy_track, decision.reason,
        )
        result = ExecutionResult(
            intent=intent,
            status=OrderStatus.DENIED,
            deny_reason=decision.reason,
        )
        _safe_journal(result, trade_id)
        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state=OrderStatus.PLANNED.value,
            to_state=OrderStatus.DENIED.value,
            event="risk_denied",
            reason=decision.reason,
            metadata={"check_name": decision.check_name, "path": "dry_run"},
        )
        return result

    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state=OrderStatus.PLANNED.value,
        to_state="risk_passed",
        event="risk_allowed",
        reason=decision.reason,
        metadata={"path": "dry_run"},
    )

    # 4. Allowed — log as dry_run_sent (no real order placed)
    log.info(
        "[DRY_RUN SENT]    %s %s  track=%s  size=$%.2f  mcap=%.0f  trade_id=%s",
        intent.action.value, intent.token[:8],
        intent.policy_track, intent.intended_size_usd,
        intent.intended_entry_mcap, trade_id,
    )
    result = ExecutionResult(
        intent=intent,
        status=OrderStatus.DRY_RUN_SENT,
        deny_reason=None,
    )
    _safe_journal(result, trade_id)
    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state="risk_passed",
        to_state=OrderStatus.DRY_RUN_SENT.value,
        event="dry_run_simulated",
        metadata={"path": "dry_run"},
    )

    # Phase 3: update persistent live-state store
    _safe_state_update(intent, result, trade_id)

    # Phase 3: append to execution log
    _safe_execution_log(result, trade_id)

    return result


# ── micro_live execution path (Phase 4) ──────────────────────────────────────

def _micro_live_execute(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str = "",
) -> ExecutionResult:
    """Run a BUY or SELL intent through the guarded micro_live pipeline.

    Steps:
    1. Journal the pre-send intent (PLANNED status) — auditable record of
       what we were about to attempt before any gate ran.
    2. Load live state from persistent store for cap checks.
    3. Evaluate risk (all Phase 4 gates — armed, safe_mode, track, caps,
       daily loss, wallet balance stub, quote freshness, slippage).
    4. Journal the risk event.
    5. If denied → journal DENIED order, return DENIED result.
    6. If allowed → attempt send via _send_live_order (fail-closed stub).
    7. Journal the post-send result.
    8. Append to execution log.
    9. Update persistent state ONLY if result.status == FILLED.
       (FAILED or DENIED results never mutate position state.)

    The wallet_balance check in the risk engine is a fail-closed stub, so no
    real order will be sent in Phase 4. This function is structured so that
    replacing _send_live_order with a real implementation is the only change
    needed once RPC/wallet infrastructure is available.
    """
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    # 1. Pre-send intent journal (PLANNED) — written before any gate runs.
    pre_intent_result = ExecutionResult(
        intent=intent,
        status=OrderStatus.PLANNED,
        deny_reason=None,
    )
    _safe_journal(pre_intent_result, trade_id)
    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state="",
        to_state=OrderStatus.PLANNED.value,
        event="intent_created",
        metadata={"path": "micro_live"},
    )
    log.info(
        "[MICRO_LIVE INTENT]  %s %s  track=%s  size=$%.2f  trade_id=%s",
        intent.action.value, intent.token[:8],
        intent.policy_track, intent.intended_size_usd, trade_id,
    )

    # 2. Load live state for position/notional cap checks.
    live_state = _load_live_state_for_risk()

    # 3. Risk evaluation (full Phase 4 gate suite).
    if intent.action == OrderAction.BUY:
        decision = risk_engine.evaluate_entry(intent, live_state=live_state, config=config)
    else:
        decision = risk_engine.evaluate_exit(intent, live_state=live_state, config=config)

    # 4. Journal risk event.
    _safe_risk_event(intent, decision)

    # 5. Handle denial.
    if not decision.allow:
        log.info(
            "[MICRO_LIVE DENIED]  %s %s  track=%s  check=%s  reason=%s",
            intent.action.value, intent.token[:8],
            intent.policy_track, decision.check_name, decision.reason,
        )
        denied_result = ExecutionResult(
            intent=intent,
            status=OrderStatus.DENIED,
            deny_reason=decision.reason,
        )
        _safe_journal(denied_result, trade_id)
        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state=OrderStatus.PLANNED.value,
            to_state=OrderStatus.DENIED.value,
            event="risk_denied",
            reason=decision.reason,
            metadata={"check_name": decision.check_name, "path": "micro_live"},
        )
        return denied_result

    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state=OrderStatus.PLANNED.value,
        to_state="risk_passed",
        event="risk_allowed",
        reason=decision.reason,
        metadata={"path": "micro_live"},
    )

    # 6. All gates passed — attempt send via the configured live backend.
    log.warning(
        "[MICRO_LIVE SEND ATTEMPT]  %s %s  track=%s  size=$%.2f  trade_id=%s  "
        "— routing to configured live send backend",
        intent.action.value, intent.token[:8],
        intent.policy_track, intent.intended_size_usd, trade_id,
    )
    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state="risk_passed",
        to_state="send_attempted",
        event="send_attempt_started",
        metadata={"path": "micro_live"},
    )
    send_result = _send_live_order(intent, config, trade_id)

    # 7. Journal post-send result.
    _safe_journal(send_result, trade_id)
    _safe_transition(
        domain="order",
        trade_id=trade_id,
        token=intent.token,
        policy_track=intent.policy_track,
        mode=intent.mode,
        from_state="send_attempted",
        to_state=send_result.status.value,
        event="send_attempt_finished",
        reason=send_result.deny_reason or send_result.failure_details or "",
        metadata={
            "path": "micro_live",
            "tx_id": send_result.tx_id,
            "external_order_id": send_result.external_order_id,
        },
    )

    # 8. Execution log.
    _safe_execution_log(send_result, trade_id)

    # 9. State update ONLY on confirmed fill — never on FAILED or DENIED.
    if send_result.status == OrderStatus.FILLED:
        log.info(
            "[MICRO_LIVE FILLED]  %s %s  trade_id=%s  tx_id=%s — updating state",
            intent.action.value, intent.token[:8], trade_id, send_result.tx_id,
        )
        _safe_state_update(intent, send_result, trade_id)

        # 10. After a confirmed BUY fill: check whether a pending exit was
        #     queued while we were waiting for this fill.  This resolves the
        #     paper→live race: paper evaluate_exit() may have fired (and queued
        #     an exit intent) before the BUY fill reached the state store.
        if intent.action == OrderAction.BUY:
            _safe_check_pending_exit(trade_id, config)
    else:
        log.warning(
            "[MICRO_LIVE NO STATE UPDATE]  %s %s  trade_id=%s  status=%s — "
            "state not mutated (no confirmed fill)",
            intent.action.value, intent.token[:8], trade_id,
            send_result.status.value,
        )
        if intent.action == OrderAction.SELL and send_result.status == OrderStatus.FAILED:
            _safe_queue_failed_sell_retry(intent, trade_id, send_result)

    return send_result


def _send_live_order(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str,
) -> ExecutionResult:
    """Build, sign, send, and confirm a micro_live order.

    Current Phase 4.2 implementation uses:
      - solders for local keypair loading and transaction signing
      - the configured unsigned-tx builder endpoint (LIVE_TRADE_API_URL)
        for pump.fun transaction construction
      - the configured Solana RPC for broadcast + confirmation polling

    This stays fail-closed on every missing dependency or parse error.
    A FILLED result is returned only after on-chain confirmation.
    """
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    if config.live is None:
        return _failed_result(intent, "micro_live send path requires live config")

    rpc_url = (config.live.rpc_url or "").strip()
    if not rpc_url:
        return _failed_result(intent, "LIVE_RPC_URL not set")

    backend = (config.live.execution_backend or "trade_api").strip().lower()
    if backend == "pumpfun_native":
        return _send_live_order_via_pumpfun_native(
            intent=intent,
            config=config,
            trade_id=trade_id,
            rpc_url=rpc_url,
        )

    if backend == "pumpfun_cli" and intent.action == OrderAction.BUY:
        return _send_live_order_via_pumpfun_cli(
            intent=intent,
            config=config,
            trade_id=trade_id,
            rpc_url=rpc_url,
        )

    wallet_path = (config.live.wallet_path or "").strip()
    trade_api_url = (config.live.trade_api_url or "").strip()

    if not wallet_path:
        return _failed_result(intent, "LIVE_WALLET_PATH not set")
    if not trade_api_url:
        return _failed_result(intent, "LIVE_TRADE_API_URL not set")

    try:
        keypair = _load_wallet_keypair(wallet_path)
        wallet_pubkey = str(keypair.pubkey())

        candidate_pools = [config.live.trade_pool]
        if intent.action == OrderAction.SELL and (config.live.trade_pool or "pump") == "pump":
            candidate_pools.append("auto")

        attempt_intents = [intent, *_build_buy_6002_retry_intents(intent, config)]
        last_exc: Optional[Exception] = None
        last_attempt_intent = intent

        for attempt_index, attempt_intent in enumerate(attempt_intents):
            last_attempt_intent = attempt_intent
            amount_resolution_started_at = time.time()
            if attempt_intent.action == OrderAction.BUY:
                amount, denominated_in_sol = _buy_amount_for_trade_api(attempt_intent)
            else:
                _safe_transition(
                    domain="order",
                    trade_id=trade_id,
                    token=attempt_intent.token,
                    policy_track=attempt_intent.policy_track,
                    mode=attempt_intent.mode,
                    from_state="send_attempted",
                    to_state="send_attempted",
                    event="sell_size_resolve_started",
                    metadata={
                        "path": "micro_live",
                        "attempt_index": attempt_index + 1,
                        "attempt_count": len(attempt_intents),
                    },
                )
                try:
                    amount, denominated_in_sol, resolve_metadata = _sell_amount_for_trade_api(
                        rpc_url=rpc_url,
                        wallet_pubkey=wallet_pubkey,
                        mint=attempt_intent.token,
                        cached_token_amount=_cached_position_token_amount(trade_id, attempt_intent.token),
                        visibility_timeout_ms=max(5000, config.live.order_timeout_ms),
                    )
                except Exception as exc:
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="sell_size_resolve_failed",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "resolve_elapsed_ms": round((time.time() - amount_resolution_started_at) * 1000.0, 1),
                            "error": str(exc),
                        },
                    )
                    raise
                _safe_transition(
                    domain="order",
                    trade_id=trade_id,
                    token=attempt_intent.token,
                    policy_track=attempt_intent.policy_track,
                    mode=attempt_intent.mode,
                    from_state="send_attempted",
                    to_state="send_attempted",
                    event="sell_size_resolved",
                    metadata={
                        "path": "micro_live",
                        "attempt_index": attempt_index + 1,
                        "attempt_count": len(attempt_intents),
                        "amount": amount,
                        "resolve_elapsed_ms": round((time.time() - amount_resolution_started_at) * 1000.0, 1),
                        **resolve_metadata,
                    },
                )

            retry_next_buy_attempt = False
            for pool in candidate_pools:
                try:
                    trade_api_started_at = time.time()
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="trade_api_request_started",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "pool": pool,
                            "action": attempt_intent.action.value,
                        },
                    )
                    unsigned_tx_bytes = _request_trade_local_transaction(
                        trade_api_url = trade_api_url,
                        wallet_pubkey = wallet_pubkey,
                        intent        = attempt_intent,
                        amount        = amount,
                        denominated_in_sol = denominated_in_sol,
                        slippage_percent   = _slippage_percent(config),
                        priority_fee_sol   = config.live.priority_fee_sol,
                        pool               = pool,
                        timeout_ms         = config.live.quote_timeout_ms,
                    )
                    trade_api_elapsed_ms = round((time.time() - trade_api_started_at) * 1000.0, 1)
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="trade_api_request_finished",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "pool": pool,
                            "action": attempt_intent.action.value,
                            "elapsed_ms": trade_api_elapsed_ms,
                        },
                    )
                    sign_started_at = time.time()
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="tx_sign_started",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "pool": pool,
                            "action": attempt_intent.action.value,
                        },
                    )
                    signed_tx_b64 = _sign_versioned_transaction(unsigned_tx_bytes, keypair)
                    sign_elapsed_ms = round((time.time() - sign_started_at) * 1000.0, 1)
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="tx_sign_finished",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "pool": pool,
                            "action": attempt_intent.action.value,
                            "elapsed_ms": sign_elapsed_ms,
                        },
                    )
                    broadcast_started_at = time.time()
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="tx_broadcast_started",
                        metadata={
                            "path": "micro_live",
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "pool": pool,
                            "action": attempt_intent.action.value,
                        },
                    )
                    tx_id = _broadcast_signed_transaction(
                        rpc_url=rpc_url,
                        signed_tx_b64=signed_tx_b64,
                    )
                    broadcast_elapsed_ms = round((time.time() - broadcast_started_at) * 1000.0, 1)
                    _safe_transition(
                        domain="order",
                        trade_id=trade_id,
                        token=attempt_intent.token,
                        policy_track=attempt_intent.policy_track,
                        mode=attempt_intent.mode,
                        from_state="send_attempted",
                        to_state="send_attempted",
                        event="tx_broadcasted",
                        metadata={
                            "path": "micro_live",
                            "tx_id": tx_id,
                            "attempt_index": attempt_index + 1,
                            "attempt_count": len(attempt_intents),
                            "trade_api_elapsed_ms": trade_api_elapsed_ms,
                            "sign_elapsed_ms": sign_elapsed_ms,
                            "broadcast_elapsed_ms": broadcast_elapsed_ms,
                        },
                    )
                    transition_meta = {
                        "path": "micro_live",
                        "tx_id": tx_id,
                        "attempt_index": attempt_index + 1,
                        "attempt_count": len(attempt_intents),
                    }

                    def _emit_post_send_transition(event: str) -> None:
                        _safe_transition(
                            domain="order",
                            trade_id=trade_id,
                            token=attempt_intent.token,
                            policy_track=attempt_intent.policy_track,
                            mode=attempt_intent.mode,
                            from_state="send_attempted",
                            to_state="send_attempted",
                            event=event,
                            metadata=transition_meta,
                        )

                    tx_view = _await_confirmed_transaction_details(
                        rpc_url=rpc_url,
                        tx_id=tx_id,
                        timeout_ms=config.live.order_timeout_ms,
                        on_confirmed=lambda: _emit_post_send_transition("tx_confirmed"),
                        on_fetched=lambda: _emit_post_send_transition("tx_fetch_confirmed"),
                    )
                    realized = _extract_realized_fill(
                        tx_view=tx_view,
                        wallet_pubkey=wallet_pubkey,
                        mint=attempt_intent.token,
                        intent=attempt_intent,
                    )

                    log.info(
                        "[MICRO_LIVE SENT]  %s %s  trade_id=%s  tx_id=%s  realized_usd=%s",
                        attempt_intent.action.value,
                        attempt_intent.token[:8],
                        trade_id,
                        tx_id,
                        f"{realized['realized_size_usd']:.2f}" if realized.get("realized_size_usd") is not None else "n/a",
                    )
                    return ExecutionResult(
                        intent=attempt_intent,
                        status=OrderStatus.FILLED,
                        deny_reason=None,
                        realized_size_usd=realized.get("realized_size_usd"),
                        realized_price=realized.get("realized_price"),
                        filled_token_amount=realized.get("filled_token_amount"),
                        slippage_bps_proxy=realized.get("slippage_bps_proxy"),
                        tx_id=tx_id,
                        external_order_id=tx_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if _should_fallback_trade_api_to_pumpfun_native(
                        intent=attempt_intent,
                        config=config,
                        trade_api_url=trade_api_url,
                        exc=exc,
                    ):
                        log.warning(
                            "[MICRO_LIVE FALLBACK]  %s %s  trade_id=%s  backend=trade_api -> pumpfun_native  reason=%s",
                            attempt_intent.action.value,
                            attempt_intent.token[:8],
                            trade_id,
                            exc,
                        )
                        _safe_transition(
                            domain="order",
                            trade_id=trade_id,
                            token=attempt_intent.token,
                            policy_track=attempt_intent.policy_track,
                            mode=attempt_intent.mode,
                            from_state="send_attempted",
                            to_state="send_attempted",
                            event="trade_api_fallback_to_pumpfun_native",
                            reason=str(exc),
                            metadata={
                                "path": "micro_live",
                                "trade_api_url": trade_api_url,
                            },
                        )
                        return _send_live_order_via_pumpfun_native(
                            intent=attempt_intent,
                            config=config,
                            trade_id=trade_id,
                            rpc_url=rpc_url,
                        )
                    if _should_retry_sell_with_auto_pool(intent=attempt_intent, pool=pool, exc=exc):
                        log.warning(
                            "[MICRO_LIVE SEND RETRY]  %s %s  trade_id=%s  pool=%s -> auto  reason=%s",
                            attempt_intent.action.value,
                            attempt_intent.token[:8],
                            trade_id,
                            pool,
                            exc,
                        )
                        continue
                    if (
                        _should_retry_buy_after_6002(attempt_intent, exc)
                        and attempt_index < len(attempt_intents) - 1
                    ):
                        next_intent = attempt_intents[attempt_index + 1]
                        log.warning(
                            "[MICRO_LIVE BUY RETRY]  %s %s  trade_id=%s  size=$%.2f -> $%.2f  attempt=%d/%d  reason=%s",
                            attempt_intent.action.value,
                            attempt_intent.token[:8],
                            trade_id,
                            attempt_intent.intended_size_usd,
                            next_intent.intended_size_usd,
                            attempt_index + 2,
                            len(attempt_intents),
                            exc,
                        )
                        _safe_transition(
                            domain="order",
                            trade_id=trade_id,
                            token=attempt_intent.token,
                            policy_track=attempt_intent.policy_track,
                            mode=attempt_intent.mode,
                            from_state="send_attempted",
                            to_state="send_attempted",
                            event="buy_retry_resized",
                            reason=str(exc),
                            metadata={
                                "attempt_index": attempt_index + 2,
                                "attempt_count": len(attempt_intents),
                                "prior_size_usd": round(attempt_intent.intended_size_usd, 4),
                                "next_size_usd": round(next_intent.intended_size_usd, 4),
                                "path": "micro_live",
                            },
                        )
                        retry_next_buy_attempt = True
                        break
                    raise

            if retry_next_buy_attempt:
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("send path exhausted without result")
    except Exception as exc:  # noqa: BLE001
        log.error(
            "[MICRO_LIVE SEND FAILED]  %s %s  trade_id=%s — %s",
            last_attempt_intent.action.value,
            last_attempt_intent.token[:8],
            trade_id,
            exc,
        )
        details = str(exc)
        if last_attempt_intent is not intent:
            details = (
                f"{details} | original_size_usd={intent.intended_size_usd:.4f} "
                f"final_attempt_size_usd={last_attempt_intent.intended_size_usd:.4f}"
            )
        return _failed_result(last_attempt_intent, details)


def _send_live_order_via_pumpfun_cli(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str,
    rpc_url: str,
) -> ExecutionResult:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    if config.live is None:
        return _failed_result(intent, "pumpfun-cli send path requires live config")
    if intent.action != OrderAction.BUY:
        return _failed_result(intent, "LIVE_EXECUTION_BACKEND=pumpfun_cli currently supports BUY only")

    keyfile_path = (config.live.pumpfun_cli_keyfile_path or "").strip()
    if not keyfile_path:
        return _failed_result(intent, "LIVE_PUMPFUN_CLI_KEYFILE_PATH not set")

    password_env_name = (config.live.pumpfun_cli_password_env or "PUMPFUN_PASSWORD").strip() or "PUMPFUN_PASSWORD"
    password = os.getenv(password_env_name)
    if not password:
        return _failed_result(intent, f"{password_env_name} not set for pumpfun-cli wallet decrypt")

    command_prefix, env_overrides, command_source = _resolve_pumpfun_cli_command(config)
    command_timeout_s = max(
        15,
        int((config.live.quote_timeout_ms + config.live.order_timeout_ms) / 1000.0) + 10,
    )

    try:
        plan = build_buy_command(
            intent,
            config,
            sol_price_usd=risk_engine._fetch_sol_price_usd(),  # noqa: SLF001
            executable=command_prefix[0],
            keyfile_path=keyfile_path,
            priority_fee_micro_lamports=config.live.pumpfun_cli_priority_fee_micro_lamports,
            dry_run=False,
        )
        argv = [*command_prefix, *plan.argv[1:]]
        cmd_env = os.environ.copy()
        cmd_env.update(env_overrides)
        cmd_env[password_env_name] = password

        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state="send_attempted",
            to_state="send_attempted",
            event="pumpfun_cli_command_started",
            metadata={
                **plan.metadata,
                "path": "micro_live_pumpfun_cli",
                "command_source": command_source,
                "command_timeout_s": command_timeout_s,
            },
        )
        started_at = time.time()
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=cmd_env,
            timeout=command_timeout_s,
            check=False,
        )
        elapsed_ms = round((time.time() - started_at) * 1000.0, 1)
        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state="send_attempted",
            to_state="send_attempted",
            event="pumpfun_cli_command_finished",
            metadata={
                "path": "micro_live_pumpfun_cli",
                "command_source": command_source,
                "elapsed_ms": elapsed_ms,
                "returncode": completed.returncode,
            },
        )
        if completed.returncode != 0:
            detail = _pumpfun_cli_error_detail(completed)
            raise ValueError(f"pumpfun-cli exited {completed.returncode}: {detail}")

        result_payload = _parse_pumpfun_cli_json_output(completed.stdout)
        if result_payload.get("error"):
            raise ValueError(str(result_payload.get("message") or result_payload["error"]))

        tx_id = str(result_payload.get("signature") or "").strip()
        if not tx_id:
            raise ValueError(f"pumpfun-cli returned no signature: {result_payload}")

        wallet_pubkey = _load_pumpfun_cli_wallet_pubkey(keyfile_path)
        tx_view = _await_confirmed_transaction_details(
            rpc_url=rpc_url,
            tx_id=tx_id,
            timeout_ms=config.live.order_timeout_ms,
            on_confirmed=lambda: _safe_transition(
                domain="order",
                trade_id=trade_id,
                token=intent.token,
                policy_track=intent.policy_track,
                mode=intent.mode,
                from_state="send_attempted",
                to_state="send_attempted",
                event="tx_confirmed",
                metadata={"path": "micro_live_pumpfun_cli", "tx_id": tx_id},
            ),
            on_fetched=lambda: _safe_transition(
                domain="order",
                trade_id=trade_id,
                token=intent.token,
                policy_track=intent.policy_track,
                mode=intent.mode,
                from_state="send_attempted",
                to_state="send_attempted",
                event="tx_fetch_confirmed",
                metadata={"path": "micro_live_pumpfun_cli", "tx_id": tx_id},
            ),
        )
        realized = _extract_realized_fill(
            tx_view=tx_view,
            wallet_pubkey=wallet_pubkey,
            mint=intent.token,
            intent=intent,
        )
        return ExecutionResult(
            intent=intent,
            status=OrderStatus.FILLED,
            deny_reason=None,
            realized_size_usd=realized.get("realized_size_usd"),
            realized_price=realized.get("realized_price"),
            filled_token_amount=realized.get("filled_token_amount"),
            slippage_bps_proxy=realized.get("slippage_bps_proxy"),
            tx_id=tx_id,
            external_order_id=tx_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "[MICRO_LIVE SEND FAILED]  %s %s  trade_id=%s  backend=pumpfun_cli  reason=%s",
            intent.action.value,
            intent.token[:8],
            trade_id,
            exc,
        )
        return _failed_result(intent, str(exc))


def _send_live_order_via_pumpfun_native(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_id: str,
    rpc_url: str,
) -> ExecutionResult:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    if config.live is None:
        return _failed_result(intent, "pumpfun-native send path requires live config")

    wallet_path = (config.live.wallet_path or "").strip()
    if not wallet_path:
        return _failed_result(intent, "LIVE_WALLET_PATH not set")

    priority_fee_micro_lamports = int(config.live.pumpfun_cli_priority_fee_micro_lamports or 5_000_000)
    slippage_bps = int(config.live.max_slippage_bps or 0)

    try:
        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state="send_attempted",
            to_state="send_attempted",
            event="pumpfun_native_send_started",
            metadata={
                "path": "micro_live_pumpfun_native",
                "action": intent.action.value,
                "policy_track": intent.policy_track,
                "intended_size_usd": intent.intended_size_usd,
                "priority_fee_micro_lamports": priority_fee_micro_lamports,
                "loaded_accounts_limit_bytes": config.live.pumpfun_native_loaded_accounts_limit_bytes,
                "require_preflight": bool(config.live.require_preflight),
            },
        )
        started_at = time.time()
        _buy_send_ts: Optional[float] = None

        # BUY: use minimum 1000 bps slippage so a 10% in-flight price spike
        # triggers error 6002 on-chain rather than filling at a bad price.
        buy_slippage_bps = max(slippage_bps, 1000) if intent.action == OrderAction.BUY else slippage_bps

        # Extract signal price for entry premium guard (10% cap).
        _signal_price: float = 0.0
        if intent.action == OrderAction.BUY and intent.quote_metadata:
            try:
                _signal_price = float(intent.quote_metadata.get("signal_price") or 0.0)
            except Exception:
                _signal_price = 0.0

        if intent.action == OrderAction.BUY:
            try:
                prepared = send_pumpfun_native_buy_transaction(
                    rpc_url=rpc_url,
                    wallet_path=wallet_path,
                    mint=intent.token,
                    intended_size_usd=float(intent.intended_size_usd),
                    sol_price_usd=float(risk_engine._fetch_sol_price_usd()),  # noqa: SLF001
                    slippage_bps=buy_slippage_bps,
                    require_preflight=bool(config.live.require_preflight),
                    compute_unit_price_micro_lamports=priority_fee_micro_lamports,
                    loaded_accounts_limit_bytes=int(config.live.pumpfun_native_loaded_accounts_limit_bytes),
                    signal_price=_signal_price,
                )
            except EntryPremiumAbort as _epa:
                if _epa.is_data_error:
                    log.warning(
                        "[GUARD_DATA_ERROR]  %s  trade_id=%s  posible unit mismatch  "
                        "premium=%dbps  current=%.10f  signal=%.10f — BUY skipped (safe side)",
                        intent.token[:8], trade_id,
                        _epa.premium_bps, _epa.current_price, _epa.signal_price,
                    )
                    _event = "entry_premium_guard_data_error"
                else:
                    log.warning(
                        "[ENTRY_PREMIUM_GUARD ABORT]  %s  trade_id=%s  premium=%dbps  "
                        "current=%.10f  signal=%.10f — BUY aborted",
                        intent.token[:8], trade_id,
                        _epa.premium_bps, _epa.current_price, _epa.signal_price,
                    )
                    _event = "entry_premium_guard_abort"
                _safe_transition(
                    domain="order",
                    trade_id=trade_id,
                    token=intent.token,
                    policy_track=intent.policy_track,
                    mode=intent.mode,
                    from_state="send_attempted",
                    to_state="failed",
                    event=_event,
                    reason=str(_epa),
                    metadata={
                        "premium_bps": _epa.premium_bps,
                        "current_price": _epa.current_price,
                        "signal_price": _epa.signal_price,
                        "is_data_error": _epa.is_data_error,
                    },
                )
                return _failed_result(intent, str(_epa))
            _buy_send_ts = time.time()
            elapsed_ms = round((_buy_send_ts - started_at) * 1000.0, 1)
            if elapsed_ms > BUY_LATENCY_WARN_MS:
                log.warning(
                    "BUY_LATENCY_REGRESSION  %s  trade_id=%s  build+send=%.1fms (> %dms baseline)",
                    intent.token[:8], trade_id, elapsed_ms, BUY_LATENCY_WARN_MS,
                )
            tx_id = prepared.signature
            native_meta: dict = {
                "path": "micro_live_pumpfun_native",
                "elapsed_ms": elapsed_ms,
                "tx_id": tx_id,
                "recent_blockhash": prepared.recent_blockhash,
                "token_amount_raw": prepared.token_amount_raw,
                "max_amount_lamports": prepared.max_amount_lamports,
                "fee_recipient": prepared.fee_recipient,
                "breaking_fee_recipient": prepared.breaking_fee_recipient,
                "token_program": prepared.token_program,
                "creator_vault": prepared.creator_vault,
                "bonding_curve": prepared.bonding_curve,
                "bonding_curve_v2": prepared.bonding_curve_v2,
            }
            wallet_pubkey_for_fill = prepared.wallet_pubkey
        else:
            # SELL path — resolve token amount from state or RPC balance.
            _raw_amount = _cached_position_token_amount(trade_id, intent.token)
            # token_amount in state is UI amount (6-decimal SPL); multiply to get raw lamports
            token_amount_raw = int(_raw_amount * 1_000_000) if _raw_amount is not None else 0
            if token_amount_raw <= 0:
                return _failed_result(
                    intent,
                    f"pumpfun_native SELL: no cached token amount for trade_id={trade_id} "
                    f"mint={intent.token[:8]} — cannot build sell tx",
                )
            tx_id = _send_sell_with_retry(
                rpc_url=rpc_url,
                wallet_path=wallet_path,
                mint=intent.token,
                token_amount_raw=token_amount_raw,
                slippage_bps=slippage_bps,
                require_preflight=bool(config.live.require_preflight),
                compute_unit_price_micro_lamports=priority_fee_micro_lamports,
                use_jito=bool(config.live.jito_enabled),
                jito_tip_lamports=int(config.live.jito_tip_lamports),
                trade_id=trade_id,
            )
            elapsed_ms = round((time.time() - started_at) * 1000.0, 1)
            if elapsed_ms > SELL_LATENCY_WARN_MS:
                log.warning(
                    "SELL_LATENCY_REGRESSION  %s  trade_id=%s  build+send=%.1fms (> %dms baseline)",
                    intent.token[:8], trade_id, elapsed_ms, SELL_LATENCY_WARN_MS,
                )
            native_meta = {
                "path": "micro_live_pumpfun_native",
                "elapsed_ms": elapsed_ms,
                "tx_id": tx_id,
                "token_amount_raw": token_amount_raw,
            }
            # Derive wallet pubkey for fill extraction (needed by _extract_realized_fill).
            from trading.execution.pumpfun_native import _load_wallet_keypair as _nlwk
            wallet_pubkey_for_fill = str(_nlwk(wallet_path).pubkey())
        _safe_transition(
            domain="order",
            trade_id=trade_id,
            token=intent.token,
            policy_track=intent.policy_track,
            mode=intent.mode,
            from_state="send_attempted",
            to_state="send_attempted",
            event="pumpfun_native_send_submitted",
            metadata=native_meta,
        )
        try:
            tx_view = _await_confirmed_transaction_details(
                rpc_url=rpc_url,
                tx_id=tx_id,
                timeout_ms=config.live.order_timeout_ms,
                on_confirmed=lambda: _safe_transition(
                    domain="order",
                    trade_id=trade_id,
                    token=intent.token,
                    policy_track=intent.policy_track,
                    mode=intent.mode,
                    from_state="send_attempted",
                    to_state="send_attempted",
                    event="tx_confirmed",
                    metadata={"path": "micro_live_pumpfun_native", "tx_id": tx_id},
                ),
                on_fetched=lambda: _safe_transition(
                    domain="order",
                    trade_id=trade_id,
                    token=intent.token,
                    policy_track=intent.policy_track,
                    mode=intent.mode,
                    from_state="send_attempted",
                    to_state="send_attempted",
                    event="tx_fetch_confirmed",
                    metadata={"path": "micro_live_pumpfun_native", "tx_id": tx_id},
                ),
            )
        except TimeoutError as _te:
            # tx confirmed on-chain but getTransaction timed out fetching details.
            # Only applies to BUY: the position IS open, tokens are in the wallet.
            # Return partial FILLED so open_position() is called with the known
            # tx_id — position will not be left orphaned with tx_id=null.
            # SELL timeouts propagate normally (no partial-fill recovery needed).
            if intent.action == OrderAction.BUY and "confirmed transaction details unavailable" in str(_te):
                log.warning(
                    "[MICRO_LIVE BUY PARTIAL FILL]  %s  trade_id=%s  tx_id=%s — "
                    "tx confirmed on-chain but getTransaction timed out; "
                    "opening position with estimated size, tx_id preserved",
                    intent.token[:8], trade_id, tx_id,
                )
                return ExecutionResult(
                    intent=intent,
                    status=OrderStatus.FILLED,
                    deny_reason=None,
                    realized_size_usd=intent.intended_size_usd,
                    realized_price=None,
                    filled_token_amount=None,
                    slippage_bps_proxy=None,
                    tx_id=tx_id,
                    external_order_id=tx_id,
                )
            raise
        _confirm_ts = time.time()
        realized = _extract_realized_fill(
            tx_view=tx_view,
            wallet_pubkey=wallet_pubkey_for_fill,
            mint=intent.token,
            intent=intent,
        )
        if intent.action == OrderAction.BUY and _buy_send_ts is not None:
            from datetime import datetime as _dt, timezone as _tz
            _append_latency_csv(
                trade_id=trade_id,
                mint=intent.token,
                policy_track=intent.policy_track,
                signal_timestamp=intent.timestamp,
                build_timestamp=_dt.fromtimestamp(started_at, tz=_tz.utc),
                send_timestamp=_dt.fromtimestamp(_buy_send_ts, tz=_tz.utc),
                confirm_timestamp=_dt.fromtimestamp(_confirm_ts, tz=_tz.utc),
                signal_mcap_usd=float(intent.intended_entry_mcap),
                realized_price_sol=realized.get("realized_price"),
                realized_size_usd=realized.get("realized_size_usd"),
                intended_size_usd=float(intent.intended_size_usd),
            )
        return ExecutionResult(
            intent=intent,
            status=OrderStatus.FILLED,
            deny_reason=None,
            realized_size_usd=realized.get("realized_size_usd"),
            realized_price=realized.get("realized_price"),
            filled_token_amount=realized.get("filled_token_amount"),
            slippage_bps_proxy=realized.get("slippage_bps_proxy"),
            tx_id=tx_id,
            external_order_id=tx_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "[MICRO_LIVE SEND FAILED]  %s %s  trade_id=%s  backend=pumpfun_native  reason=%s",
            intent.action.value,
            intent.token[:8],
            trade_id,
            exc,
        )
        return _failed_result(intent, str(exc))



def _resolve_pumpfun_cli_command(config: ExecutionConfig) -> tuple[list[str], dict[str, str], str]:
    if config.live is None:
        raise ValueError("pumpfun-cli command resolution requires live config")

    executable = (config.live.pumpfun_cli_executable or "").strip()
    if executable:
        return [executable], {}, "explicit_executable"

    repo_dir = (config.live.pumpfun_cli_repo_dir or "").strip()
    if repo_dir:
        src_dir = Path(repo_dir) / "src"
        pythonpath_parts = [str(src_dir)]
        existing_pythonpath = os.getenv("PYTHONPATH", "").strip()
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env = {"PYTHONPATH": os.pathsep.join(pythonpath_parts)}

        # pumpfun-cli does not currently execute its Typer app when invoked as
        # `python -m pumpfun_cli.cli`; that path exits 0 with empty stdout and
        # no trade. Prefer the packaged console script whenever a repo venv is
        # configured.
        venv_pumpfun = Path(repo_dir) / ".venv" / "bin" / "pumpfun"
        if venv_pumpfun.exists():
            return [str(venv_pumpfun)], env, "repo_venv_console_script"

        return ["pumpfun"], env, "path_lookup_with_repo_pythonpath"

    return ["pumpfun"], {}, "path_lookup"


def _pumpfun_cli_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout and stderr:
        return f"stdout={stdout[:240]} stderr={stderr[:240]}"
    if stdout:
        return stdout[:240]
    if stderr:
        return stderr[:240]
    return "no stdout/stderr"


def _parse_pumpfun_cli_json_output(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("pumpfun-cli returned empty stdout")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pumpfun-cli returned non-JSON stdout: {text[:240]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"pumpfun-cli returned unexpected JSON payload: {payload!r}")
    return payload


def _load_pumpfun_cli_wallet_pubkey(keyfile_path: str) -> str:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    payload = json.loads(Path(keyfile_path).read_text(encoding="utf-8"))
    pubkey = str(payload.get("pubkey") or "").strip()
    if not pubkey:
        raise ValueError(f"pumpfun-cli keyfile {keyfile_path} missing pubkey")
    return pubkey


def _append_latency_csv(
    *,
    trade_id: str,
    mint: str,
    policy_track: str,
    signal_timestamp: "datetime",
    build_timestamp: "datetime",
    send_timestamp: "datetime",
    confirm_timestamp: "datetime",
    signal_mcap_usd: Optional[float],
    realized_price_sol: Optional[float],
    realized_size_usd: Optional[float],
    intended_size_usd: float,
) -> None:
    import csv as _csv
    _FIELDS = [
        "trade_id", "mint", "policy_track",
        "signal_timestamp", "build_timestamp", "send_timestamp", "confirm_timestamp",
        "signal_to_build_ms", "build_to_send_ms", "send_to_confirm_ms", "signal_to_fill_ms",
        "signal_mcap_usd", "realized_price_sol", "realized_size_usd", "intended_size_usd",
    ]
    def _ms(a: "datetime", b: "datetime") -> float:
        return round((b.timestamp() - a.timestamp()) * 1000.0, 1)
    row = {
        "trade_id": trade_id,
        "mint": mint,
        "policy_track": policy_track,
        "signal_timestamp": signal_timestamp.isoformat(),
        "build_timestamp": build_timestamp.isoformat(),
        "send_timestamp": send_timestamp.isoformat(),
        "confirm_timestamp": confirm_timestamp.isoformat(),
        "signal_to_build_ms": _ms(signal_timestamp, build_timestamp),
        "build_to_send_ms": _ms(build_timestamp, send_timestamp),
        "send_to_confirm_ms": _ms(send_timestamp, confirm_timestamp),
        "signal_to_fill_ms": _ms(signal_timestamp, confirm_timestamp),
        "signal_mcap_usd": signal_mcap_usd,
        "realized_price_sol": realized_price_sol,
        "realized_size_usd": realized_size_usd,
        "intended_size_usd": intended_size_usd,
    }
    try:
        _path = runtime_root() / "logs" / "latency_analysis.csv"
        _exists = _path.exists()
        _path.parent.mkdir(parents=True, exist_ok=True)
        with _path.open("a", newline="") as _f:
            _w = _csv.DictWriter(_f, fieldnames=_FIELDS)
            if not _exists:
                _w.writeheader()
            _w.writerow(row)
        log.info(
            "[LATENCY_ANALYSIS] signal_to_fill=%.0fms  build_to_send=%.0fms  send_to_confirm=%.0fms  trade_id=%s",
            row["signal_to_fill_ms"], row["build_to_send_ms"], row["send_to_confirm_ms"], trade_id,
        )
    except Exception:
        pass


def _is_sell_6003_error(exc: Exception) -> bool:
    """Return True when exc is the pump.fun 6003 TooLittleSolReceived SELL slippage error."""
    text = str(exc)
    return "6003" in text or "TooLittleSolReceived" in text or "0x1773" in text


def _send_sell_with_retry(
    *,
    rpc_url: str,
    wallet_path: str,
    mint: str,
    token_amount_raw: int,
    slippage_bps: int,
    require_preflight: bool,
    compute_unit_price_micro_lamports: int,
    use_jito: bool = False,
    jito_tip_lamports: int = 50_000,
    trade_id: str,
    max_retries: int = 3,
    retry_delay_s: float = 1.0,
) -> str:
    """Send a SELL transaction with automatic retry on 6003 (TooLittleSolReceived).

    Each retry calls send_pumpfun_native_sell_transaction from scratch, which
    re-fetches the bonding curve and recomputes min_sol_output from the fresh
    on-chain reserves — no stale price is carried across attempts.

    When use_jito=True, a Jito bundle is attempted first with a 15-second
    confirmation poll. If the bundle is not confirmed within that window (or
    the send raises), the function falls back to the standard RPC path
    automatically — no caller change needed.

    Non-6003 errors on the RPC path are re-raised immediately (no retry).

    On full exhaustion, writes state/sell_failed_exhausted.json for the
    supervisor and raises with a SELL_FAILED_EXHAUSTED prefix.
    """
    # ── Jito attempt with fast confirmation poll ──────────────────────────────
    if use_jito:
        try:
            jito_sig = send_pumpfun_native_sell_transaction(
                rpc_url=rpc_url,
                wallet_path=wallet_path,
                mint=mint,
                token_amount_raw=token_amount_raw,
                slippage_bps=slippage_bps,
                require_preflight=require_preflight,
                compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
                use_jito=True,
                jito_tip_lamports=jito_tip_lamports,
            )
            from trading.execution.pumpfun_native import confirm_transaction as _jito_confirm
            if _jito_confirm(rpc_url, jito_sig, timeout_s=5.0, poll_interval_s=1.0):
                log.info(
                    "[SELL_JITO_OK] bundle confirmed  trade_id=%s  mint=%s  sig=%s",
                    trade_id, mint[:8], jito_sig,
                )
                return jito_sig
            log.warning(
                "[SELL_JITO_TIMEOUT] bundle not confirmed in 5s  trade_id=%s  mint=%s — "
                "falling back to RPC sendTransaction",
                trade_id, mint[:8],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[SELL_JITO_FAILED] Jito send error  trade_id=%s  mint=%s  reason=%s — "
                "falling back to RPC sendTransaction",
                trade_id, mint[:8], exc,
            )

    # ── RPC path: initial (use_jito=False) or Jito fallback ──────────────────
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 2):  # attempts 1..max_retries+1
        try:
            return send_pumpfun_native_sell_transaction(
                rpc_url=rpc_url,
                wallet_path=wallet_path,
                mint=mint,
                token_amount_raw=token_amount_raw,
                slippage_bps=slippage_bps,
                require_preflight=require_preflight,
                compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
                use_jito=False,
                jito_tip_lamports=0,
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_sell_6003_error(exc):
                raise
            last_exc = exc
            remaining = max_retries + 1 - attempt
            log.warning(
                "[SELL_RETRY] 6003 TooLittleSolReceived on attempt %d/%d  "
                "trade_id=%s  mint=%s  remaining=%d  error=%s",
                attempt, max_retries + 1, trade_id, mint[:8], remaining, exc,
            )
            if remaining > 0:
                time.sleep(retry_delay_s)

    # All attempts exhausted — write supervisor notification and raise.
    exhausted_msg = (
        f"SELL_FAILED_EXHAUSTED: 6003 after {max_retries + 1} attempts  "
        f"trade_id={trade_id}  mint={mint[:8]}"
    )
    log.error("[SELL_FAILED_EXHAUSTED]  trade_id=%s  mint=%s  last_error=%s", trade_id, mint[:8], last_exc)
    try:
        import datetime as _dt
        _state_dir = runtime_root() / "state"
        _state_dir.mkdir(parents=True, exist_ok=True)
        (_state_dir / "sell_failed_exhausted.json").write_text(
            json.dumps({
                "trade_id": trade_id,
                "mint": mint,
                "attempts": max_retries + 1,
                "last_error": str(last_exc),
                "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass  # notification write failure must never block the raise
    raise ValueError(exhausted_msg) from last_exc


def _failed_result(intent: ExecutionIntent, details: str) -> ExecutionResult:
    return ExecutionResult(
        intent=intent,
        status=OrderStatus.FAILED,
        deny_reason=None,
        failure_details=details,
    )


def _should_retry_sell_with_auto_pool(
    intent: ExecutionIntent,
    pool: str,
    exc: Exception,
) -> bool:
    """Return True when a SELL should retry with pool=auto after pump fails.

    Original logic only retried on a few known migration signatures. In real
    recovery flow we also observed generic `trade API HTTP 400: Bad Request`
    from `pool=pump` while the exact same SELL request succeeded with
    `pool=auto`. For SELLs only, treat that generic pump-builder 400 as a
    retryable condition too.
    """
    if intent.action != OrderAction.SELL:
        return False
    if (pool or "").strip().lower() != "pump":
        return False
    text = str(exc)
    lower = text.lower()
    return (
        "BondingCurveComplete" in text
        or "custom program error: 0x1775" in text
        or "liquidity migrated to raydium" in lower
        or ("trade api http 400" in lower and "bad request" in lower)
    )


def _should_fallback_trade_api_to_pumpfun_native(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    trade_api_url: str,
    exc: Exception,
) -> bool:
    if intent.action != OrderAction.BUY:
        return False
    if config.live is None:
        return False
    if not (config.live.wallet_path or "").strip():
        return False

    lower_url = (trade_api_url or "").strip().lower()
    if not lower_url:
        return False
    if not (
        "/pump-fun/swap" in lower_url
        or "quicknode" in lower_url
        or "quiknode.pro" in lower_url
        or "metis" in lower_url
    ):
        return False

    lower = str(exc).lower()
    return any(
        token in lower
        for token in (
            "trade api http 401",
            "trade api http 402",
            "trade api http 403",
            "trade api http 429",
            "payment required",
            "quota",
            "rate limit",
            "credits",
            "subscription",
            "unauthorized",
            "forbidden",
        )
    )


def _build_buy_6002_retry_intents(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> list[ExecutionIntent]:
    """Build smaller BUY retry intents for bounded 6002 recovery.

    Disabled by default. When enabled, this does not widen slippage tolerance.
    It only tries smaller notional sizes after a retryable 6002 failure.
    """
    live = config.live
    if live is None or intent.action != OrderAction.BUY:
        return []
    if not live.buy_6002_retry_enabled:
        return []

    retry_count = max(0, int(live.buy_6002_retry_count or 0))
    shrink_factor = float(live.buy_6002_retry_shrink_factor or 0.0)
    min_size_usd = max(0.0, float(live.buy_6002_retry_min_size_usd or 0.0))

    if retry_count <= 0:
        return []
    if not (0.0 < shrink_factor < 1.0):
        return []

    retry_intents: list[ExecutionIntent] = []
    next_size_usd = float(intent.intended_size_usd)
    for retry_index in range(1, retry_count + 1):
        next_size_usd *= shrink_factor
        if next_size_usd <= 0:
            break
        if min_size_usd > 0 and next_size_usd < min_size_usd:
            break
        extra = dict(intent.extra or {})
        extra.update(
            {
                "buy_6002_retry": True,
                "buy_6002_retry_index": retry_index,
                "buy_6002_retry_original_size_usd": intent.intended_size_usd,
            }
        )
        retry_intents.append(
            replace(
                intent,
                intended_size_usd=next_size_usd,
                extra=extra,
            )
        )
    return retry_intents


def _should_retry_buy_after_6002(
    intent: ExecutionIntent,
    exc: Exception,
) -> bool:
    if intent.action != OrderAction.BUY:
        return False
    text = str(exc)
    lower = text.lower()
    return (
        "toomuchsolrequired" in lower
        or "error code: 6002" in lower
        or "custom program error: 0x1772" in lower
        or "{'custom': 6002}" in lower
        or '"custom": 6002' in lower
    )


def _buy_amount_for_trade_api(intent: ExecutionIntent) -> tuple[str, str]:
    sol_price_usd = risk_engine._fetch_sol_price_usd()  # noqa: SLF001
    sol_amount = Decimal(str(intent.intended_size_usd)) / Decimal(str(sol_price_usd))
    if sol_amount <= 0:
        raise ValueError("computed SOL buy amount <= 0")
    return (_decimal_to_api_string(sol_amount), "true")


def _sell_amount_for_trade_api(
    rpc_url: str,
    wallet_pubkey: str,
    mint: str,
    cached_token_amount: Optional[float] = None,
    visibility_timeout_ms: int = 5000,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve SELL size from the wallet, with brief retries after fresh fills.

    A BUY can confirm on-chain slightly before RPC token-account / mint lookups
    become queryable for the immediate follow-on SELL. We therefore allow a
    short visibility window before failing closed.
    """
    if cached_token_amount is not None:
        try:
            cached = Decimal(str(cached_token_amount))
        except (InvalidOperation, ValueError):
            cached = Decimal("0")
        if cached > 0:
            return (
                _decimal_to_api_string(cached),
                "false",
                {
                    "visibility_timeout_ms": visibility_timeout_ms,
                    "poll_count": 0,
                    "zero_balance_reads": 0,
                    "error_reads": 0,
                    "balance_visible_after_ms": 0.0,
                    "amount_source": "cached_buy_token_amount",
                },
            )

    started_at = time.time()
    deadline = started_at + max(1.0, visibility_timeout_ms / 1000.0)
    last_error: Optional[Exception] = None
    poll_count = 0
    zero_balance_reads = 0
    error_reads = 0
    while time.time() < deadline:
        poll_count += 1
        try:
            token_amount = _get_wallet_token_balance(
                rpc_url=rpc_url,
                wallet_pubkey=wallet_pubkey,
                mint=mint,
            )
            if token_amount > 0:
                return (
                    _decimal_to_api_string(token_amount),
                    "false",
                    {
                        "visibility_timeout_ms": visibility_timeout_ms,
                        "poll_count": poll_count,
                        "zero_balance_reads": zero_balance_reads,
                        "error_reads": error_reads,
                        "balance_visible_after_ms": round((time.time() - started_at) * 1000.0, 1),
                        "amount_source": "wallet_token_balance",
                    },
                )
            zero_balance_reads += 1
            last_error = ValueError(f"wallet token balance is zero for mint {mint}")
        except Exception as exc:  # noqa: BLE001
            error_reads += 1
            last_error = exc
        time.sleep(0.5)

    detail = (
        f"polls={poll_count} zero_balance_reads={zero_balance_reads} "
        f"error_reads={error_reads} visibility_timeout_ms={visibility_timeout_ms}"
    )
    if last_error is not None:
        raise ValueError(
            f"token balance not queryable for mint {mint} after visibility wait: {last_error} ({detail})"
        )
    raise ValueError(f"wallet token balance is zero for mint {mint} after visibility wait ({detail})")


def _slippage_percent(config: ExecutionConfig) -> str:
    if config.live is None:
        raise ValueError("live config missing while computing slippage")
    bps = max(1, int(config.live.max_slippage_bps or 0))
    return _decimal_to_api_string(Decimal(bps) / Decimal(100))


def _decimal_to_api_string(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000000001"))
    text = format(normalized.normalize(), "f")
    return text.rstrip("0").rstrip(".") or "0"


def _load_wallet_keypair(wallet_path: str):
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    from solders.keypair import Keypair

    raw = json.loads(Path(wallet_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"wallet file {wallet_path} is not a JSON int array")
    secret = bytes(int(x) for x in raw)
    return Keypair.from_bytes(secret)


def _request_trade_local_transaction(
    trade_api_url: str,
    wallet_pubkey: str,
    intent: ExecutionIntent,
    amount: str,
    denominated_in_sol: str,
    slippage_percent: str,
    priority_fee_sol: float,
    pool: str,
    timeout_ms: int,
) -> bytes:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    import requests

    timeout_s = max(1.0, timeout_ms / 1000.0)
    lower_url = trade_api_url.lower()

    if "/pump-fun/swap" in lower_url:
        payload = {
            "wallet": wallet_pubkey,
            "type": intent.action.value.upper(),
            "mint": intent.token,
            "inAmount": _amount_for_metis_pump_fun(intent, amount, denominated_in_sol),
            "slippageBps": str(max(1, int((Decimal(slippage_percent) * Decimal(100)).quantize(Decimal("1"))))),
        }
        priority_level = _priority_fee_level_for_metis(priority_fee_sol)
        if priority_level:
            payload["priorityFeeLevel"] = priority_level

        resp = requests.post(
            trade_api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
    else:
        data = {
            "publicKey": wallet_pubkey,
            "action": intent.action.value.lower(),
            "mint": intent.token,
            "amount": amount,
            "denominatedInSol": denominated_in_sol,
            "slippage": slippage_percent,
            "priorityFee": _decimal_to_api_string(Decimal(str(priority_fee_sol))),
            "pool": pool or "pump",
        }
        resp = requests.post(
            trade_api_url,
            data=data,
            timeout=timeout_s,
        )

    if resp.status_code >= 400:
        raise ValueError(
            f"trade API HTTP {resp.status_code}: {resp.text[:240]}"
        )

    content_type = (resp.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"trade API returned invalid JSON payload: {exc}") from exc

        for key in ("transaction", "swapTransaction", "serializedTransaction"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    return base64.b64decode(value)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"trade API JSON field {key} is not valid base64: {exc}") from exc

        raise ValueError(f"trade API JSON payload missing serialized transaction field: {body}")

    if not resp.content:
        raise ValueError("trade API returned empty transaction payload")
    return resp.content


def _amount_for_metis_pump_fun(
    intent: ExecutionIntent,
    amount: str,
    denominated_in_sol: str,
) -> str:
    raw = Decimal(str(amount))
    if raw <= 0:
        raise ValueError(f"invalid trade amount for Metis: {amount!r}")

    if denominated_in_sol == "true":
        return str(int((raw * Decimal(LAMPORTS_PER_SOL)).quantize(Decimal("1"))))

    # Pump.fun docs specify 6 decimals for token amounts on SELL.
    return str(int((raw * Decimal(1_000_000)).quantize(Decimal("1"))))


def _priority_fee_level_for_metis(priority_fee_sol: float) -> Optional[str]:
    fee = Decimal(str(priority_fee_sol))
    if fee <= Decimal("0"):
        return None
    if fee < Decimal("0.0005"):
        return "low"
    if fee < Decimal("0.001"):
        return "medium"
    if fee < Decimal("0.005"):
        return "high"
    return "extreme"


def _sign_versioned_transaction(unsigned_tx_bytes: bytes, keypair) -> str:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    from solders.transaction import VersionedTransaction

    unsigned_tx = VersionedTransaction.from_bytes(unsigned_tx_bytes)
    signed_tx = VersionedTransaction(unsigned_tx.message, [keypair])
    return base64.b64encode(bytes(signed_tx)).decode("ascii")


def _broadcast_signed_transaction(
    rpc_url: str,
    signed_tx_b64: str,
) -> str:
    raise RuntimeError("Live execution and wallet access are disabled in this public release")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            signed_tx_b64,
            {
                "encoding": "base64",
                "preflightCommitment": "confirmed",
            },
        ],
    }
    data = _rpc_post(rpc_url, payload)
    result = data.get("result")
    if not result:
        raise ValueError(f"sendTransaction returned no signature: {data}")
    return str(result)


def _await_confirmed_transaction_details(
    rpc_url: str,
    tx_id: str,
    timeout_ms: int,
    on_confirmed: Optional[Callable[[], None]] = None,
    on_fetched: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Wait for confirmation and confirmed tx details in one polling loop.

    This preserves the existing fail-closed semantics but avoids a second
    serialized polling phase after confirmation is already known, which can add
    an extra sleep quantum before the adapter records FILLED.
    """
    deadline = time.time() + max(1.0, timeout_ms / 1000.0)
    confirmed_seen = False
    confirmed_emitted = False
    while time.time() < deadline:
        if not confirmed_seen:
            data = _rpc_post(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[tx_id], {"searchTransactionHistory": True}],
                },
            )
            value = ((data.get("result") or {}).get("value") or [None])[0]
            if value:
                if value.get("err") is not None:
                    raise ValueError(_describe_onchain_failure(rpc_url, tx_id, value["err"]))
                confirmed_seen = value.get("confirmationStatus") in {"confirmed", "finalized"}
                if confirmed_seen and not confirmed_emitted:
                    confirmed_emitted = True
                    if on_confirmed is not None:
                        on_confirmed()

        if confirmed_seen:
            data = _rpc_post(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        tx_id,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "confirmed",
                        },
                    ],
                },
            )
            result = data.get("result")
            if result:
                if on_fetched is not None:
                    on_fetched()
                return result

        time.sleep(0.5)
    if confirmed_seen:
        raise TimeoutError(f"confirmed transaction details unavailable for {tx_id}")
    raise TimeoutError(f"transaction confirmation timed out for {tx_id}")


def _describe_onchain_failure(rpc_url: str, tx_id: str, err: Any) -> str:
    detail = f"transaction failed on-chain: {err}"
    try:
        data = _rpc_post(
            rpc_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    tx_id,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed",
                    },
                ],
            },
        )
        result = data.get("result") or {}
        meta = result.get("meta") or {}
        log_messages = meta.get("logMessages") or []
        anchor_error = next((msg for msg in log_messages if "TooMuchSolRequired" in msg or "AnchorError thrown" in msg), "")
        left = _extract_logged_amount(log_messages, "Program log: Left:")
        right = _extract_logged_amount(log_messages, "Program log: Right:")
        parts: list[str] = []
        if anchor_error:
            parts.append(anchor_error)
        if left is not None:
            parts.append(f"Left={int(left)}")
        if right is not None:
            parts.append(f"Right={int(right)}")
        if left is not None and right is not None and left > 0 and right > left:
            over_cap_bps = ((right / left) - 1.0) * 10000.0
            parts.append(f"observed_over_cap_bps={over_cap_bps:.1f}")
        if parts:
            detail += " | " + " | ".join(parts)
    except Exception:
        pass
    return detail


def _extract_logged_amount(log_messages: list[Any], prefix: str) -> Optional[float]:
    for msg in log_messages:
        if not isinstance(msg, str) or prefix not in msg:
            continue
        try:
            return float(msg.split(prefix, 1)[1].strip())
        except Exception:
            continue
    return None


def _extract_realized_fill(
    tx_view: dict[str, Any],
    wallet_pubkey: str,
    mint: str,
    intent: ExecutionIntent,
) -> dict[str, Optional[float]]:
    meta = tx_view.get("meta") or {}
    msg = ((tx_view.get("transaction") or {}).get("message") or {})
    account_keys = msg.get("accountKeys") or []

    wallet_index = None
    for idx, item in enumerate(account_keys):
        if isinstance(item, dict):
            pubkey = item.get("pubkey")
        else:
            pubkey = item
        if pubkey == wallet_pubkey:
            wallet_index = idx
            break
    if wallet_index is None:
        raise ValueError("wallet pubkey not present in confirmed transaction")

    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
        raise ValueError("wallet lamport balances missing in confirmed transaction")

    sol_delta = Decimal(post_balances[wallet_index] - pre_balances[wallet_index]) / Decimal(LAMPORTS_PER_SOL)
    token_delta = _token_balance_delta(meta, wallet_pubkey, mint)
    quote_price = _quote_price_decimal(intent)
    sol_price_usd = Decimal(str(risk_engine._fetch_sol_price_usd()))  # noqa: SLF001

    realized_price: Optional[float] = None
    if token_delta != 0:
        realized_price = float(abs(sol_delta) / abs(token_delta))
    elif quote_price is not None:
        realized_price = float(quote_price)

    realized_size_usd: Optional[float] = None
    if sol_delta != 0:
        realized_size_usd = float(abs(sol_delta) * sol_price_usd)
    elif intent.intended_size_usd > 0:
        realized_size_usd = intent.intended_size_usd

    slippage_bps_proxy: Optional[int] = None
    if realized_price is not None and quote_price is not None and quote_price > 0:
        slip = (Decimal(str(realized_price)) / quote_price) - Decimal("1")
        slippage_bps_proxy = int(abs(slip) * Decimal(10000))

    filled_token_amount: Optional[float] = None
    if token_delta != 0:
        filled_token_amount = float(abs(token_delta))

    return {
        "realized_price": realized_price,
        "realized_size_usd": realized_size_usd,
        "filled_token_amount": filled_token_amount,
        "slippage_bps_proxy": slippage_bps_proxy,
    }


def _cached_position_token_amount(trade_id: str, mint: str) -> Optional[float]:
    """Return the confirmed BUY token amount stored for an open position.

    This avoids waiting on fresh token-account visibility before an immediate
    SELL. The value comes from confirmed transaction metadata, so it is often
    available before RPC token-account scans catch up.
    """
    try:
        from trading.execution import state as _state  # local import to avoid cycles

        pos = (_state.get_all_positions() or {}).get(trade_id) or {}
        if pos.get("status") != "open" or pos.get("token") != mint:
            return None
        raw = pos.get("token_amount")
        if raw in (None, ""):
            return None
        amount = float(raw)
        return amount if amount > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.debug("adapter: cached token amount lookup failed: %s", exc)
        return None


def _quote_price_decimal(intent: ExecutionIntent) -> Optional[Decimal]:
    raw = (intent.quote_metadata or {}).get("signal_price")
    if raw in (None, ""):
        return None
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return price if price > 0 else None


def _token_balance_delta(meta: dict[str, Any], wallet_pubkey: str, mint: str) -> Decimal:
    pre = _collect_token_balances(meta.get("preTokenBalances") or [], wallet_pubkey, mint)
    post = _collect_token_balances(meta.get("postTokenBalances") or [], wallet_pubkey, mint)
    return post - pre


def _collect_token_balances(entries: list[dict[str, Any]], wallet_pubkey: str, mint: str) -> Decimal:
    total = Decimal("0")
    for entry in entries:
        if entry.get("mint") != mint:
            continue
        owner = entry.get("owner")
        if owner and owner != wallet_pubkey:
            continue
        token_amount = (entry.get("uiTokenAmount") or {})
        amt = token_amount.get("uiAmountString")
        if amt is None:
            raw_amt = token_amount.get("amount")
            decimals = int(token_amount.get("decimals") or 0)
            if raw_amt is None:
                continue
            total += Decimal(str(raw_amt)) / (Decimal(10) ** decimals)
        else:
            total += Decimal(str(amt))
    return total


def _get_wallet_token_balance(
    rpc_url: str,
    wallet_pubkey: str,
    mint: str,
) -> Decimal:
    """Return wallet balance for a mint across both SPL token programs.

    Some pump.fun tokens land under Token-2022 (`Tokenz...`) rather than the
    legacy Token Program (`Tokenkeg...`). A mint-filtered RPC query can fail
    for the immediate follow-on SELL even when the BUY filled correctly.
    Scanning both program IDs and filtering by parsed mint avoids that blind
    spot while staying compatible with legacy tokens.
    """
    total = Decimal("0")
    saw_matching_account = False
    lookup_errors: list[str] = []

    for program_id in TOKEN_PROGRAM_IDS:
        try:
            data = _rpc_post(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet_pubkey,
                        {"programId": program_id},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            lookup_errors.append(f"{program_id}: {exc}")
            continue

        result = (data.get("result") or {}).get("value") or []
        for item in result:
            parsed = (((item.get("account") or {}).get("data") or {}).get("parsed") or {})
            info = parsed.get("info") or {}
            if info.get("mint") != mint:
                continue

            saw_matching_account = True
            token_amount = info.get("tokenAmount") or {}
            amt = token_amount.get("uiAmountString")
            if amt is None:
                raw_amt = token_amount.get("amount")
                decimals = int(token_amount.get("decimals") or 0)
                if raw_amt is None:
                    continue
                total += Decimal(str(raw_amt)) / (Decimal(10) ** decimals)
            else:
                total += Decimal(str(amt))

    if total > 0 or saw_matching_account:
        return total

    if lookup_errors and len(lookup_errors) == len(TOKEN_PROGRAM_IDS):
        raise ValueError(
            "rpc token-account lookups failed: " + " | ".join(lookup_errors)
        )

    return total


def _rpc_post(rpc_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    from trading.safety import require_read_only_rpc; require_read_only_rpc(payload)
    import requests

    resp = requests.post(
        rpc_url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error") is not None:
        raise ValueError(f"rpc error: {data['error']}")
    return data


def _safe_check_pending_exit(trade_id: str, config: "ExecutionConfig") -> None:
    """After a confirmed BUY fill: fire any pending exit queued for this trade_id.

    This is the primary resolution path for the paper→live race condition.

    Race scenario:
      1. Paper BUY fires → live BUY attempt starts (blocking RPC send).
      2. Paper evaluate_exit() triggers while BUY is still in-flight — no live
         position exists in state yet → exit was queued instead of dropped.
      3. BUY fill confirmed → _safe_state_update() persists the open position
         → this function is called → pending exit found → SELL fired immediately.

    Hardening (deferred-SELL loss prevention):
      The pending exit is popped atomically at the start to prevent concurrent
      double-fire with the orphan supervisor.  If the SELL does NOT reach FILLED
      (DENIED by a risk gate, or FAILED on a send/quote/slippage path), the
      entry is re-queued immediately so the orphan supervisor can retry on its
      next sweep.  ``popped_exit`` is set to None once the entry is either
      consumed (FILLED) or successfully re-queued, so the exception handler
      only re-queues in genuinely unexpected failure paths (e.g. risk engine
      raises before returning a result).

    If no pending exit was queued (the common case — paper held for ≥1 tick),
    this is a fast no-op (single state file read).
    """
    popped_exit: Optional[dict] = None
    try:
        from trading.execution import state as _state

        popped_exit = _state.pop_pending_exit(trade_id)
        if popped_exit is None:
            return  # common path — no pending exit queued

        attempt = int(popped_exit.get("deferred_attempt", 0)) + 1
        log.warning(
            "[PENDING EXIT CLAIMED]  trade_id=%s  mint=%s  queued_at=%s  attempt=%d — "
            "paper exit fired before live BUY fill; emitting deferred SELL now",
            trade_id,
            str(popped_exit.get("mint", "?"))[:8],
            popped_exit.get("queued_at", "?"),
            attempt,
        )

        # Refresh signal_time_s to now so the quote-freshness gate does not
        # reject the SELL as stale (the original signal_time_s was at paper-
        # exit time, which may be several seconds in the past).
        from trading.execution.quote import make_signal_quote
        sell_intent = ExecutionIntent(
            action               = OrderAction.SELL,
            token                = popped_exit["mint"],
            policy_track         = popped_exit.get("track_name", "unknown"),
            intended_size_usd    = float(popped_exit.get("stake_usd", 0.0)),
            intended_entry_mcap  = float(popped_exit.get("entry_mcap", 0.0)),
            pressure             = float(popped_exit.get("pressure", 0.0)),
            mode                 = config.mode.value,
            quote_metadata       = make_signal_quote(
                signal_price  = float(popped_exit.get("signal_price", 0.0)),
                signal_time_s = time.time(),   # refresh — must not be stale
                entry_mcap    = float(popped_exit.get("entry_mcap", 0.0)),
            ),
        )

        sell_result = _micro_live_execute(sell_intent, config, trade_id=trade_id)

        if sell_result.status == OrderStatus.FILLED:
            # Entry already consumed by pop above; position is closed by state update.
            log.info(
                "[DEFERRED SELL FILLED]  trade_id=%s  tx_id=%s  attempt=%d — "
                "pending exit resolved",
                trade_id, sell_result.tx_id, attempt,
            )
            popped_exit = None  # consumed — exception handler must not re-queue

        else:
            # DENIED or FAILED: re-queue so orphan supervisor can retry.
            # The pop already removed the entry; re-queuing restores recoverability
            # without double-fire risk (the supervisor's pop will claim it
            # exclusively on its next sweep).
            log.warning(
                "[DEFERRED SELL NOT FILLED]  trade_id=%s  status=%s  attempt=%d — "
                "re-queuing pending exit for orphan supervisor retry",
                trade_id, sell_result.status.value, attempt,
            )
            _state.queue_pending_exit(
                trade_id=trade_id,
                exit_info={**popped_exit, "deferred_attempt": attempt},
            )
            popped_exit = None  # re-queued — exception handler must not act again

    except Exception as exc:  # noqa: BLE001
        log.error(
            "adapter: _safe_check_pending_exit failed for trade_id=%s: %s — "
            "orphan supervisor will retry on next 10-second sweep",
            trade_id, exc,
        )
        # If the entry was popped before the exception but has not yet been
        # consumed (FILLED) or re-queued (non-FILLED normal path), restore it
        # so the orphan supervisor can retry.  This guards against unexpected
        # raises from risk_engine or quote construction.
        if popped_exit is not None:
            _safe_requeue_pending_exit(
                trade_id,
                popped_exit,
                int(popped_exit.get("deferred_attempt", 0)) + 1,
            )


def _safe_requeue_pending_exit(
    trade_id: str,
    exit_info: dict,
    attempt: int,
) -> None:
    """Re-queue a pending exit that was popped but could not be fulfilled.

    Called from the exception handler in _safe_check_pending_exit when the
    entry was atomically popped but the SELL raised unexpectedly before a
    result was returned.  A second failure here is logged as CRITICAL.
    """
    try:
        from trading.execution import state as _state
        _state.queue_pending_exit(
            trade_id=trade_id,
            exit_info={**exit_info, "deferred_attempt": attempt},
        )
        log.warning(
            "adapter: re-queued pending exit after unexpected error  "
            "trade_id=%s  attempt=%d",
            trade_id, attempt,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "adapter: CRITICAL — failed to re-queue pending exit for "
            "trade_id=%s after exception: %s — position may be orphaned; "
            "manual intervention required",
            trade_id, exc,
        )


def _safe_queue_failed_sell_retry(
    intent: ExecutionIntent,
    trade_id: str,
    result: ExecutionResult,
) -> None:
    """Queue retry for a failed live SELL when the position is still open.

    This closes the gap where an immediate micro_live SELL can fail after a
    real BUY fill exists in state, leaving the lane blocked with no
    pending-exit entry for the orphan supervisor to recover.
    """
    if not trade_id:
        return
    try:
        from trading.execution import state as _state

        positions = _state.get_all_positions()
        pos = positions.get(trade_id)
        if not pos or pos.get("status") != "open":
            return

        pending = _state.get_pending_exits()
        if trade_id in pending:
            return

        signal_price = 0.0
        if intent.quote_metadata is not None:
            try:
                signal_price = float(intent.quote_metadata.get("signal_price") or 0.0)
            except Exception:
                signal_price = 0.0

        _state.queue_pending_exit(
            trade_id=trade_id,
            exit_info={
                "trade_id": trade_id,
                "mint": intent.token,
                "track_name": intent.policy_track,
                "stake_usd": float(pos.get("size_usd") or intent.intended_size_usd or 0.0),
                "entry_mcap": float(pos.get("entry_mcap") or intent.intended_entry_mcap or 0.0),
                "pressure": float(pos.get("pressure") or intent.pressure or 0.0),
                "signal_price": signal_price,
                "signal_time_s": time.time(),
                "source": "failed_live_sell_after_open_position",
                "mode": intent.mode,
                "failure_details": result.failure_details or result.deny_reason or "",
            },
        )
        log.warning(
            "adapter: queued pending exit after failed live SELL  trade_id=%s  reason=%s",
            trade_id,
            result.failure_details or result.deny_reason or "-",
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "adapter: failed to queue retry after failed live SELL  trade_id=%s: %s",
            trade_id, exc,
        )


def _load_live_state_for_risk() -> dict:
    """Load current positions dict for risk cap checks.

    Returns the full {trade_id: position} dict (open + closed).
    Returns {} on failure — cap checks handle None/empty conservatively.
    """
    try:
        from trading.execution import state as _state
        return _state.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: failed to load live state for risk check: %s", exc)
        return {}


# ── Paper no-op ───────────────────────────────────────────────────────────────

def _paper_noop(intent: ExecutionIntent) -> ExecutionResult:
    """Return a no-op result for paper mode.

    Paper mode never touches the execution adapter in normal operation.
    This function exists so the adapter can be safely imported and tested.
    """
    return ExecutionResult(
        intent=intent,
        status=OrderStatus.PLANNED,
        deny_reason=None,
    )


# ── Safe wrappers ─────────────────────────────────────────────────────────────
# All side-effect calls must never block or crash the execution path.

def _safe_journal(result: ExecutionResult, trade_id: str = "") -> None:
    try:
        append_order_journal(result, trade_id=trade_id)
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: order journal write failed: %s", exc)


def _safe_risk_event(intent: ExecutionIntent, decision) -> None:
    try:
        append_risk_event(intent, decision)
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: risk event journal write failed: %s", exc)


def _safe_state_update(
    intent: ExecutionIntent,
    result: ExecutionResult,
    trade_id: str,
) -> None:
    """Update the persistent state store after a confirmed execution result.

    Called after dry_run_sent (dry-run) or FILLED (micro_live) results only.
    Never called for DENIED, FAILED, or PLANNED results.
    """
    try:
        from trading.execution import state as _state  # local import to avoid cycles

        is_live_fill = result.status == OrderStatus.FILLED

        if intent.action == OrderAction.BUY:
            _state.open_position(
                trade_id     = trade_id,
                token        = intent.token,
                policy_track = intent.policy_track,
                size_usd     = result.realized_size_usd or intent.intended_size_usd,
                entry_price  = result.realized_price or 0.0,
                entry_mcap   = intent.intended_entry_mcap,
                pressure     = intent.pressure,
                mode         = intent.mode,
                token_amount = result.filled_token_amount,
                buy_tx_id    = result.tx_id,
            )
        else:
            # For SELL, close the matching open position if it exists.
            close_reason = "live_sell" if is_live_fill else "dry_run_sell"
            _state.close_position(
                trade_id           = trade_id,
                status             = "closed",
                realized_price     = result.realized_price,
                realized_size_usd  = result.realized_size_usd,
                slippage_bps_proxy = result.slippage_bps_proxy,
                tx_id              = result.tx_id,
                reason             = close_reason,
            )
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: state update failed: %s", exc)


def _safe_execution_log(result: ExecutionResult, trade_id: str) -> None:
    try:
        market_regime = ""
        if result.intent.action == OrderAction.BUY:
            try:
                from trading.execution.market_regime import get_current_market_regime
                market_regime = get_current_market_regime()
            except Exception as exc:  # noqa: BLE001
                log.error("adapter: market regime lookup failed: %s", exc)
        append_execution_log(result, trade_id=trade_id, market_regime=market_regime)
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: execution log write failed: %s", exc)


def _safe_transition(**kwargs) -> None:
    try:
        append_state_transition(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("adapter: state transition journal write failed: %s", exc)


def _new_trade_id() -> str:
    """Generate a fallback trade_id when the caller does not provide one."""
    return f"auto_{uuid.uuid4().hex[:12]}"
