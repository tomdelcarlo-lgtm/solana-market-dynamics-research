# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
risk/engine.py
───────────────
Risk evaluation entry points.

Public API:
    evaluate_entry(intent, live_state, config) → RiskDecision
    evaluate_exit(intent, live_state, config)  → RiskDecision

Phase 2: sequential checks for dry_run_live mode.
Phase 4: full micro_live check suite — all gates required before any real order.

Phase 1 rule preserved:
    Paper mode bypasses this engine entirely.

Design rules:
- Checks run in order; first failure short-circuits and returns a deny.
- Deny reasons use the DENY_REASONS constants so log grep is reliable.
- Never return allow without running all applicable checks.

Phase 4 micro_live gates (entry):
    live_config_present, armed, safe_mode, sells_only_mode, track_allowed,
    low_lane_sunday, size_cap, max_open_positions, notional_cap, daily_loss,
    wallet_balance, quote_freshness, slippage.

Phase 4.1 implementations (this pass):
    wallet_balance — real RPC check via LIVE_RPC_URL + LIVE_WALLET_PATH.
                     Requires Jupiter or CoinGecko reachable for SOL price.
                     Fail-closed on any error (missing config, RPC failure,
                     price fetch failure).

Remaining stubs (fail-closed):
    send path — _send_live_order in adapter.py; requires solders/solana-py
                for tx construction and ed25519 signing. No-op until Phase 4.2.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from trading.execution.config import ExecutionConfig, ExecutionMode
from trading.execution.types import ExecutionIntent, OrderAction, RiskDecision
from trading.risk.policy import DENY_REASONS, is_track_allowed

log = logging.getLogger(__name__)

# Short-lived SOL/USD cache for wallet-balance and amount calculations.
# This avoids hammering public price endpoints on the hot path and lets us
# fail closed only after both providers fail and the cache is too old.
_SOL_PRICE_CACHE_TTL_S = 300.0
_SOL_PRICE_CACHE_STALE_OK_S = 900.0
_sol_price_cache_usd: Optional[float] = None
_sol_price_cache_ts: float = 0.0

# Short-lived getBalance cache. The wallet balance changes only after a fill;
# re-fetching on every 2-second evaluation loop tick wastes ~3s per call when
# Helius is slow. 30s TTL is safe: the bot holds MAX_OPEN_POSITIONS=1 and
# each BUY is ~$15, so a stale reading for ≤30s cannot cause double-spend.
_BALANCE_CACHE_TTL_S = 30.0
_balance_cache_lamports: Optional[int] = None
_balance_cache_ts: float = 0.0
_balance_cache_key: str = ""  # rpc_url+pubkey — invalidate on config change

# Reserve a small amount of SOL so a freshly-opened micro_live position still
# has enough native balance to pay rent / fees and exit cleanly.
#
# Why this exists:
# - A BUY can pass the old "size + 10%" balance check but still strand the
#   wallet too close to zero for the subsequent SELL simulation/broadcast.
# - We observed real orphaned positions where the exit failed with
#   `InsufficientFundsForRent` even though the BUY was admitted.
#
# This reserve is intentionally conservative and small relative to the live
# wallet, but large enough to avoid opening positions that are not practically
# closable.
_ENTRY_EXIT_SOL_RESERVE = 0.006

# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_entry(
    intent: ExecutionIntent,
    live_state: Optional[Dict[str, Any]],
    config: ExecutionConfig,
) -> RiskDecision:
    """Evaluate whether a BUY intent should be allowed to proceed.

    Phase 2: real sequential checks for dry_run_live.
    Phase 4: full micro_live check suite with all required gates.

    `live_state` is the full positions dict (trade_id → position) used for
    cap checks. Pass None in dry_run mode (not required). In micro_live the
    adapter must load and pass live_state before calling this function.
    """
    if config.mode == ExecutionMode.PAPER:
        return RiskDecision.allow_order("paper mode — risk engine not applied")

    if config.mode == ExecutionMode.MICRO_LIVE:
        return _run_micro_live_entry_checks(intent, config, live_state)

    # ── dry_run_live sequential checks ────────────────────────────────────────
    return _run_dry_run_entry_checks(intent, config)


