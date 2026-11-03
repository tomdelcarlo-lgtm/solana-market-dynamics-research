# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
from __future__ import annotations

import base64
import concurrent.futures
import json
import random
import struct
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import requests
from solders.compute_budget import (
    set_compute_unit_price,
    set_loaded_accounts_data_size_limit,
)
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
EXPECTED_CURVE_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
BUY_DISCRIMINATOR = struct.pack("<Q", 16927863322537952870)
SELL_DISCRIMINATOR = struct.pack("<Q", 12502976635542562355)

JITO_BUNDLE_ENDPOINT = ""  # Removed funded/routing configuration; historical route disabled.
SELL_SPRAY_RPCS = []  # Removed funded/routing configuration; historical route disabled.
JITO_TIP_ACCOUNTS = []  # Removed funded/routing configuration; historical route disabled.

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
BREAKING_FEE_RECIPIENTS = []  # Recipient configuration excluded; historical send path disabled.
RESERVED_FEE_RECIPIENT_OFFSET = 483


@dataclass(frozen=True)
class PumpCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: Pubkey | None
    is_mayhem_mode: bool
    is_cashback_coin: bool  # byte[82] of bonding-curve account; drives sell account count


@dataclass(frozen=True)
class PreparedBuy:
    wallet_pubkey: str
    signature: str
    recent_blockhash: str
    token_amount_raw: int
    max_amount_lamports: int
    fee_recipient: str
    breaking_fee_recipient: str
    token_program: str
    creator_vault: str
    bonding_curve: str
    bonding_curve_v2: str


# Premiums above this threshold are implausible as real price moves and most
# likely indicate a units/scaling bug in signal_price or current_price rather
# than a genuine catastrophic pump (see 2026-06-10 1000x units bug).
GUARD_DATA_ERROR_THRESHOLD_BPS = 30_000  # 300%


class EntryPremiumAbort(Exception):
    """Raised when the live curve price exceeds the signal price cap before send."""
    def __init__(self, premium_bps: int, current_price: float, signal_price: float) -> None:
        self.premium_bps = premium_bps
        self.current_price = current_price
        self.signal_price = signal_price
        self.is_data_error = premium_bps > GUARD_DATA_ERROR_THRESHOLD_BPS
        if self.is_data_error:
            super().__init__(
                f"entry_premium_guard_data_error: posible unit mismatch, "
                f"premium={premium_bps}bps current={current_price:.10f} "
                f"signal={signal_price:.10f}"
            )
        else:
            super().__init__(
                f"entry_premium_guard aborted: premium={premium_bps}bps "
                f"current={current_price:.10f} signal={signal_price:.10f}"
            )


