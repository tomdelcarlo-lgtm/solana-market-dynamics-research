# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/pumpfun_cli_shim.py
─────────────────────────────
Bounded migration-prep shim for a pumpfun-cli execution pilot.

This module intentionally does not import pumpfun-cli, load wallets, sign, or
send transactions.  Its first job is command construction only: turn our
existing ExecutionIntent + ExecutionConfig contract into the exact dry-run CLI
argv we would hand to pumpfun-cli for a candidate BUY.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from trading.execution.config import ExecutionConfig
from trading.execution.types import ExecutionIntent, OrderAction


@dataclass(frozen=True)
class PumpfunCliDryRunCommand:
    """A side-effect-free pumpfun-cli dry-run command plan."""

    argv: list[str]
    action: str
    mint: str
    sol_amount: str
    slippage_percent: str
    priority_fee_micro_lamports: Optional[int]
    metadata: dict[str, Any]


def build_buy_dry_run_command(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    *,
    sol_price_usd: Optional[float] = None,
    sol_price_resolver: Optional[Callable[[], float]] = None,
    executable: str = "pumpfun",
    include_keyfile: bool = True,
    priority_fee_micro_lamports: Optional[int] = None,
) -> PumpfunCliDryRunCommand:
    return build_buy_command(
        intent,
        config,
        sol_price_usd=sol_price_usd,
        sol_price_resolver=sol_price_resolver,
        executable=executable,
        include_keyfile=include_keyfile,
        keyfile_path=None,
        priority_fee_micro_lamports=priority_fee_micro_lamports,
        dry_run=True,
    )


def build_buy_command(
    intent: ExecutionIntent,
    config: ExecutionConfig,
    *,
    sol_price_usd: Optional[float] = None,
    sol_price_resolver: Optional[Callable[[], float]] = None,
    executable: str = "pumpfun",
    include_keyfile: bool = True,
    keyfile_path: Optional[str] = None,
    priority_fee_micro_lamports: Optional[int] = None,
    dry_run: bool = False,
) -> PumpfunCliDryRunCommand:
    """Build a pumpfun-cli JSON BUY command from our live intent."""
    if intent.action != OrderAction.BUY:
        raise ValueError("pumpfun-cli dry-run shim currently supports BUY only")
    if config.live is None:
        raise ValueError("live config required for pumpfun-cli dry-run command")

    rpc_url = (config.live.rpc_url or "").strip()
    if not rpc_url:
        raise ValueError("LIVE_RPC_URL required for pumpfun-cli dry-run command")

    wallet_path = (keyfile_path or config.live.wallet_path or "").strip()
    if include_keyfile and not wallet_path:
        raise ValueError("LIVE_WALLET_PATH required for pumpfun-cli dry-run command")

    price = _resolve_sol_price_usd(intent, sol_price_usd, sol_price_resolver)
    sol_amount = _usd_to_sol(intent.intended_size_usd, price)
    slippage_percent = _slippage_percent(config)
    if priority_fee_micro_lamports is not None and priority_fee_micro_lamports < 0:
        raise ValueError("priority_fee_micro_lamports must be >= 0")

    argv = [
        executable,
        "--json",
        "--rpc",
        rpc_url,
    ]
    if include_keyfile:
        argv.extend(["--keyfile", wallet_path])
    if priority_fee_micro_lamports is not None:
        argv.extend(["--priority-fee", str(priority_fee_micro_lamports)])
    argv.extend([
        "buy",
        intent.token,
        sol_amount,
        "--slippage",
        slippage_percent,
    ])
    if dry_run:
        argv.append("--dry-run")

    return PumpfunCliDryRunCommand(
        argv=argv,
        action="BUY",
        mint=intent.token,
        sol_amount=sol_amount,
        slippage_percent=slippage_percent,
        priority_fee_micro_lamports=priority_fee_micro_lamports,
        metadata={
            "path": "pumpfun_cli_dry_run_buy" if dry_run else "pumpfun_cli_live_buy",
            "policy_track": intent.policy_track,
            "intended_size_usd": intent.intended_size_usd,
            "sol_price_usd": float(price),
            "include_keyfile": include_keyfile,
            "sends_transaction": not dry_run,
            "dry_run": dry_run,
            "live_priority_fee_sol_not_mapped": config.live.priority_fee_sol,
            "priority_fee_unit_note": "pumpfun-cli expects micro-lamports; live config stores SOL, so this shim only includes --priority-fee when explicitly supplied",
        },
    )


def _resolve_sol_price_usd(
    intent: ExecutionIntent,
    explicit: Optional[float],
    resolver: Optional[Callable[[], float]],
) -> Decimal:
    if explicit is not None:
        return _positive_decimal(explicit, "sol_price_usd")

    metadata = intent.quote_metadata or {}
    for key in ("sol_price_usd", "quote_sol_price_usd", "sol_usd"):
        if key in metadata and metadata[key] is not None:
            return _positive_decimal(metadata[key], f"quote_metadata.{key}")

    if resolver is not None:
        return _positive_decimal(resolver(), "sol_price_resolver()")

    raise ValueError("sol_price_usd required for pumpfun-cli BUY size conversion")


def _usd_to_sol(size_usd: float, sol_price_usd: Decimal) -> str:
    usd = _positive_decimal(size_usd, "intended_size_usd")
    return _decimal_to_cli_string(usd / sol_price_usd)


def _slippage_percent(config: ExecutionConfig) -> str:
    if config.live is None:
        raise ValueError("live config required while computing slippage")
    bps = max(1, int(config.live.max_slippage_bps or 0))
    return _decimal_to_cli_string(Decimal(bps) / Decimal(100))


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return parsed


def _decimal_to_cli_string(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000000001"))
    text = format(normalized.normalize(), "f")
    return text.rstrip("0").rstrip(".") or "0"
