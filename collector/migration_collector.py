# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
#!/usr/bin/env python3
"""Fase B — Migration collector (read-only, no trading).

Standalone process, independent of pumpfun_bot.py / micro_live. It:

1. Subscribes to a single Helius `logsSubscribe` feed on the pump.fun
   program. Each message is used for two independent purposes:

   a. Pre-migration "ADN" tracking: per-mint price/volume from token
      creation (chg_first_to_last_pct in the first minute, early
      volume/buyers/pressure proxy) — same as before, kept for
      cross-referencing post-pool behaviour against pump origin.

   b. Migration detection: an exact-match check for
      "Program log: Instruction: Migrate" / "...MigrateV2" — the real
      bonding-curve -> PumpSwap migration instructions (this excludes
      the unrelated "MigrateBondingCurveCreator" fee-sharing call,
      which also contains the substring "Migrate"). This targets platform migration events (coverage is not guaranteed), independent of
      whether this process saw the token born or crossed any mcap
      threshold.

2. On a migration match, fetches the transaction once (by signature,
   no search) to read `account_mint`/`account_base_mint` and
   `account_pool` directly from the instruction's account list
   (fixed positions verified against live pump.fun "Migrate"/
   "MigrateV2" transactions). Logs a MIGRATION_DETECTED row, attaching
   the pre-migration ADN if this process tracked the mint from birth.

3. Reads the PumpSwap pool account once (301 bytes, discriminator
   f19a6d0411b16dbc; base_mint @43, quote_mint @75, base_vault @139,
   quote_vault @171 — all verified against a live pool) to get the
   reserve vault addresses directly, then polls those vaults every
   POOL_POLL_INTERVAL_S for up to TRACK_WINDOW_HOURS, logging
   POOL_SNAPSHOT rows with reserves/price.

All output goes to logs/migration_tape.csv. This process holds no wallet,
sends no transactions, and never calls risk/engine.py or ops/live_control.py.
"""
from __future__ import annotations

import asyncio
import base64
import base58
import csv
import json
import logging
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root, require_network
from typing import Any, Optional

import websockets

from trading.execution.pumpfun_native import (
    PUMP_PROGRAM,
    _account_data_bytes,
    _get_account_info,
    _rpc_post,
)

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT = runtime_root()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
_live_rpc_url = os.getenv("LIVE_RPC_URL", "").strip()
RPC_URL = _live_rpc_url
HELIUS_WS_URL = os.getenv("SOLANA_WS_URL", "").strip()

PUMPFUN_PROGRAM = str(PUMP_PROGRAM)
# PumpSwap Pool account layout — verified against a live pool account
# (301 bytes, discriminator f19a6d0411b16dbc):
#   8  discriminator
#   1  pool_bump
#   2  index (u16)
#   32 creator
#   32 base_mint   (confirmed at offset 43)
#   32 quote_mint  (WSOL, confirmed at offset 75)
#   32 lp_mint     (offset 107, not used)
#   32 pool_base_token_account  (confirmed at offset 139)
#   32 pool_quote_token_account (confirmed at offset 171)
#   8  lp_supply
POOL_BASE_VAULT_OFFSET = 139
POOL_QUOTE_VAULT_OFFSET = 171

# Exact-match log lines for the real bonding-curve -> PumpSwap migration
# instructions. Substring matching is NOT safe: "Instruction:
# MigrateBondingCurveCreator" (an unrelated, high-frequency creator
# fee-sharing call) also contains "Migrate".
MIGRATE_LOG_LINES = {
    "Program log: Instruction: Migrate": "Migrate",
    "Program log: Instruction: MigrateV2": "MigrateV2",
}

MIGRATION_TAPE_PATH = REPO_ROOT / "logs" / "migration_tape.csv"
RECONNECT_DELAY = 5