def send_buy_transaction(
    *,
    rpc_url: str,
    wallet_path: str,
    mint: str,
    intended_size_usd: float,
    sol_price_usd: float,
    slippage_bps: int,
    require_preflight: bool,
    compute_unit_price_micro_lamports: int = 5_000_000,
    loaded_accounts_limit_bytes: int = 16_384_000,
    signal_price: float = 0.0,
    entry_premium_cap_bps: int = 1000,
) -> PreparedBuy:
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    payer = _load_wallet_keypair(wallet_path)
    payer_pubkey = payer.pubkey()
    mint_pk = Pubkey.from_string(mint)

    # Parallel fetch of mint info + bonding curve to reduce latency (fix C1).
    bonding_curve = _find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM)
    mint_info, curve_info = _fetch_mint_and_curve(rpc_url, mint_pk, bonding_curve)
    token_program = _resolve_token_program(mint_info)
    curve_state = _parse_curve_state(_account_data_bytes(curve_info))
    if curve_state.creator is None:
        raise ValueError("bonding curve creator missing; cannot derive creator_vault")

    # Entry premium guard: abort if live price has moved more than cap above signal.
    # current_price must be in SOL/token (lamports -> SOL, raw units -> token with
    # 6 decimals), same units as signal_price computed in pumpfun_bot.py's trade
    # parser: price_sol = (virt_sol / 1e9) / (virt_tok / 1e6).
    if signal_price > 0.0 and curve_state.virtual_token_reserves > 0:
        current_price = (
            (curve_state.virtual_sol_reserves / 1e9)
            / (curve_state.virtual_token_reserves / 1e6)
        )
        premium_bps = round((current_price / signal_price - 1.0) * 10_000)
        if premium_bps > entry_premium_cap_bps:
            raise EntryPremiumAbort(
                premium_bps=premium_bps,
                current_price=current_price,
                signal_price=signal_price,
            )

    associated_bonding_curve = _derive_ata(bonding_curve, mint_pk, token_program)
    associated_token_account = _derive_ata(payer_pubkey, mint_pk, token_program)
    creator_vault = _find_program_address([b"creator-vault", bytes(curve_state.creator)], PUMP_PROGRAM)
    user_volume_accumulator = _find_program_address([b"user_volume_accumulator", bytes(payer_pubkey)], PUMP_PROGRAM)
    global_volume_accumulator = _find_program_address([b"global_volume_accumulator"], PUMP_PROGRAM)
    fee_config = _find_program_address([b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM)
    bonding_curve_v2 = _find_program_address([b"bonding-curve-v2", bytes(mint_pk)], PUMP_PROGRAM)

    fee_recipient = _resolve_fee_recipient(rpc_url, curve_state)
    breaking_fee_recipient = random.choice(BREAKING_FEE_RECIPIENTS)

    amount_lamports = _usd_to_lamports(intended_size_usd, sol_price_usd)

    # AMM constant-product formula for tokens_out given sol_in (fix B1/B2).
    # tokens_out = vt - (vt * vs) / (vs + sol_in)
    vt = curve_state.virtual_token_reserves
    vs = curve_state.virtual_sol_reserves
    if vs <= 0 or vt <= 0:
        raise ValueError("invalid bonding curve reserves")
    token_amount_raw_estimated = vt - (vt * vs) // (vs + amount_lamports)
    if token_amount_raw_estimated <= 0:
        raise ValueError(f"computed token buy amount <= 0 for {amount_lamports} lamports")

    # Apply downward slippage to min_tokens_out → avoids error 6042.
    token_amount_raw = int(
        (Decimal(token_amount_raw_estimated) * (
            Decimal("1") - (Decimal(max(1, slippage_bps)) / Decimal(10_000))
        )).to_integral_value(rounding=ROUND_DOWN)
    )
    if token_amount_raw <= 0:
        raise ValueError("slippage-adjusted min_tokens_out <= 0")

    # Apply upward slippage to max_sol_cost → avoids error 6002.
    max_amount_lamports = int(
        (Decimal(amount_lamports) * (
            Decimal("1") + (Decimal(max(1, slippage_bps)) / Decimal(10_000))
        )).to_integral_value(rounding=ROUND_DOWN)
    )

    instructions: list[Instruction] = []
    if loaded_accounts_limit_bytes > 0:
        # 16MB needed for Token-2022 buys; 512KB triggers a loaded-account data size error
        # on Token-2022-era pump.fun accounts. Benchmark confirms 16MB is correct.
        instructions.append(set_loaded_accounts_data_size_limit(int(loaded_accounts_limit_bytes)))
    if compute_unit_price_micro_lamports > 0:
        instructions.append(set_compute_unit_price(int(compute_unit_price_micro_lamports)))
    instructions.append(
        _create_associated_token_account_idempotent_ix(
            payer=payer_pubkey,
            owner=payer_pubkey,
            mint=mint_pk,
            token_program=token_program,
            ata=associated_token_account,
        )
    )
    instructions.append(
        Instruction(
            PUMP_PROGRAM,
            BUY_DISCRIMINATOR
            + struct.pack("<Q", token_amount_raw)
            + struct.pack("<Q", max_amount_lamports)
            + bytes([1]),   # track_volume: plain bool, 1 byte (fix X1 — NOT OptionBool)
            [
                AccountMeta(PUMP_GLOBAL, False, False),            # 0
                AccountMeta(fee_recipient, False, True),           # 1
                AccountMeta(mint_pk, False, False),                # 2
                AccountMeta(bonding_curve, False, True),           # 3
                AccountMeta(associated_bonding_curve, False, True),# 4
                AccountMeta(associated_token_account, False, True),# 5
                AccountMeta(payer_pubkey, True, True),             # 6
                AccountMeta(SYSTEM_PROGRAM, False, False),         # 7
                AccountMeta(token_program, False, False),          # 8
                AccountMeta(creator_vault, False, True),           # 9
                AccountMeta(PUMP_EVENT_AUTHORITY, False, False),   # 10
                AccountMeta(PUMP_PROGRAM, False, False),           # 11
                AccountMeta(global_volume_accumulator, False, False), # 12
                AccountMeta(user_volume_accumulator, False, True), # 13
                AccountMeta(fee_config, False, False),             # 14
                AccountMeta(PUMP_FEE_PROGRAM, False, False),       # 15
                AccountMeta(bonding_curve_v2, False, False),       # 16
                AccountMeta(breaking_fee_recipient, False, True),  # 17
            ],
        )
    )

    recent_blockhash = _get_latest_blockhash(rpc_url)
    tx = Transaction.new_signed_with_payer(
        instructions,
        payer_pubkey,
        [payer],
        Hash.from_string(recent_blockhash),
    )
    signature = _send_transaction_multi_rpc(
        rpc_url,
        bytes(tx),
        skip_preflight=not require_preflight,
    )
    return PreparedBuy(
        wallet_pubkey=str(payer_pubkey),
        signature=signature,
        recent_blockhash=recent_blockhash,
        token_amount_raw=token_amount_raw,
        max_amount_lamports=max_amount_lamports,
        fee_recipient=str(fee_recipient),
        breaking_fee_recipient=str(breaking_fee_recipient),
        token_program=str(token_program),
        creator_vault=str(creator_vault),
        bonding_curve=str(bonding_curve),
        bonding_curve_v2=str(bonding_curve_v2),
    )


def send_sell_transaction(
    *,
    rpc_url: str,
    wallet_path: str,
    mint: str,
    token_amount_raw: int,
    slippage_bps: int,
    require_preflight: bool,
    compute_unit_price_micro_lamports: int = 5_000_000,
    use_jito: bool = False,
    jito_tip_lamports: int = 50_000,
    jito_endpoint: str = JITO_BUNDLE_ENDPOINT,
) -> str:
    """Build, sign and send a SELL transaction for pump.fun bonding curve (post-Feb-2026 layout).

    Account layout is dynamic based on is_cashback_coin (byte[82] of bonding curve):
      cashback=false → 16 accounts (14 base + bonding_curve_v2 + breaking_fee_recipient)
      cashback=true  → 17 accounts (14 base + user_volume_accumulator + bonding_curve_v2 + breaking_fee_recipient)

    When use_jito=True the transaction is sent as a 1-tx Jito bundle instead of
    via the normal RPC sendTransaction. A tip transfer to a random JITO_TIP_ACCOUNT
    is prepended, compute_unit_price is lowered to 1_000 µL (the tip is the
    competitive lever), and the return value is the transaction signature extracted
    before send so confirm_transaction() continues to work unchanged.

    Returns the transaction signature string.
    """
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    payer = _load_wallet_keypair(wallet_path)
    payer_pubkey = payer.pubkey()
    mint_pk = Pubkey.from_string(mint)

    # Parallel fetch of mint info + bonding curve to reduce latency (fix C1 equivalent).
    bonding_curve = _find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM)
    mint_info, curve_info = _fetch_mint_and_curve(rpc_url, mint_pk, bonding_curve)
    token_program = _resolve_token_program(mint_info)
    curve_state = _parse_curve_state(_account_data_bytes(curve_info))

    associated_bonding_curve = _derive_ata(bonding_curve, mint_pk, token_program)
    associated_token_account = _derive_ata(payer_pubkey, mint_pk, token_program)

    if curve_state.creator is None:
        raise ValueError("bonding curve creator missing; cannot derive creator_vault")
    creator_vault = _find_program_address([b"creator-vault", bytes(curve_state.creator)], PUMP_PROGRAM)
    fee_config = _find_program_address([b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM)
    bonding_curve_v2 = _find_program_address([b"bonding-curve-v2", bytes(mint_pk)], PUMP_PROGRAM)
    fee_recipient = _resolve_fee_recipient(rpc_url, curve_state)
    breaking_fee_recipient = random.choice(BREAKING_FEE_RECIPIENTS)

    # Constant-product min_sol_output with downward slippage.
    vt = curve_state.virtual_token_reserves
    vs = curve_state.virtual_sol_reserves
    if vs <= 0 or vt <= 0:
        raise ValueError("invalid bonding curve reserves for sell")
    sol_out_estimated = vs - (vs * vt) // (vt + token_amount_raw)
    min_sol_output = int(
        (Decimal(max(0, sol_out_estimated)) * (
            Decimal("1") - (Decimal(max(1, slippage_bps)) / Decimal(10_000))
        )).to_integral_value(rounding=ROUND_DOWN)
    )

    # Base 14 accounts (same for cashback and non-cashback).
    accounts: list[AccountMeta] = [
        AccountMeta(PUMP_GLOBAL, False, False),            # 0
        AccountMeta(fee_recipient, False, True),           # 1
        AccountMeta(mint_pk, False, False),                # 2
        AccountMeta(bonding_curve, False, True),           # 3
        AccountMeta(associated_bonding_curve, False, True),# 4
        AccountMeta(associated_token_account, False, True),# 5
        AccountMeta(payer_pubkey, True, True),             # 6
        AccountMeta(SYSTEM_PROGRAM, False, False),         # 7
        AccountMeta(creator_vault, False, True),           # 8
        AccountMeta(token_program, False, False),          # 9
        AccountMeta(PUMP_EVENT_AUTHORITY, False, False),   # 10
        AccountMeta(PUMP_PROGRAM, False, False),           # 11
        AccountMeta(fee_config, False, False),             # 12
        AccountMeta(PUMP_FEE_PROGRAM, False, False),       # 13
    ]

    # cashback=true: insert user_volume_accumulator BEFORE bonding_curve_v2.
    if curve_state.is_cashback_coin:
        user_vol_acc = _find_program_address(
            [b"user_volume_accumulator", bytes(payer_pubkey)], PUMP_PROGRAM
        )
        accounts.append(AccountMeta(user_vol_acc, False, True))  # 14 (cashback only)

    accounts.append(AccountMeta(bonding_curve_v2, False, False))     # 14 or 15
    accounts.append(AccountMeta(breaking_fee_recipient, False, True)) # 15 or 16

    sell_ix = Instruction(
        PUMP_PROGRAM,
        SELL_DISCRIMINATOR
        + struct.pack("<Q", token_amount_raw)
        + struct.pack("<Q", min_sol_output)
        + bytes([1, 1]),  # track_volume: OptionBool Some(true) — 2 bytes for SELL (correct)
        accounts,
    )

    instructions: list[Instruction] = []
    if use_jito:
        # Tip must appear in the tx; prepend before sell_ix.
        # Use 1_000 µL compute price — Jito tip is the competitive lever.
        instructions.append(set_compute_unit_price(1_000))
        tip_account = random.choice(JITO_TIP_ACCOUNTS)
        # SystemProgram Transfer: discriminant=2 (u32 LE) + amount (u64 LE)
        instructions.append(Instruction(
            SYSTEM_PROGRAM,
            struct.pack("<IQ", 2, int(jito_tip_lamports)),
            [
                AccountMeta(payer_pubkey, True, True),
                AccountMeta(tip_account, False, True),
            ],
        ))
    elif compute_unit_price_micro_lamports > 0:
        instructions.append(set_compute_unit_price(int(compute_unit_price_micro_lamports)))
    instructions.append(sell_ix)

    recent_blockhash = _get_latest_blockhash(rpc_url)
    tx = Transaction.new_signed_with_payer(
        instructions,
        payer_pubkey,
        [payer],
        Hash.from_string(recent_blockhash),
    )
    if use_jito:
        return _send_jito_bundle(tx, jito_endpoint)
    return _send_transaction_multi_rpc(rpc_url, bytes(tx), skip_preflight=not require_preflight)


def confirm_transaction(
    rpc_url: str,
    signature: str,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> bool:
    """Poll for transaction confirmation.

    Returns True when confirmed/finalized.
    Returns False on timeout — state is UNCERTAIN; do NOT assume failed and do NOT
    retry without first reconciling on-chain state.
    Raises ValueError if the transaction is confirmed to have failed on-chain.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": False}],
        }
        try:
            data = _rpc_post(rpc_url, payload)
            statuses = ((data.get("result") or {}).get("value") or [None])
            status = statuses[0] if statuses else None
            if status is not None:
                err = status.get("err")
                if err is not None:
                    raise ValueError(f"tx failed on-chain: {err}")
                conf = status.get("confirmationStatus")
                if conf in ("confirmed", "finalized"):
                    return True
        except ValueError:
            raise
        except Exception:
            pass
        time.sleep(poll_interval_s)
    return False  # UNCERTAIN — not confirmed within timeout


# ── Private helpers ────────────────────────────────────────────────────────────

def _fetch_mint_and_curve(
    rpc_url: str, mint_pk: Pubkey, bonding_curve: Pubkey
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch mint info and bonding curve account data in parallel (fix C1)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_mint = executor.submit(_get_account_info, rpc_url, str(mint_pk))
        f_curve = executor.submit(_get_account_info, rpc_url, str(bonding_curve))
        return f_mint.result(), f_curve.result()


def _load_wallet_keypair(wallet_path: str) -> Keypair:
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    raw = json.loads(Path(wallet_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"wallet file {wallet_path} is not a JSON int array")
    return Keypair.from_bytes(bytes(int(x) for x in raw))


def _usd_to_lamports(intended_size_usd: float, sol_price_usd: float) -> int:
    if intended_size_usd <= 0:
        raise ValueError("intended_size_usd must be > 0")
    if sol_price_usd <= 0:
        raise ValueError("sol_price_usd must be > 0")
    amount_sol = Decimal(str(intended_size_usd)) / Decimal(str(sol_price_usd))
    lamports = int((amount_sol * Decimal(LAMPORTS_PER_SOL)).to_integral_value(rounding=ROUND_DOWN))
    if lamports <= 0:
        raise ValueError("computed SOL amount <= 0 lamports")
    return lamports


def _resolve_token_program(mint_info: dict[str, Any]) -> Pubkey:
    owner = str((mint_info.get("owner") or "").strip())
    if owner == str(TOKEN_2022_PROGRAM):
        return TOKEN_2022_PROGRAM
    if owner == str(TOKEN_PROGRAM):
        return TOKEN_PROGRAM
    raise ValueError(f"unsupported mint owner/token program: {owner}")


def _parse_curve_state(data: bytes) -> PumpCurveState:
    """Parse bonding curve account data (post-Feb-2026 layout, 83 bytes body).

    Layout (after 8-byte discriminator):
      bytes 0–39  : 5×uint64 (virtual/real reserves + supply)
      byte  40    : complete (bool)
      bytes 41–72 : creator pubkey (32 bytes)
      byte  73    : is_mayhem_mode
      byte  74    : is_cashback_coin  ← byte[82] of full account
    """
    if len(data) < 8:
        raise ValueError("bonding curve data too short")
    if data[:8] != EXPECTED_CURVE_DISCRIMINATOR:
        raise ValueError("invalid curve state discriminator")
    body = data[8:]
    if len(body) < 41:
        raise ValueError("bonding curve body too short")
    values = struct.unpack_from("<QQQQQ?", body, 0)
    offset = 41
    creator = None
    if len(body) >= offset + 32:
        creator = Pubkey.from_bytes(body[offset : offset + 32])
        offset += 32
    is_mayhem_mode = False
    if len(body) >= offset + 1:
        is_mayhem_mode = bool(body[offset])
        offset += 1  # always increment after reading (was missing before, latent bug)
    is_cashback_coin = False
    if len(body) >= offset + 1:
        is_cashback_coin = bool(body[offset])
        offset += 1
    return PumpCurveState(
        virtual_token_reserves=values[0],
        virtual_sol_reserves=values[1],
        real_token_reserves=values[2],
        real_sol_reserves=values[3],
        token_total_supply=values[4],
        complete=values[5],
        creator=creator,
        is_mayhem_mode=is_mayhem_mode,
        is_cashback_coin=is_cashback_coin,
    )


def _resolve_fee_recipient(rpc_url: str, curve_state: PumpCurveState) -> Pubkey:
    if not curve_state.is_mayhem_mode:
        return PUMP_FEE

    try:
        global_info = _get_account_info(rpc_url, str(PUMP_GLOBAL))
        global_data = _account_data_bytes(global_info)
    except Exception:
        return PUMP_FEE

    if len(global_data) < RESERVED_FEE_RECIPIENT_OFFSET + 32:
        return PUMP_FEE

    return Pubkey.from_bytes(
        global_data[RESERVED_FEE_RECIPIENT_OFFSET : RESERVED_FEE_RECIPIENT_OFFSET + 32]
    )


def _derive_ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return _find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )


def _create_associated_token_account_idempotent_ix(
    *,
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
    token_program: Pubkey,
    ata: Pubkey,
) -> Instruction:
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([1]),
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


def _get_latest_blockhash(rpc_url: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "processed"}],
    }
    data = _rpc_post(rpc_url, payload)
    result = ((data.get("result") or {}).get("value") or {}).get("blockhash")
    if not result:
        raise ValueError(f"getLatestBlockhash returned no blockhash: {data}")
    return str(result)


def _send_transaction_multi_rpc(
    primary_rpc_url: str,
    tx_bytes: bytes,
    *,
    skip_preflight: bool,
) -> str:
    """Broadcast a signed transaction to multiple RPC endpoints in parallel.

    Returns the signature from the first endpoint that accepts it. All endpoints
    receive the same serialized bytes — the signature is deterministic and identical
    regardless of which RPC responds first. Spray RPCs always skip preflight since
    the primary already ran simulation if required.
    """
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    all_urls = [primary_rpc_url] + SELL_SPRAY_RPCS
    last_exc: Exception | None = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(all_urls))
    try:
        futures = {
            executor.submit(
                _send_transaction,
                url,
                tx_bytes,
                skip_preflight=skip_preflight if i == 0 else True,
            ): url
            for i, url in enumerate(all_urls)
        }
        for fut in concurrent.futures.as_completed(futures):
            try:
                return fut.result()
            except Exception as exc:
                last_exc = exc
    finally:
        executor.shutdown(wait=False)
    raise RuntimeError(
        f"all RPC endpoints failed for SELL send; last error: {last_exc}"
    ) from last_exc


def _send_transaction(rpc_url: str, tx_bytes: bytes, *, skip_preflight: bool) -> str:
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            base64.b64encode(tx_bytes).decode("ascii"),
            {
                "encoding": "base64",
                "skipPreflight": bool(skip_preflight),
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    }
    data = _rpc_post(rpc_url, payload)
    result = data.get("result")
    if not result:
        raise ValueError(f"sendTransaction returned no signature: {data}")
    return str(result)


def _get_account_info(rpc_url: str, account: str) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [account, {"encoding": "base64", "commitment": "confirmed"}],
    }
    data = _rpc_post(rpc_url, payload)
    value = (data.get("result") or {}).get("value")
    if not value:
        raise ValueError(f"account not found: {account}")
    return value


def _account_data_bytes(info: dict[str, Any]) -> bytes:
    data = info.get("data") or []
    if not isinstance(data, list) or not data:
        raise ValueError("account data missing or not base64-encoded")
    return base64.b64decode(data[0])


def _find_program_address(seeds: list[bytes], program_id: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program_id)[0]


def _send_jito_bundle(tx: Transaction, jito_endpoint: str) -> str:
    """Send a signed transaction as a 1-tx Jito bundle.

    Returns the tx signature (extracted before send) so confirm_transaction()
    can poll getSignatureStatuses normally once the bundle lands on-chain.
    """
    raise RuntimeError("Historical construction preserved; signing/submission disabled in public release")
    import base58 as _base58
    tx_bytes = bytes(tx)
    tx_b58 = _base58.b58encode(tx_bytes).decode("ascii")
    tx_signature = str(tx.signatures[0])
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [[tx_b58]],
    }
    resp = requests.post(
        jito_endpoint,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error") is not None:
        raise ValueError(f"Jito sendBundle error: {data['error']}")
    return tx_signature


def _rpc_post(rpc_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    from trading.safety import require_read_only_rpc; require_read_only_rpc(payload)
    resp = requests.post(
        rpc_url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error") is not None:
        raise ValueError(f"rpc error: {data['error']}")
    return data