def evaluate_exit(
    intent: ExecutionIntent,
    live_state: Optional[Dict[str, Any]],
    config: ExecutionConfig,
) -> RiskDecision:
    """Evaluate whether a SELL intent should be allowed to proceed.

    Phase 2: real sequential checks for dry_run_live.
    Phase 4: full micro_live exit check suite.
    Exit checks are lighter than entry checks (sells reduce risk) but still
    require arm, safe_mode, and quote gates for micro_live.
    """
    if config.mode == ExecutionMode.PAPER:
        return RiskDecision.allow_order("paper mode — risk engine not applied")

    if config.mode == ExecutionMode.MICRO_LIVE:
        return _run_micro_live_exit_checks(intent, config, live_state)

    return _run_dry_run_exit_checks(intent, config)


# ── Phase 4 micro_live check suites ──────────────────────────────────────────

def _run_micro_live_entry_checks(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    live_state: Optional[Dict[str, Any]],
) -> RiskDecision:
    """Run all micro_live entry gates in order. First failure short-circuits.

    Gate order (intentional):
      1. live_config_present  — must have LIVE_* config
      2. armed                — manual arm required
      3. safe_mode            — system must not be in safe mode
      4. sells_only_mode      — BUY denied if sells-only flag is set
      5. track_allowed        — track must be in allowlist
      6. low_lane_sunday      — low-lane BUYs are blocked on Sunday UTC unless explicitly allowed
      7. min_pressure         — optional per-track pressure floor
      8. entry_mcap_band      — optional per-track entry mcap band
      9. size_cap             — order size must not exceed per-trade cap
     10. max_open_positions   — open position count must be within cap
     11. notional_cap         — projected total notional must be within cap
     12. daily_loss           — today's realized loss must be within cap
     13. quote_freshness      — quote must be present and not stale
     14. wallet_balance       — fail-closed on missing funds / price / RPC
     15. slippage             — estimated slippage must be within threshold
    """
    # Ordered (check_fn, extra_args)
    check_calls = [
        (_check_live_config_present, ()),
        (_check_armed,               ()),
        (_check_safe_mode,           ()),
        (_check_sells_only_mode,     ()),
        (_check_track_allowed,       ()),
        (_check_low_lane_sunday,     ()),
        (_check_min_pressure,        ()),
        (_check_entry_mcap_band,     ()),
        (_check_size_cap,            ()),
        (_check_max_open_positions,  (live_state,)),
        (_check_notional_cap,        (live_state,)),
        (_check_daily_loss,          (live_state,)),
        (_check_quote_freshness,     ()),
        (_check_wallet_balance,      ()),
        (_check_slippage,            ()),
    ]

    for check_fn, extra in check_calls:
        decision = check_fn(intent, config, *extra)
        if decision is not None:
            log.info(
                "micro_live entry DENIED  token=%s track=%s check=%s reason=%s",
                intent.token[:8], intent.policy_track,
                decision.check_name, decision.reason,
            )
            return decision

    log.info(
        "micro_live entry ALLOWED token=%s track=%s size=%.2f",
        intent.token[:8], intent.policy_track, intent.intended_size_usd,
    )
    return RiskDecision.allow_order("all micro_live entry checks passed")