POOL_POLL_INTERVAL_S = float(os.getenv("MIGRATION_POOL_POLL_S", "60"))
TRACK_WINDOW_HOURS = float(os.getenv("MIGRATION_TRACK_WINDOW_HOURS", "48"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("migration_collector")

# ── CSV ───────────────────────────────────────────────────────────────────────
TAPE_FIELDS = [
    "timestamp",
    "mint",
    "event",
    "token_age_sec",
    "mcap_usd",
    "sol_usd",
    "chg_first_to_last_pct_1min",
    "vol_first_60s_usd",
    "trades_first_60s",
    "unique_buyers_first_60s",
    "buy_volume_share_first_60s",
    "pool_address",
    "pool_base_vault",
    "pool_quote_vault",
    "pool_base_reserve",
    "pool_quote_reserve",
    "pool_base_decimals",
    "pool_quote_decimals",
    "pool_price_sol",
    "pool_price_usd",
    "note",
]


def _write_row(row: dict[str, Any]) -> None:
    MIGRATION_TAPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not MIGRATION_TAPE_PATH.exists()
    with open(MIGRATION_TAPE_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TAPE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        full = {k: row.get(k, "") for k in TAPE_FIELDS}
        writer.writerow(full)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SOL price (lightweight cache, independent of pumpfun_bot) ──────────────────
_sol_price_cache: dict[str, float] = {"price": 150.0, "fetched_at": 0.0}
_SOL_PRICE_REFRESH = 300


def get_sol_price() -> float:
    if os.getenv("SOLANA_ALLOW_NETWORK") != "1": return float(os.getenv("SOLANA_FIXTURE_SOL_USD", "150"))
    now = time.time()
    if now - _sol_price_cache["fetched_at"] < _SOL_PRICE_REFRESH:
        return _sol_price_cache["price"]
    try:
        import urllib.request

        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        price = float(data["solana"]["usd"])
        _sol_price_cache["price"] = price
        _sol_price_cache["fetched_at"] = now
    except Exception as exc:  # noqa: BLE001
        log.debug("get_sol_price failed, keeping cached value: %s", exc)
    return _sol_price_cache["price"]


# ── pump.fun trade event parsing (self-contained copy of pumpfun_bot logic) ────
import hashlib  # noqa: E402


def _discriminator(event_name: str) -> bytes:
    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]


DISC_TRADE = _discriminator("TradeEvent")
TOKEN_TOTAL_SUPPLY = 1_000_000_000  # pump.fun fixed supply, 6 decimals


def _b58(data: bytes) -> str:
    return base58.b58encode(data).decode()


def _parse_trade_event(payload: bytes) -> Optional[dict]:
    """Decode a pump.fun TradeEvent (105-byte body after the discriminator)."""
    if len(payload) < 105:
        return None
    offset = 0
    mint_bytes = payload[offset:offset + 32]; offset += 32
    sol_amount_lam, = struct.unpack_from("<Q", payload, offset); offset += 8
    token_amount, = struct.unpack_from("<Q", payload, offset); offset += 8
    is_buy = payload[offset] != 0; offset += 1
    user_bytes = payload[offset:offset + 32]; offset += 32
    timestamp, = struct.unpack_from("<q", payload, offset); offset += 8
    virt_sol, = struct.unpack_from("<Q", payload, offset); offset += 8
    virt_tok, = struct.unpack_from("<Q", payload, offset); offset += 8

    sol_amount = sol_amount_lam / 1e9
    price_sol = (virt_sol / 1e9) / (virt_tok / 1e6) if virt_tok > 0 else 0.0

    return {
        "mint": _b58(mint_bytes),
        "side": "buy" if is_buy else "sell",
        "usd": sol_amount * get_sol_price(),
        "price_sol": price_sol,
        "wallet": _b58(user_bytes),
        "ts": float(timestamp) if timestamp > 1_000_000_000 else time.time(),
    }


def parse_trade(msg: dict) -> Optional[dict]:
    try:
        value = msg.get("params", {}).get("result", {}).get("value", {})
        logs = value.get("logs", [])
        if not any(PUMPFUN_PROGRAM in l for l in logs):
            return None
        for line in logs:
            if not line.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(line[len("Program data: "):])
            except Exception:
                continue
            if len(raw) < 8:
                continue
            if raw[:8] == DISC_TRADE:
                return _parse_trade_event(raw[8:])
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("parse_trade error: %s", exc)
        return None


# ── Per-mint state ───────────────────────────────────────────────────────────
class MintState:
    __slots__ = (
        "start_time", "first_price_sol", "price_60s", "chg_60s",
        "vol_60s_usd", "trades_60s", "buyers_60s", "buy_vol_60s_usd",
        "last_price_sol", "last_mcap_usd", "migration_logged",
    )

    def __init__(self, start_time: float, first_price_sol: float) -> None:
        self.start_time = start_time
        self.first_price_sol = first_price_sol
        self.price_60s: Optional[float] = None
        self.chg_60s: Optional[float] = None
        self.vol_60s_usd = 0.0
        self.trades_60s = 0
        self.buyers_60s: set[str] = set()
        self.buy_vol_60s_usd = 0.0
        self.last_price_sol = first_price_sol
        self.last_mcap_usd = 0.0
        self.migration_logged = False


MINTS: dict[str, MintState] = {}
TRACKED_POOLS: dict[str, dict[str, Any]] = {}  # mint -> pool tracking info

# Bounded set of already-processed migration tx signatures (dedupe against
# websocket redelivery on reconnect).
_PROCESSED_SIGS: set[str] = set()
_PROCESSED_SIGS_MAX = 2000


def _mcap_usd(price_sol: float, sol_usd: float) -> float:
    return price_sol * TOKEN_TOTAL_SUPPLY * sol_usd


# ── WS listener ──────────────────────────────────────────────────────────────
async def listen() -> None:
    require_network()
    subscribe_payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [{"mentions": [PUMPFUN_PROGRAM]}, {"commitment": "confirmed"}],
    })

    while True:
        try:
            log.info("Connecting to Helius WebSocket…")
            async with websockets.connect(
                HELIUS_WS_URL, ping_interval=30, ping_timeout=60, max_size=10 * 1024 * 1024,
            ) as ws:
                await ws.send(subscribe_payload)
                ack = await ws.recv()
                log.info("Subscribed ack=%s", str(ack)[:120])

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "id" in msg and "result" in msg:
                        continue

                    trade = parse_trade(msg)
                    if trade:
                        _handle_trade(trade)

                    value = msg.get("params", {}).get("result", {}).get("value", {})
                    logs = value.get("logs", [])
                    for line in logs:
                        instr_version = MIGRATE_LOG_LINES.get(line.strip())
                        if instr_version:
                            sig = value.get("signature")
                            if sig:
                                asyncio.create_task(_on_migration_event(sig, instr_version))
                            break
        except (websockets.ConnectionClosed, websockets.WebSocketException, OSError) as exc:
            exc_str = str(exc)
            delay = 60 if "429" in exc_str else RECONNECT_DELAY
            log.warning("WebSocket disconnected: %s — retrying in %ss", exc, delay)
            await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            log.error("Unexpected error in listen(): %s", exc, exc_info=True)
            await asyncio.sleep(RECONNECT_DELAY)


