"""Original observed-token reconstruction and bounded deduplication.
Callbacks are explicit replay hooks; defaults are no-ops, never orders.
"""
import collections, threading, time, logging
from typing import Dict
from collector.parser import get_sol_price
from market_state.tape import log_outcome_v2_tape, log_csv
log=logging.getLogger(__name__)
tokens={}
tokens_lock=threading.Lock()
open_positions={}
positions_lock=threading.Lock()
_dl_write_trade=None
def maybe_evaluate(mint): pass
def evaluate_exit(mint, position, price, now): pass
_DEDUP_MAXSIZE = 50_000   # ~4 h headroom at peak pump.fun volume (~200 tx/min)
_seen_tx_sigs: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_seen_tx_sigs_lock = threading.Lock()
_DEDUP_TRADE_MAXSIZE = 20_000
_seen_trade_keys: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
_seen_trade_keys_lock = threading.Lock()
def _is_duplicate_sig(sig: str) -> bool:
    """Return True if sig was already processed this session; record it if new."""
    if not sig:
        return False
    with _seen_tx_sigs_lock:
        if sig in _seen_tx_sigs:
            return True
        _seen_tx_sigs[sig] = None
        if len(_seen_tx_sigs) > _DEDUP_MAXSIZE:
            _seen_tx_sigs.popitem(last=False)   # evict oldest entry
    return False

def _is_duplicate_trade(trade: dict) -> bool:
    """Return True if this exact trade content was already processed this session."""
    key = (
        trade.get("mint", ""),
        trade.get("wallet", ""),
        trade.get("side", ""),
        int(trade.get("ts", 0)),          # blockchain timestamp (seconds, integer)
        trade.get("token_amount", 0),     # raw token units — unique per tx
    )
    with _seen_trade_keys_lock:
        if key in _seen_trade_keys:
            return True
        _seen_trade_keys[key] = None
        if len(_seen_trade_keys) > _DEDUP_TRADE_MAXSIZE:
            _seen_trade_keys.popitem(last=False)
    return False

def handle_trade(trade: dict) -> None:
    # Content-based dedup: guard against Helius re-delivering the same tx when
    # the message has no signature (sig-level dedup in listen() would have missed it).
    if _is_duplicate_trade(trade):
        log.debug(
            f"dedup(content): dropped duplicate trade "
            f"{trade.get('mint','')[:8]} wallet={trade.get('wallet','')[:8]} "
            f"ts={int(trade.get('ts',0))} amt={trade.get('token_amount',0)}"
        )
        return

    mint = trade["mint"]

    with tokens_lock:
        is_new = mint not in tokens
        if is_new:
            tokens[mint] = {
                "start_time": trade["ts"],
                "trades":     [],
                "evaluated":  False,
                "position":   None,
            }
        tokens[mint]["trades"].append(trade)
        state = tokens[mint]

    sol_usd = get_sol_price()

    if _dl_write_trade is not None:
        try:
            _dl_write_trade(trade, sol_usd)
        except Exception as _e:
            log.warning(f"[dl] write_trade: {_e}")

    try:
        log_outcome_v2_tape(trade, state, sol_usd)
    except Exception as _e:
        log.warning(f"[v2_tape] write_trade: {_e}")

    if is_new:
        print(f"👁 NEW TOKEN: {mint[:8]}... | total monitored: {len(tokens)}")
        log_csv(mint=mint, action="SEEN", reason="new_token_detected")

    # Check evaluation window for potential entry
    maybe_evaluate(mint)

    # Update exit conditions for any existing open position on this mint
    with positions_lock:
        pos = open_positions.get(mint)
    if pos and not pos.get("closed") and trade.get("price", 0) > 0:
        evaluate_exit(mint, pos, trade["price"], time.time())