def _run_micro_live_exit_checks(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    live_state: Optional[Dict[str, Any]],
) -> RiskDecision:
    """Run all micro_live exit gates in order. Sells are allowed more freely.

    Gate order:
      1. live_config_present  — must have LIVE_* config
      2. armed                — manual arm required
      3. safe_mode            — system must not be in safe mode
      4. track_allowed        — track must be in allowlist
      5. quote_freshness      — quote must be present and not stale
      6. slippage             — estimated slippage must be within threshold

    Size cap is NOT applied on exits — the position may have changed in value.
    Notional/loss caps are NOT applied on exits — sells reduce exposure.
    Wallet-balance funding checks are also skipped on exits — a SELL should not
    reuse the BUY-side "need order notional + buffer" rule, which can strand an
    already-open position if the wallet balance falls after entry.
    """
    check_calls = [
        (_check_live_config_present, ()),
        (_check_armed,               ()),
        (_check_safe_mode,           ()),
        (_check_track_allowed,       ()),
        (_check_quote_freshness,     ()),
        (_check_slippage,            ()),
    ]

    for check_fn, extra in check_calls:
        decision = check_fn(intent, config, *extra)
        if decision is not None:
            log.info(
                "micro_live exit DENIED  token=%s track=%s check=%s reason=%s",
                intent.token[:8], intent.policy_track,
                decision.check_name, decision.reason,
            )
            return decision

    log.info(
        "micro_live exit ALLOWED token=%s track=%s",
        intent.token[:8], intent.policy_track,
    )
    return RiskDecision.allow_order("all micro_live exit checks passed")


# ── Phase 2 check suites ──────────────────────────────────────────────────────

def _run_dry_run_entry_checks(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> RiskDecision:
    """Run all Phase 2 entry checks in order. First failure short-circuits."""

    checks = [
        _check_live_config_present,
        _check_sells_only_mode,
        _check_track_allowed,
        _check_low_lane_sunday,
        _check_min_pressure,
        _check_entry_mcap_band,
        _check_size_cap,
    ]

    for check_fn in checks:
        decision = check_fn(intent, config)
        if decision is not None:
            log.info(
                "risk entry DENIED  token=%s track=%s check=%s reason=%s",
                intent.token[:8], intent.policy_track,
                decision.check_name, decision.reason,
            )
            return decision

    log.debug(
        "risk entry ALLOWED token=%s track=%s size=%.2f",
        intent.token[:8], intent.policy_track, intent.intended_size_usd,
    )
    return RiskDecision.allow_order("all dry_run_live entry checks passed")


def _run_dry_run_exit_checks(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> RiskDecision:
    """Run all Phase 2 exit checks in order. Sells are allowed more freely."""

    checks = [
        _check_live_config_present,
        _check_track_allowed,
    ]

    for check_fn in checks:
        decision = check_fn(intent, config)
        if decision is not None:
            log.info(
                "risk exit DENIED  token=%s track=%s check=%s reason=%s",
                intent.token[:8], intent.policy_track,
                decision.check_name, decision.reason,
            )
            return decision

    log.debug(
        "risk exit ALLOWED token=%s track=%s",
        intent.token[:8], intent.policy_track,
    )
    return RiskDecision.allow_order("all dry_run_live exit checks passed")


# ── Individual check helpers ──────────────────────────────────────────────────
# Convention: each returns RiskDecision (deny) on failure, None on pass.
# Signature: check_fn(intent, config, *extra) → Optional[RiskDecision]

def _check_live_config_present(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    if config.live is None:
        return RiskDecision.deny_order(
            reason="live config missing — set LIVE_* env vars and re-run preflight",
            check_name="no_live_config",
        )
    return None


def _check_armed(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if live trading is not manually armed."""
    from trading.ops.live_control import is_armed  # local import — avoids circular dep
    if not is_armed():
        return RiskDecision.deny_order(
            reason=DENY_REASONS.NOT_ARMED,
            check_name="not_armed",
        )
    return None


def _check_safe_mode(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if the system is in safe mode."""
    from trading.ops.live_control import is_safe_mode  # local import
    if is_safe_mode():
        return RiskDecision.deny_order(
            reason=DENY_REASONS.SAFE_MODE_ACTIVE,
            check_name="safe_mode",
        )
    return None


def _check_sells_only_mode(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny BUY orders when LIVE_ALLOW_SELLS_ONLY is true."""
    if config.live is None:
        return None
    if config.live.allow_sells_only and intent.action == OrderAction.BUY:
        return RiskDecision.deny_order(
            reason=DENY_REASONS.SELLS_ONLY_MODE,
            check_name="sells_only_mode",
        )
    return None


def _check_track_allowed(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if the policy track is not in the configured allowlist."""
    if config.live is None:
        return None
    if not is_track_allowed(intent.policy_track, config.live.allowed_policy_tracks):
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.TRACK_NOT_ALLOWED}: {intent.policy_track!r} "
                f"not in {config.live.allowed_policy_tracks}"
            ),
            check_name="track_allowlist",
        )
    return None


def _is_low_lane_track(track_name: str) -> bool:
    """Return True for the low-lane policy-track naming convention."""
    return "_low_" in (track_name or "").strip().lower()


def _check_low_lane_sunday(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny low-lane BUY orders on Sunday UTC unless explicitly allowed."""
    if config.live is None:
        return None
    if intent.action != OrderAction.BUY:
        return None
    if not _is_low_lane_track(intent.policy_track):
        return None
    if config.live.allow_low_lane_sunday:
        return None
    if datetime.now(timezone.utc).weekday() != 6:  # Monday=0 … Sunday=6
        return None
    return RiskDecision.deny_order(
        reason=f"{DENY_REASONS.LOW_LANE_SUNDAY}: track={intent.policy_track!r}",
        check_name="low_lane_sunday",
    )


def _check_min_pressure(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if a per-track minimum pressure gate is configured and unmet."""
    if config.live is None:
        return None
    minimum = config.live.min_pressure_for_track(intent.policy_track)
    if minimum is None:
        return None
    if intent.pressure < minimum:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.PRESSURE_BELOW_MIN}: "
                f"pressure={intent.pressure:.6f} < min={minimum:.6f}"
            ),
            check_name="min_pressure",
        )
    return None