def _handle_trade(trade: dict) -> None:
    mint = trade["mint"]
    ts = trade["ts"]
    price_sol = trade["price_sol"]
    if price_sol <= 0:
        return
    sol_usd = get_sol_price()

    state = MINTS.get(mint)
    if state is None:
        state = MintState(start_time=ts, first_price_sol=price_sol)
        MINTS[mint] = state

    age = ts - state.start_time

    # ADN pre-migración: capture the first-60s price-change/volume window.
    if age <= 60:
        state.vol_60s_usd += trade["usd"]
        state.trades_60s += 1
        if trade["side"] == "buy":
            state.buy_vol_60s_usd += trade["usd"]
            state.buyers_60s.add(trade["wallet"])
        state.price_60s = price_sol
    elif state.chg_60s is None and state.price_60s is not None:
        if state.first_price_sol > 0:
            state.chg_60s = (state.price_60s / state.first_price_sol - 1.0) * 100.0

    state.last_price_sol = price_sol
    state.last_mcap_usd = _mcap_usd(price_sol, sol_usd)


# ── Migration event handling (logsSubscribe "Instruction: Migrate") ────────
def _extract_mint_pool(sig: str, instr_version: str) -> tuple[Optional[str], Optional[str]]:
    """Read account_mint/account_base_mint and account_pool from a migrate tx.

    Account positions (verified against live Migrate/MigrateV2 txs, matching
    Dune's decoded pump_call_migrate / pump_call_migrate_v2 ordering):
      Migrate:   accounts[2]=mint,       accounts[9]=pool
      MigrateV2: accounts[2]=base_mint,  accounts[10]=pool
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
    }
    tx = None
    for _ in range(3):
        data = _rpc_post(RPC_URL, payload)
        tx = data.get("result")
        if tx is not None:
            break
        time.sleep(2)
    if tx is None:
        return None, None

    msg = tx["transaction"]["message"]
    keys = list(msg["accountKeys"])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])

    pool_idx = 10 if instr_version == "MigrateV2" else 9
    for instr in msg["instructions"]:
        if keys[instr["programIdIndex"]] != PUMPFUN_PROGRAM:
            continue
        accs = instr["accounts"]
        if len(accs) <= pool_idx:
            continue
        return keys[accs[2]], keys[accs[pool_idx]]
    return None, None


async def _on_migration_event(sig: str, instr_version: str) -> None:
    if sig in _PROCESSED_SIGS:
        return
    _PROCESSED_SIGS.add(sig)
    if len(_PROCESSED_SIGS) > _PROCESSED_SIGS_MAX:
        _PROCESSED_SIGS.clear()

    try:
        mint, pool = await asyncio.to_thread(_extract_mint_pool, sig, instr_version)
    except Exception as exc:  # noqa: BLE001
        log.warning("migration tx fetch failed sig=%s: %s", sig, exc)
        return
    if mint is None or pool is None:
        log.warning("could not extract mint/pool from migration tx sig=%s", sig)
        return

    state = MINTS.get(mint)
    age = (time.time() - state.start_time) if state else 0.0
    sol_usd = get_sol_price()
    row = {
        "timestamp": _now_iso(),
        "mint": mint,
        "event": "MIGRATION_DETECTED",
        "token_age_sec": f"{age:.1f}" if state else "",
        "mcap_usd": f"{state.last_mcap_usd:.2f}" if state else "",
        "sol_usd": f"{sol_usd:.4f}",
        "chg_first_to_last_pct_1min": "" if not state or state.chg_60s is None else f"{state.chg_60s:.4f}",
        "vol_first_60s_usd": f"{state.vol_60s_usd:.4f}" if state else "",
        "trades_first_60s": state.trades_60s if state else "",
        "unique_buyers_first_60s": len(state.buyers_60s) if state else "",
        "buy_volume_share_first_60s": (
            f"{(state.buy_vol_60s_usd / state.vol_60s_usd):.4f}" if state and state.vol_60s_usd > 0 else ""
        ),
        "pool_address": pool,
        "note": f"event={instr_version} sig={sig}",
    }
    _write_row(row)
    log.info("MIGRATION_DETECTED mint=%s pool=%s event=%s sig=%s", mint, pool, instr_version, sig)

    if state is not None:
        state.migration_logged = True

    try:
        await asyncio.to_thread(_init_pool_tracking, mint, pool)
    except Exception as exc:  # noqa: BLE001
        log.warning("pool init failed mint=%s pool=%s: %s", mint, pool, exc)


# Pool account lookup retries: the pool account is sometimes not yet visible
# to the RPC node within <1s of the Migrate instruction's logs arriving
# (propagation lag), so a single immediate getAccountInfo can spuriously
# report "account not found". Retry with a short backoff before giving up.
POOL_LOOKUP_RETRY_DELAYS_S = (1, 2, 3)


def _init_pool_tracking(mint: str, pool_address: str) -> None:
    info = None
    last_exc: Optional[Exception] = None
    for attempt in range(1 + len(POOL_LOOKUP_RETRY_DELAYS_S)):
        if attempt > 0:
            time.sleep(POOL_LOOKUP_RETRY_DELAYS_S[attempt - 1])
        try:
            info = _get_account_info(RPC_URL, pool_address)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "pool lookup attempt=%d failed mint=%s pool=%s: %s",
                attempt + 1, mint, pool_address, exc,
            )

    if info is None:
        _write_row({
            "timestamp": _now_iso(), "mint": mint, "event": "POOL_NOT_FOUND",
            "pool_address": pool_address,
            "note": f"attempts={1 + len(POOL_LOOKUP_RETRY_DELAYS_S)} last_error={last_exc}",
        })
        log.warning("POOL_NOT_FOUND mint=%s pool=%s after retries: %s", mint, pool_address, last_exc)
        return

    data = _account_data_bytes(info)

    if len(data) < POOL_QUOTE_VAULT_OFFSET + 32:
        _write_row({
            "timestamp": _now_iso(), "mint": mint, "event": "POOL_PARSE_ERROR",
            "pool_address": pool_address, "note": f"unexpected_account_len={len(data)}",
        })
        return

    base_vault = _b58(data[POOL_BASE_VAULT_OFFSET:POOL_BASE_VAULT_OFFSET + 32])
    quote_vault = _b58(data[POOL_QUOTE_VAULT_OFFSET:POOL_QUOTE_VAULT_OFFSET + 32])

    track = {
        "deadline": time.time() + TRACK_WINDOW_HOURS * 3600,
        "pool_address": pool_address,
        "pool_base_vault": base_vault,
        "pool_quote_vault": quote_vault,
    }
    TRACKED_POOLS[mint] = track

    _write_row({
        "timestamp": _now_iso(), "mint": mint, "event": "POOL_FOUND",
        "pool_address": pool_address, "pool_base_vault": base_vault, "pool_quote_vault": quote_vault,
        "note": f"account_len={len(data)}",
    })
    log.info("POOL_FOUND mint=%s pool=%s", mint, pool_address)

    # First snapshot immediately.
    _snapshot_pool(mint, track)


# ── PumpSwap pool reserve poller ────────────────────────────────────────────
async def pool_poll_loop() -> None:
    while True:
        await asyncio.sleep(POOL_POLL_INTERVAL_S)
        if not TRACKED_POOLS:
            continue
        for mint in list(TRACKED_POOLS.keys()):
            track = TRACKED_POOLS[mint]
            if time.time() > track["deadline"]:
                _write_row({
                    "timestamp": _now_iso(), "mint": mint, "event": "TRACKING_EXPIRED",
                    "pool_address": track["pool_address"],
                })
                del TRACKED_POOLS[mint]
                continue

            try:
                await asyncio.to_thread(_snapshot_pool, mint, track)
            except Exception as exc:  # noqa: BLE001
                log.debug("pool poll error mint=%s: %s", mint, exc)


def _spl_token_amount_and_decimals(account: str) -> tuple[int, int]:
    """Return (raw_amount, decimals) for an SPL token account.

    SPL Token Account layout: mint(32) owner(32) amount(u64 @64) ...
    Decimals are read from the account's mint (offset 44 in the Mint account).
    """
    info = _get_account_info(RPC_URL, account)
    data = _account_data_bytes(info)
    if len(data) < 72:
        raise ValueError(f"token account too short: {len(data)} bytes")
    amount, = struct.unpack_from("<Q", data, 64)
    mint_bytes = data[0:32]
    mint_b58 = _b58(mint_bytes)
    mint_info = _get_account_info(RPC_URL, mint_b58)
    mint_data = _account_data_bytes(mint_info)
    decimals = mint_data[44] if len(mint_data) > 44 else 0
    return amount, decimals


def _snapshot_pool(mint: str, track: dict[str, Any]) -> None:
    base_vault = track["pool_base_vault"]
    quote_vault = track["pool_quote_vault"]
    if not base_vault or not quote_vault:
        return

    try:
        base_amount, base_decimals = _spl_token_amount_and_decimals(base_vault)
        quote_amount, quote_decimals = _spl_token_amount_and_decimals(quote_vault)
    except Exception as exc:  # noqa: BLE001
        _write_row({
            "timestamp": _now_iso(), "mint": mint, "event": "POOL_PARSE_ERROR",
            "pool_address": track["pool_address"], "note": f"vault_read_failed: {exc}",
        })
        return

    base_reserve = base_amount / (10 ** base_decimals) if base_decimals else float(base_amount)
    quote_reserve = quote_amount / (10 ** quote_decimals) if quote_decimals else float(quote_amount)
    price_sol = (quote_reserve / base_reserve) if base_reserve > 0 else 0.0
    sol_usd = get_sol_price()

    _write_row({
        "timestamp": _now_iso(), "mint": mint, "event": "POOL_SNAPSHOT",
        "pool_address": track["pool_address"],
        "pool_base_vault": base_vault, "pool_quote_vault": quote_vault,
        "pool_base_reserve": f"{base_reserve:.6f}", "pool_quote_reserve": f"{quote_reserve:.6f}",
        "pool_base_decimals": base_decimals, "pool_quote_decimals": quote_decimals,
        "pool_price_sol": f"{price_sol:.12f}", "pool_price_usd": f"{price_sol * sol_usd:.8f}",
        "mcap_usd": f"{_mcap_usd(price_sol, sol_usd):.2f}",
        "sol_usd": f"{sol_usd:.4f}",
    })


# ── Housekeeping: trim MINTS to avoid unbounded growth ─────────────────────────
async def gc_loop() -> None:
    while True:
        await asyncio.sleep(600)
        stale = [
            m for m, s in MINTS.items()
            if (time.time() - s.start_time) > 6 * 3600
            and m not in TRACKED_POOLS
        ]
        for m in stale:
            del MINTS[m]
        if stale:
            log.info("gc: dropped %d stale mints, tracking=%d", len(stale), len(TRACKED_POOLS))


async def main() -> None:
    require_network()
    log.info("migration_collector starting — tape=%s", MIGRATION_TAPE_PATH)
    await asyncio.gather(listen(), pool_poll_loop(), gc_loop())


if __name__ == "__main__":
    asyncio.run(main())
