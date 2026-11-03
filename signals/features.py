# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""Historical feature and signal functions extracted without changing formulas.
Only NumPy scalar log1p is replaced by standard-library math.log1p.
No learned model or training dataset is distributed.
"""
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
import math, os
def get_sol_price():
    return float(os.getenv("SOLANA_FIXTURE_SOL_USD", "150"))
MIN_VOLUME_30MIN   = 2_000
MIN_UNIQUE_BUYERS  = 20
MIN_AVG_TRADE_SIZE = 20
MIN_BUY_SELL_RATIO = 0.8
RULE_SIGNAL_UNIQUE_BUYERS = int(os.getenv("RULE_SIGNAL_UNIQUE_BUYERS", "60"))
RULE_MIN_CLEAN_TRADE_USD = float(os.getenv("RULE_MIN_CLEAN_TRADE_USD", "100"))
def compute_features(mint: str, start_time: float, trades: List[Dict]) -> Optional[Dict]:
    """Return feature dict computed from trades in the first 30 minutes."""
    cutoff = start_time + 30 * 60
    early  = [t for t in trades if t["ts"] <= cutoff]
    if not early:
        return None

    buys  = [t for t in early if t["side"] == "buy"]
    sells = [t for t in early if t["side"] == "sell"]

    total_vol  = sum(t["usd"] for t in early)
    n_trades   = len(early)
    buy_vol    = sum(t["usd"] for t in buys)
    sell_vol   = sum(t["usd"] for t in sells)
    buy_sell_r = buy_vol / sell_vol if sell_vol > 0 else float(buy_vol)
    max_buy    = max((t["usd"] for t in buys), default=0.0)
    avg_size   = total_vol / n_trades if n_trades else 0.0

    wallet_vol: dict = {}
    for t in early:
        wallet_vol[t["wallet"]] = wallet_vol.get(t["wallet"], 0.0) + t["usd"]
    sorted_wallet_vols = sorted(wallet_vol.values(), reverse=True)
    top1_pct = sorted_wallet_vols[0] / total_vol if total_vol > 0 and sorted_wallet_vols else 0.0
    top3_pct = sum(sorted_wallet_vols[:3]) / total_vol if total_vol > 0 and sorted_wallet_vols else 0.0

    trade_sizes = [t["usd"] for t in early if t["usd"] > 0]
    if len(trade_sizes) > 1:
        _mean_sz = sum(trade_sizes) / len(trade_sizes)
        _var = sum((x - _mean_sz) ** 2 for x in trade_sizes) / len(trade_sizes)
        trade_size_cv = (_var ** 0.5) / _mean_sz if _mean_sz > 0 else 0.0
    else:
        trade_size_cv = 0.0

    dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
    feats = {
        "volume_first_30min":    total_vol,
        "n_trades_30min":        n_trades,
        "unique_buyers_30min":   len({t["wallet"] for t in buys}),
        "unique_sellers_30min":  len({t["wallet"] for t in sells}),
        "buy_sell_ratio_30min":  buy_sell_r,
        "max_single_buy_30min":  max_buy,
        "avg_trade_size_30min":  avg_size,
        "top1_wallet_pct_30min": top1_pct,
        "top3_wallet_pct_30min": top3_pct,
        "trade_size_cv_30min":   trade_size_cv,
        "launch_hour_utc":       dt.hour,
        "launch_dayofweek":      dt.weekday(),
        "launch_month":          dt.month,
    }

    # Engineered features (same as training pipeline)
    buyers  = feats["unique_buyers_30min"]
    n_tr    = feats["n_trades_30min"]
    volume  = feats["volume_first_30min"]
    max_buy = feats["max_single_buy_30min"]

    feats["buyers_per_trade"]    = buyers / (n_tr + 1)
    feats["trades_per_buyer"]    = n_tr / (buyers + 1)
    feats["volume_per_buyer"]    = volume / (buyers + 1)
    feats["volume_per_trade"]    = volume / (n_tr + 1)
    feats["whale_dominance"]     = max_buy / (volume + 1)
    feats["buyer_seller_ratio"]  = buyers / (feats["unique_sellers_30min"] + 1)
    feats["buyers_per_volume"]   = buyers / (volume + 1)
    feats["trades_per_min"]      = n_tr / 30
    feats["volume_per_min"]      = volume / 30
    feats["log_trades"]          = math.log1p(n_tr)
    feats["log_buyers"]          = math.log1p(buyers)
    feats["log_volume"]          = math.log1p(volume)
    feats["log_max_buy"]         = math.log1p(max_buy)

    return feats

def passes_filters(feats: Dict) -> Tuple[bool, str, str]:
    """Returns (passed, short_code, detail_message)."""
    if feats["volume_first_30min"] < MIN_VOLUME_30MIN:
        return False, "low_volume",    f"volume={feats['volume_first_30min']:.0f}<{MIN_VOLUME_30MIN}"
    if feats["unique_buyers_30min"] < MIN_UNIQUE_BUYERS:
        return False, "low_buyers",    f"buyers={feats['unique_buyers_30min']}<{MIN_UNIQUE_BUYERS}"
    if feats["avg_trade_size_30min"] < MIN_AVG_TRADE_SIZE:
        return False, "low_avg_size",  f"avg_size={feats['avg_trade_size_30min']:.1f}<{MIN_AVG_TRADE_SIZE}"
    if feats["buy_sell_ratio_30min"] < MIN_BUY_SELL_RATIO:
        return False, "low_buy_sell",  f"buy_sell={feats['buy_sell_ratio_30min']:.2f}<{MIN_BUY_SELL_RATIO}"
    return True, "ok", "ok"

def calc_pnl(
    entry_price: float, current_price: float, size_usd: float
) -> Tuple[float, float]:
    pnl_pct = (current_price - entry_price) / entry_price
    pnl_usd = pnl_pct * size_usd
    return pnl_pct, pnl_usd

def compute_rule_signal_metrics(trades: List[Dict], signal_unique_buyers: int = RULE_SIGNAL_UNIQUE_BUYERS) -> Optional[Dict[str, float]]:
    buys = [t for t in trades if t["side"] == "buy" and t.get("wallet")]
    if not buys:
        return None

    first_buy_by_wallet: Dict[str, Dict] = {}
    for t in buys:
        w = t["wallet"]
        prev = first_buy_by_wallet.get(w)
        if prev is None or t["ts"] < prev["ts"]:
            first_buy_by_wallet[w] = t

    ordered = sorted(first_buy_by_wallet.values(), key=lambda x: x["ts"])
    if len(ordered) < signal_unique_buyers:
        return None

    signal_trade = ordered[signal_unique_buyers - 1]
    signal_time = signal_trade["ts"]

    to_signal = [t for t in trades if t["ts"] <= signal_time]
    total_vol = sum(float(t.get("usd", 0.0)) for t in to_signal)
    buy_vol = sum(float(t.get("usd", 0.0)) for t in to_signal if t.get("side") == "buy")
    buy_volume_share_to_signal = (buy_vol / total_vol) if total_vol > 0 else 0.0

    clean_post_signal_buys = [
        t for t in buys
        if t["ts"] >= signal_time and float(t.get("usd", 0.0)) >= RULE_MIN_CLEAN_TRADE_USD and float(t.get("price", 0.0)) > 0
    ]
    if not clean_post_signal_buys:
        return None

    entry_trade = sorted(clean_post_signal_buys, key=lambda x: x["ts"])[0]
    entry_price = float(entry_trade["price"])
    # `price` is stored as SOL/token, so convert the implied 1B-token market cap to USD
    # before comparing against USD-calibrated policy thresholds.
    entry_mcap_clean = entry_price * 1e9 * get_sol_price()

    minutes_to_signal = (signal_time - trades[0]["ts"]) / 60.0 if trades else 0.0
    return {
        "signal_time": signal_time,
        "buy_volume_share_to_signal": buy_volume_share_to_signal,
        "entry_trade_time": entry_trade["ts"],
        "entry_price": entry_price,
        "entry_mcap_clean": entry_mcap_clean,
        "minutes_to_signal": minutes_to_signal,
        "unique_buyers_at_signal": signal_unique_buyers,
    }