def _check_entry_mcap_band(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if a per-track entry mcap band is configured and unmet."""
    if config.live is None:
        return None
    lower, upper = config.live.entry_mcap_bounds_for_track(intent.policy_track)
    if lower is None and upper is None:
        return None

    value = intent.intended_entry_mcap
    too_low = lower is not None and value < lower
    too_high = upper is not None and value >= upper
    if too_low or too_high:
        lower_label = f"{lower:.2f}" if lower is not None else "-inf"
        upper_label = f"{upper:.2f}" if upper is not None else "+inf"
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.ENTRY_MCAP_OUT_OF_RANGE}: "
                f"mcap={value:.2f} outside [{lower_label}, {upper_label})"
            ),
            check_name="entry_mcap_band",
        )
    return None


def _check_size_cap(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if the intended order size exceeds the per-trade cap."""
    if config.live is None:
        return None
    cap = config.live.position_size_for_track(intent.policy_track)
    if cap > 0 and intent.intended_size_usd > cap:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.SIZE_EXCEEDS_CAP}: "
                f"${intent.intended_size_usd:.2f} > cap ${cap:.2f}"
            ),
            check_name="size_cap",
        )
    return None


def _check_max_open_positions(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    live_state: Optional[Dict[str, Any]],
) -> Optional[RiskDecision]:
    """Deny if current open position count is at or above the cap."""
    if config.live is None:
        return None
    cap = config.live.max_open_positions
    if cap <= 0:
        return None  # unconfigured — not enforced

    if live_state is not None:
        open_count = sum(
            1 for p in live_state.values() if p.get("status") == "open"
        )
    else:
        from trading.execution.state import count_open_positions
        open_count = count_open_positions()

    if open_count >= cap:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.MAX_POSITIONS_REACHED}: "
                f"open={open_count} >= cap={cap}"
            ),
            check_name="max_open_positions",
        )
    return None


def _check_notional_cap(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    live_state: Optional[Dict[str, Any]],
) -> Optional[RiskDecision]:
    """Deny if projected total notional (current + intended) exceeds the cap."""
    if config.live is None:
        return None
    cap = config.live.max_notional_total_usd
    if cap <= 0:
        return None  # unconfigured — not enforced

    if live_state is not None:
        current_total = sum(
            p.get("size_usd", 0.0)
            for p in live_state.values()
            if p.get("status") == "open"
        )
    else:
        from trading.execution.state import get_open_positions
        current_total = sum(
            p.get("size_usd", 0.0) for p in get_open_positions()
        )

    projected = current_total + intent.intended_size_usd
    if projected > cap:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.NOTIONAL_CAP_EXCEEDED}: "
                f"projected=${projected:.2f} > cap=${cap:.2f} "
                f"(current=${current_total:.2f} + intended=${intent.intended_size_usd:.2f})"
            ),
            check_name="notional_cap",
        )
    return None


