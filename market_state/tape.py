"""Original CSV tape and decision observability functions, extracted from the runtime."""
import csv, threading, time, logging, collections
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any
from trading.safety import runtime_root
log = logging.getLogger(__name__)
REPO_ROOT = runtime_root()
LOG_PATH_V2 = REPO_ROOT / "logs/paper_trading_log_v2.csv"
RULE_OBS_PATH = REPO_ROOT / "logs/rule_observability.csv"
OUTCOME_V2_TAPE_PATH = REPO_ROOT / "logs/outcome_v2_tape.csv"
_csv_lock = threading.Lock()
_tape_lock = threading.Lock()
TRADING_MODE = "rule_based"
LOCAL_TZ = timezone.utc
_score_window=[]
_score_window_lock=threading.Lock()
_SCORE_WINDOW_SIZE=500
CSV_COLUMNS = [
    # ── Identity ──────────────────────────────────────────────────────────────
    "timestamp",
    "token",
    "action",
    "reason",
    "trade_id",
    "trading_mode",
    "policy_track",
    # ── Model / diagnostics ───────────────────────────────────────────────────
    "score",
    "p_win",
    "ev",
    # ── Features / signal stats ───────────────────────────────────────────────
    "volume_first_30min",
    "unique_buyers",
    "avg_trade_size",
    "buy_sell_ratio",
    "buy_volume_share_to_signal",
    "entry_mcap_clean",
    "concurrent_open_positions",
    # ── Filter flags ──────────────────────────────────────────────────────────
    "passed_volume",
    "passed_buyers",
    "passed_avg_size",
    "passed_buy_sell",
    "all_filters_passed",
    # ── Trade tracking ────────────────────────────────────────────────────────
    "entry_price",
    "exit_price",
    "gross_multiple",
    "net_multiple_est",
    "pnl_pct",
    "pnl_usd",
    "position_size_usd",
    # ── Ranking metadata ──────────────────────────────────────────────────────
    "ranking_position",
    "ranking_gap",
    # ── Phase-space axes (structure quality + activity density + breadth) ────
    "top1_wallet_pct_30min",
    "top3_wallet_pct_30min",
    "trades_per_minute",
    "trade_size_cv",
    "buyers_velocity",
    "buyers_acceleration",
    # ── Timing ────────────────────────────────────────────────────────────────
    "age_min",
    "signal_time",
    "signal_hour_utc",
    "entry_time",
    "exit_time",
    # ── Score window stats — non-empty only on RANKING_STATS rows ─────────────
    "score_min",
    "score_max",
    "score_mean",
    "score_p50",
    "score_p75",
    "score_p90",
    "score_p95",
]
RULE_OBS_COLUMNS = [
    "timestamp",
    "token",
    "stage",
    "track",
    "reason",
    "token_age_min",
    "signal_age_min",
    "signal_time",
    "buyers_threshold",
    "unique_buyers_30min",
    "signal_unique_buyers",
    "volume_first_30min",
    "avg_trade_size",
    "buy_sell_ratio",
    "first_price_sol",
    "signal_price_sol",
    "last_price_sol",
    "chg_first_to_signal_pct",
    "chg_first_to_last_pct",
    "chg_signal_to_last_pct",
    "pressure",
    "pressure_min",
    "pressure_max",
    "entry_mcap_usd",
    "entry_mcap_min",
    "entry_mcap_max",
    "clean_trade_usd_min",
    "clean_trade_count",
    "score_hint",
    "open_positions",
]
OUTCOME_V2_TAPE_COLUMNS = [
    "timestamp",
    "mint",
    "tx_sig",    # Solana transaction signature (canonical event identity; empty if absent)
    "tx_slot",   # Solana slot number (ordering anchor; empty if absent)
    "side",
    "wallet",
    "trade_usd",
    "price_sol",
    "sol_usd",
    "price_usd",
    "mcap_usd",
    "token_age_sec",
    "total_trades_seen",
    "unique_buyers_seen",
    "unique_sellers_seen",
    "buy_trades_seen",
    "sell_trades_seen",
    "buy_volume_usd_seen",
    "sell_volume_usd_seen",
    "buy_volume_share_seen",
]
def _ensure_csv() -> None:
    """Ensure the log directories exist. Headers are written by log_csv() on first write."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)      # v1 directory (historical)
    LOG_PATH_V2.parent.mkdir(parents=True, exist_ok=True)   # v2 directory (new schema)
    RULE_OBS_PATH.parent.mkdir(parents=True, exist_ok=True) # rule observability log
    OUTCOME_V2_TAPE_PATH.parent.mkdir(parents=True, exist_ok=True)

def log_outcome_v2_tape(trade: Dict[str, Any], state: Dict[str, Any], sol_usd: float) -> None:
    trades = state.get("trades", [])
    if not trades:
        return

    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    buy_wallets = {t.get("wallet") for t in buys if t.get("wallet")}
    sell_wallets = {t.get("wallet") for t in sells if t.get("wallet")}
    buy_volume_usd = sum(float(t.get("usd", 0.0)) for t in buys)
    sell_volume_usd = sum(float(t.get("usd", 0.0)) for t in sells)
    total_volume_usd = buy_volume_usd + sell_volume_usd
    price_sol = float(trade.get("price", 0.0) or 0.0)
    price_usd = price_sol * sol_usd if price_sol > 0 else 0.0
    row = {
        "timestamp": datetime.fromtimestamp(float(trade.get("ts", time.time())), tz=timezone.utc).isoformat(),
        "mint": trade.get("mint", ""),
        "tx_sig":  trade.get("_sig", ""),   # Solana tx signature — set by listen() when available
        "tx_slot": trade.get("_slot", ""),  # Solana slot         — set by listen() when available
        "side": trade.get("side", ""),
        "wallet": trade.get("wallet", ""),
        "trade_usd": round(float(trade.get("usd", 0.0) or 0.0), 6),
        "price_sol": round(price_sol, 12) if price_sol > 0 else "",
        "sol_usd": round(sol_usd, 6),
        "price_usd": round(price_usd, 12) if price_usd > 0 else "",
        "mcap_usd": round(price_usd * 1e9, 2) if price_usd > 0 else "",
        "token_age_sec": round(float(trade.get("ts", 0.0)) - float(state.get("start_time", trade.get("ts", 0.0))), 3),
        "total_trades_seen": len(trades),
        "unique_buyers_seen": len(buy_wallets),
        "unique_sellers_seen": len(sell_wallets),
        "buy_trades_seen": len(buys),
        "sell_trades_seen": len(sells),
        "buy_volume_usd_seen": round(buy_volume_usd, 6),
        "sell_volume_usd_seen": round(sell_volume_usd, 6),
        "buy_volume_share_seen": round((buy_volume_usd / total_volume_usd), 6) if total_volume_usd > 0 else "",
    }

    with _tape_lock:
        exists = OUTCOME_V2_TAPE_PATH.exists()
        with OUTCOME_V2_TAPE_PATH.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTCOME_V2_TAPE_COLUMNS)
            if not exists:
                writer.writeheader()
            writer.writerow(row)

def log_csv(**kwargs) -> None:
    """Write one row to paper_trading_log_v2.csv (v2 fixed schema).

    Every row always contains all 32 columns in CSV_COLUMNS order.
    Columns not relevant to a given action are written as empty string
    (pandas reads them as NaN).

    Column mapping:
      "timestamp"  → always set to current UTC ISO string
      "token"      → taken from kwarg "mint"
      all others   → taken from kwarg of the same name; empty when absent

    Extra kwargs not in CSV_COLUMNS are silently discarded (extrasaction="ignore").
    This means call sites that still pass holding_time_sec or engineered feature
    columns (feat_kw) are unaffected — those values are just not written.
    """
    # Build a complete row with every column defaulting to empty string,
    # then overwrite with whatever kwargs were provided.
    row = {col: "" for col in CSV_COLUMNS}
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    row["token"]     = kwargs.get("mint", "")
    for col in CSV_COLUMNS:
        if col in ("timestamp", "token"):
            continue
        val = kwargs.get(col)
        if val is not None:
            row[col] = val

    with _csv_lock:
        file_exists = LOG_PATH_V2.exists()
        with open(LOG_PATH_V2, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

def log_rule_obs(**kwargs) -> None:
    row = {col: "" for col in RULE_OBS_COLUMNS}
    row["timestamp"] = kwargs.get("timestamp") or datetime.now(timezone.utc).isoformat()
    row["token"] = kwargs.get("token") or kwargs.get("mint", "")
    for col in RULE_OBS_COLUMNS:
        if col in ("timestamp", "token"):
            continue
        val = kwargs.get(col)
        if val is not None:
            row[col] = val

    with _csv_lock:
        file_exists = RULE_OBS_PATH.exists()
        with open(RULE_OBS_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RULE_OBS_COLUMNS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
