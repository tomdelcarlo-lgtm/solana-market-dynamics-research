# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""Actual historical binary parser, extracted from the policy runtime.
Publication changes: local fixture SOL/USD dependency and 105-byte length check.
No real token, wallet or transaction record is included.
"""
import base64, hashlib, struct, time, logging, os
from typing import Optional, Dict
log = logging.getLogger(__name__)
def get_sol_price():
    return float(os.getenv("SOLANA_FIXTURE_SOL_USD", "150"))
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def _discriminator(event_name: str) -> bytes:
    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]

def _pubkey_str(data: bytes, offset: int) -> str:
    """Decode a 32-byte Solana public key as a base58-like hex string."""
    return base58_encode(data[offset : offset + 32])

def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = []
    while n:
        n, rem = divmod(n, 58)
        result.append(_B58_ALPHABET[rem : rem + 1])
    result.extend(b"1" * (len(data) - len(data.lstrip(b"\x00"))))
    return b"".join(reversed(result)).decode()

def _parse_trade_event(payload: bytes) -> Optional[Dict]:
    """
    Decode a pump.fun TradeEvent from raw Anchor event bytes (after discriminator).

    Layout (little-endian):
      mint                  32 bytes  pubkey
      sol_amount             8 bytes  u64   (lamports)
      token_amount           8 bytes  u64
      is_buy                 1 byte   bool
      user                  32 bytes  pubkey
      timestamp              8 bytes  i64
      virtual_sol_reserves   8 bytes  u64   (lamports)
      virtual_token_reserves 8 bytes  u64
    Total: 105 bytes; corrected original truncated-input check
    """
    if len(payload) < 105:
        return None
    offset = 0

    mint_bytes          = payload[offset:offset+32]; offset += 32
    sol_amount_lam,     = struct.unpack_from("<Q", payload, offset); offset += 8
    token_amount,       = struct.unpack_from("<Q", payload, offset); offset += 8
    is_buy              = payload[offset] != 0;                       offset += 1
    user_bytes          = payload[offset:offset+32]; offset += 32
    timestamp,          = struct.unpack_from("<q", payload, offset); offset += 8
    virt_sol,           = struct.unpack_from("<Q", payload, offset); offset += 8
    virt_tok,           = struct.unpack_from("<Q", payload, offset); offset += 8

    mint_str = base58_encode(mint_bytes)
    user_str = base58_encode(user_bytes)
    sol_amount = sol_amount_lam / 1e9          # lamports → SOL

    sol_price  = get_sol_price()
    usd        = sol_amount * sol_price

    # Spot price in SOL per token (bonding curve ratio)
    price_sol = (virt_sol / 1e9) / (virt_tok / 1e6) if virt_tok > 0 else sol_amount

    return {
        "mint":         mint_str,
        "side":         "buy" if is_buy else "sell",
        "usd":          usd,
        "price":        price_sol,
        "wallet":       user_str,
        "ts":           float(timestamp) if timestamp > 1_000_000_000 else time.time(),
        "sol_amount":   sol_amount,
        "token_amount": token_amount,
        "virt_sol":     virt_sol,
        "virt_tok":     virt_tok,
    }

def parse_trade(msg: Dict) -> Optional[Dict]:
    """
    Parse a Helius logsSubscribe notification into a normalised trade dict.

    Pump.fun emits Anchor CPI events encoded as base64 in "Program data:" log
    lines.  Each event starts with an 8-byte discriminator:
      sha256("event:TradeEvent")[:8]   → trade
      sha256("event:CreateEvent")[:8]  → token creation (ignored here)

    Returns a dict with keys: mint, side, usd, price, wallet, ts
    or None if no parseable pump.fun TradeEvent is found.
    """
    try:
        value = msg.get("params", {}).get("result", {}).get("value", {})
        logs  = value.get("logs", [])

        # Gate: must involve the pump.fun program
        if not any(PUMPFUN_PROGRAM in l for l in logs):
            return None

        for line in logs:
            # Anchor events appear on "Program data: <base64>" lines
            if not line.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(line[len("Program data: "):])
            except Exception:
                continue

            if len(raw) < 8:
                continue

            disc = raw[:8]
            payload = raw[8:]

            if disc == DISC_TRADE:
                return _parse_trade_event(payload)
            # CreateEvent — not handled as a trade, skip

        return None

    except Exception as exc:
        log.debug(f"parse_trade error: {exc}")
        return None

DISC_TRADE = _discriminator("TradeEvent")
DISC_CREATE = _discriminator("CreateEvent")