def _check_daily_loss(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    live_state: Optional[Dict[str, Any]],
) -> Optional[RiskDecision]:
    """Deny if today's realized loss from micro_live positions exceeds the cap.

    Computes daily realized loss as: sum of max(0, size_usd - realized_size_usd)
    for micro_live positions closed today with a calculable realized_size_usd.
    Positions where realized_size_usd is None are skipped (conservatively zero).

    Since no micro_live positions exist at Phase 4 start, this check passes
    initially. It becomes meaningful once real fills are recorded in state.
    """
    if config.live is None:
        return None
    cap = config.live.max_daily_loss_usd
    if cap <= 0:
        return None  # unconfigured — not enforced

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    positions_to_check: list = []
    if live_state is not None:
        positions_to_check = list(live_state.values())
    else:
        from trading.execution.state import get_all_positions
        positions_to_check = list(get_all_positions().values())

    daily_loss = 0.0
    for pos in positions_to_check:
        # Only micro_live positions count toward the live daily loss
        if pos.get("mode") != "micro_live":
            continue
        if pos.get("status") not in ("closed", "flattened"):
            continue
        closed_at = pos.get("closed_at") or ""
        if not closed_at.startswith(today_str):
            continue
        size_in = pos.get("size_usd", 0.0) or 0.0
        size_out = pos.get("realized_size_usd")
        if size_out is None:
            continue  # cannot compute loss — skip conservatively
        loss = max(0.0, size_in - size_out)
        daily_loss += loss

    if daily_loss >= cap:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.DAILY_LOSS_BREACHED}: "
                f"today_loss=${daily_loss:.2f} >= cap=${cap:.2f}"
            ),
            check_name="daily_loss",
        )
    return None


def _check_wallet_balance(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if wallet SOL balance is insufficient for the intended BUY order.

    Phase 4.1 implementation — real RPC balance check.

    BUY flow:
      1. Verify LIVE_RPC_URL and LIVE_WALLET_PATH are configured.
      2. Load wallet public key from the JSON keypair file.
      3. Call getBalance(pubkey) via Solana JSON-RPC.
      4. Fetch SOL/USD price from Jupiter price API (CoinGecko fallback).
      5. Deny if balance_usd < intended_size_usd * 1.10 (10% fee buffer).
      6. Deny on any error (fail-closed throughout).

    SELL flow:
      - intentionally skipped here
      - exits reduce exposure and should not reuse the BUY-side notional-funding
        rule, which can otherwise block a valid SELL after a BUY has already
        opened a position

    The 10% buffer accounts for:
      - Solana transaction fees (~0.000005 SOL per tx, negligible)
      - Price slippage between balance check and execution
      - Rounding in lamport conversion

    Note on SOL price: fetched live from Jupiter then CoinGecko as fallback.
    In a production system this should be cached with a short TTL to avoid
    an extra network round-trip on every order attempt.
    """
    if intent.action == OrderAction.SELL:
        log.debug(
            "wallet_balance: skipped for SELL token=%s track=%s",
            intent.token[:8], intent.policy_track,
        )
        return None

    if config.live is None:
        return RiskDecision.deny_order(
            reason=f"{DENY_REASONS.INSUFFICIENT_BALANCE}: live config missing",
            check_name="wallet_balance_no_config",
        )

    rpc_url     = config.live.rpc_url
    wallet_path = config.live.wallet_path

    if not rpc_url:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: LIVE_RPC_URL not set — "
                "required for wallet balance check"
            ),
            check_name="wallet_balance_no_rpc",
        )

    if not wallet_path:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: LIVE_WALLET_PATH not set — "
                "required for wallet balance check"
            ),
            check_name="wallet_balance_no_wallet_path",
        )

    timeout_s = max(1.0, (config.live.quote_timeout_ms or 3000) / 1000.0)

    # Step 2: load pubkey from keypair file
    try:
        pubkey_b58 = _load_wallet_pubkey_b58(wallet_path)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "wallet_balance: failed to load pubkey from %r: %s",
            wallet_path, exc,
        )
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: keypair load failed "
                f"({wallet_path!r}): {exc}"
            ),
            check_name="wallet_balance_load_error",
        )

    # Step 3: fetch SOL balance via RPC (cached for _BALANCE_CACHE_TTL_S seconds).
    global _balance_cache_lamports, _balance_cache_ts, _balance_cache_key
    _cache_key = rpc_url + pubkey_b58
    _now_bal = time.monotonic()
    if (
        _balance_cache_lamports is not None
        and _balance_cache_key == _cache_key
        and (_now_bal - _balance_cache_ts) < _BALANCE_CACHE_TTL_S
    ):
        balance_lamports = _balance_cache_lamports
    else:
        try:
            balance_lamports = _rpc_get_balance(rpc_url, pubkey_b58, timeout_s=timeout_s)
            _balance_cache_lamports = balance_lamports
            _balance_cache_ts = _now_bal
            _balance_cache_key = _cache_key
        except Exception as exc:  # noqa: BLE001
            log.error("wallet_balance: RPC getBalance failed: %s", exc)
            return RiskDecision.deny_order(
                reason=(
                    f"{DENY_REASONS.INSUFFICIENT_BALANCE}: RPC getBalance failed: {exc}"
                ),
                check_name="wallet_balance_rpc_error",
            )

    balance_sol = balance_lamports / 1_000_000_000.0

    # Step 4: fetch SOL/USD price
    try:
        sol_price_usd = _fetch_sol_price_usd(timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        log.error("wallet_balance: SOL price fetch failed: %s", exc)
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: SOL price fetch failed: {exc}"
            ),
            check_name="wallet_balance_price_error",
        )

    if sol_price_usd <= 0:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: "
                f"invalid SOL price returned ({sol_price_usd})"
            ),
            check_name="wallet_balance_invalid_price",
        )

    # Step 5: compare balance to required.
    #
    # We reserve:
    #   - 10% over the intended BUY notional for fees / rounding / drift
    #   - an additional fixed SOL cushion so the wallet can still afford the
    #     eventual SELL path (rent + fees) after the BUY has landed.
    balance_usd  = balance_sol * sol_price_usd
    FEE_BUFFER   = 1.10  # 10% over-collateralisation for fees and rounding
    intended_size_sol = intent.intended_size_usd / sol_price_usd
    required_sol = (intended_size_sol * FEE_BUFFER) + _ENTRY_EXIT_SOL_RESERVE
    required_usd = required_sol * sol_price_usd

    if balance_usd < required_usd:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.INSUFFICIENT_BALANCE}: "
                f"balance ${balance_usd:.2f} ({balance_sol:.4f} SOL "
                f"@ ${sol_price_usd:.2f}/SOL) "
                f"< required ${required_usd:.2f} "
                f"(size ${intent.intended_size_usd:.2f} + 10% buffer + "
                f"{_ENTRY_EXIT_SOL_RESERVE:.4f} SOL exit reserve)"
            ),
            check_name="wallet_balance_insufficient",
        )

    log.info(
        "wallet_balance: OK  %.4f SOL ($%.2f)  required=$%.2f (incl %.4f SOL reserve)  pubkey=%.8s…",
        balance_sol, balance_usd, required_usd, _ENTRY_EXIT_SOL_RESERVE, pubkey_b58,
    )
    return None  # pass


# ── Wallet balance helpers ─────────────────────────────────────────────────────

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    """Encode bytes to Solana/Bitcoin base58 (standard alphabet).

    Standard implementation: integer division loop + leading-zero
    preservation. Handles the all-zeros edge case correctly.
    """
    n = int.from_bytes(data, "big")
    result: list = []
    while n > 0:
        n, rem = divmod(n, 58)
        result.append(_B58_ALPHABET[rem])
    # Preserve leading zero bytes as '1' characters
    for byte in data:
        if byte == 0:
            result.append(_B58_ALPHABET[0])
        else:
            break
    return bytes(reversed(result)).decode("ascii")


def _load_wallet_pubkey_b58(wallet_path: str) -> str:
    """Extract and base58-encode the public key from a Solana JSON keypair.

    Solana CLI keypair files (`solana-keygen new`) are JSON arrays of
    exactly 64 uint8 values:
      [0..31]  = 32-byte secret key seed (ed25519 private scalar)
      [32..63] = 32-byte public key

    Raises ValueError if the file format is unexpected or unreadable.
    The caller treats any exception as a fail-closed deny.
    """
    raise RuntimeError("Wallet access is disabled in this public research release")
    import json
    from pathlib import Path

    raw = Path(wallet_path).read_text(encoding="utf-8")
    kp  = json.loads(raw)

    if not isinstance(kp, list):
        raise ValueError(
            f"keypair file is not a JSON array (got {type(kp).__name__}); "
            "expected the 64-byte array format produced by `solana-keygen new`"
        )

    if len(kp) == 64:
        pubkey_bytes = bytes(kp[32:64])
    elif len(kp) == 32:
        raise ValueError(
            "keypair file has 32 bytes — this appears to be a seed-only format; "
            "need the full 64-byte keypair to extract the public key without "
            "ed25519 key derivation (requires solders)"
        )
    else:
        raise ValueError(
            f"keypair file has {len(kp)} bytes; expected 64 "
            "(full keypair from `solana-keygen new`)"
        )

    if len(pubkey_bytes) != 32:
        raise ValueError(
            f"pubkey extraction produced {len(pubkey_bytes)} bytes; expected 32"
        )

    return _b58encode(pubkey_bytes)


def _rpc_get_balance(
    rpc_url: str,
    pubkey_b58: str,
    timeout_s: float = 3.0,
) -> int:
    """Call getBalance on the Solana JSON-RPC endpoint.

    Returns the confirmed lamport balance as an integer.
    Raises ValueError or requests.RequestException on any error.
    """
    from trading.safety import require_network; require_network()
    import requests  # available in environment (requests>=2.32)

    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getBalance",
        "params":  [pubkey_b58, {"commitment": "confirmed"}],
    }
    resp = requests.post(rpc_url, json=payload, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"RPC returned error: {data['error']}")

    try:
        return int(data["result"]["value"])
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"unexpected RPC response structure: {data!r}"
        ) from exc


def _fetch_sol_price_usd(timeout_s: float = 3.0) -> float:
    """Fetch current SOL/USD price with short-lived caching.

    Source order:
    1. Fresh in-process cache (hot path)
    2. Jupiter price API v3
    3. CoinGecko simple price API
    4. Slightly stale cache (degraded fallback)

    Returns price as a positive float.
    Raises ValueError only when both providers fail and no acceptable cache
    is available.
    """
    from trading.safety import require_network; require_network()
    import requests  # available in environment

    global _sol_price_cache_usd, _sol_price_cache_ts

    now = time.time()
    if _sol_price_cache_usd and (now - _sol_price_cache_ts) <= _SOL_PRICE_CACHE_TTL_S:
        return float(_sol_price_cache_usd)

    SOL_MINT = "So11111111111111111111111111111111111111112"

    # ── Primary: Jupiter Price API v3 ────────────────────────────────────────
    try:
        url  = f"https://api.jup.ag/price/v3?ids={SOL_MINT}"
        resp = requests.get(url, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        price_val = data[SOL_MINT]["usdPrice"]
        price = float(price_val)
        if price > 0:
            _sol_price_cache_usd = price
            _sol_price_cache_ts = now
            log.debug("sol_price: Jupiter v3 %.4f USD", price)
            return price
    except Exception as jup_exc:  # noqa: BLE001
        log.warning("sol_price: Jupiter v3 fetch failed: %s — trying CoinGecko", jup_exc)

    # ── Fallback: CoinGecko simple price API ─────────────────────────────────
    try:
        url  = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        resp = requests.get(url, timeout=timeout_s)
        resp.raise_for_status()
        data  = resp.json()
        price = float(data["solana"]["usd"])
        if price > 0:
            _sol_price_cache_usd = price
            _sol_price_cache_ts = now
            log.debug("sol_price: CoinGecko %.4f USD", price)
            return price
    except Exception as cg_exc:  # noqa: BLE001
        log.error("sol_price: CoinGecko fetch also failed: %s", cg_exc)

    if _sol_price_cache_usd and (now - _sol_price_cache_ts) <= _SOL_PRICE_CACHE_STALE_OK_S:
        age = now - _sol_price_cache_ts
        log.warning(
            "sol_price: using cached %.4f USD after provider failures (cache_age=%.1fs)",
            _sol_price_cache_usd,
            age,
        )
        return float(_sol_price_cache_usd)

    raise ValueError(
        "SOL price unavailable: both Jupiter and CoinGecko returned errors or "
        "invalid data, and no recent cache was available — cannot verify wallet balance"
    )


def _check_quote_freshness(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if the intent has no quote metadata, or if the quote is stale.

    Staleness is determined by LIVE_QUOTE_MAX_AGE_MS:
    - 0 (default/unconfigured) → fail-closed: deny any order without a verified
      fresh quote. Operator must set LIVE_QUOTE_MAX_AGE_MS explicitly.
    - > 0 → allow if quote age (now - quote_timestamp_ms) <= max_age_ms.

    Quote timestamp is read from intent.quote_metadata["timestamp_ms"] or
    intent.quote_metadata["fetched_at_ms"].
    """
    if config.live is None:
        return None

    qmeta = intent.quote_metadata
    if qmeta is None:
        return RiskDecision.deny_order(
            reason=f"{DENY_REASONS.QUOTE_STALE}: no quote_metadata on intent",
            check_name="quote_missing",
        )

    quote_ts_ms = qmeta.get("timestamp_ms") or qmeta.get("fetched_at_ms")
    if quote_ts_ms is None:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.QUOTE_STALE}: quote_metadata present but has "
                "no timestamp_ms or fetched_at_ms field"
            ),
            check_name="quote_no_timestamp",
        )

    max_age_ms = config.live.quote_max_age_ms
    if max_age_ms <= 0:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.QUOTE_STALE}: LIVE_QUOTE_MAX_AGE_MS is 0 "
                "(not configured) — fail-closed. Set LIVE_QUOTE_MAX_AGE_MS "
                "explicitly (e.g. 2000) to allow fresh quotes."
            ),
            check_name="quote_max_age_unconfigured",
        )

    now_ms = int(time.time() * 1000)
    age_ms = now_ms - int(quote_ts_ms)
    if age_ms > max_age_ms:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.QUOTE_STALE}: quote age {age_ms}ms "
                f"> max {max_age_ms}ms"
            ),
            check_name="quote_stale",
        )
    return None


def _check_slippage(
    intent: ExecutionIntent,
    config: ExecutionConfig,
) -> Optional[RiskDecision]:
    """Deny if the estimated slippage exceeds the configured threshold.

    Reads slippage_bps or estimated_slippage_bps from intent.quote_metadata.
    If no slippage estimate is available the check is skipped (not a hard deny)
    — quote_freshness will have already denied if the quote is missing entirely.
    If LIVE_MAX_SLIPPAGE_BPS is 0 the check is not enforced.
    """
    if config.live is None:
        return None
    max_bps = config.live.max_slippage_bps
    if max_bps <= 0:
        return None  # unconfigured — not enforced

    qmeta = intent.quote_metadata
    if qmeta is None:
        return None  # quote_freshness check handles missing quote

    slippage_bps = qmeta.get("slippage_bps") or qmeta.get("estimated_slippage_bps")
    if slippage_bps is None:
        return None  # no estimate available — skip (not deniable without data)

    if int(slippage_bps) > max_bps:
        return RiskDecision.deny_order(
            reason=(
                f"{DENY_REASONS.SLIPPAGE_TOO_HIGH}: "
                f"{slippage_bps}bps > cap {max_bps}bps"
            ),
            check_name="slippage_cap",
        )
    return None
