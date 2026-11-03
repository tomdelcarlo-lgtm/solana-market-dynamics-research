# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
#!/usr/bin/env python3
"""
dashboard.py  —  pump.fun Paper Trading Bot · Live Monitor

Usage
─────
    pip3 install streamlit pandas
    streamlit run ./dashboard.py
"""

import csv
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from trading.safety import runtime_root

import pandas as pd
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT            = runtime_root()
LOG_PATH             = REPO_ROOT / "logs" / "paper_trading_log.csv"
LOG_PATH_V2          = REPO_ROOT / "logs" / "paper_trading_log_v2.csv"
REFRESH_SECONDS      = 60
SESSION_GAP_MINUTES  = 30
DEFAULT_POSITION_USD = 40.0
BANKROLL_START       = 2000.0

SELL_ACTIONS = {"SELL_ALL", "SELL_REST", "SELL_50_PCT"}

B_MEDIAN = 2.84
LOSS     = 0.85

# ── Known column schemas ──────────────────────────────────────────────────────
# 14-col original header (written when file was first created)
_BASE_COLS = [
    "timestamp", "token",
    "volume_first_30min", "unique_buyers", "avg_trade_size", "buy_sell_ratio",
    "passed_volume", "passed_buyers", "passed_avg_size", "passed_buy_sell",
    "all_filters_passed", "p_win", "action", "reason",
]
# 9 extra columns appended by the upgraded bot (positions 14–22)
_EXT_COLS = [
    "trade_id", "entry_price", "exit_price", "position_size_usd",
    "pnl_pct", "pnl_usd", "holding_time_sec",
    "ranking_position", "ranking_gap",
]
_FULL_COLS = _BASE_COLS + _EXT_COLS   # 23 columns total


# ── UI helpers ────────────────────────────────────────────────────────────────
def section_label(title: str, subtitle: str = "") -> None:
    st.subheader(title)
    if subtitle:
        st.caption(subtitle)


def divider() -> None:
    st.divider()


def dark_table(df_in: pd.DataFrame) -> None:
    st.dataframe(df_in, use_container_width=True, hide_index=True)


def bot_status_text(running: bool) -> str:
    return "● RUNNING" if running else "● STOPPED"


# ── Data helpers ──────────────────────────────────────────────────────────────
def safe_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def fmt_pct(v, na="n/a") -> str:
    try:
        f = float(v)
        return f"{f:+.2%}" if pd.notna(f) else na
    except Exception:
        return na


def fmt_usd(v, na="n/a") -> str:
    try:
        f = float(v)
        return f"${f:+.2f}" if pd.notna(f) else na
    except Exception:
        return na


def check_bot_running():
    return False, []  # Public monitor never probes external processes.
    try:
        result = subprocess.run(["pgrep", "-f", "pumpfun_bot.py"],
                                capture_output=True, text=True)
        pids = [p for p in result.stdout.strip().split() if p != str(os.getpid())]
        return bool(pids), pids
    except Exception:
        return False, []


# ── Data loading ──────────────────────────────────────────────────────────────
def _normalize_pnl_pct(raw: pd.Series) -> pd.Series:
    """
    Normalize pnl_pct to decimal form (0.50 = +50%).
    Rule:
      abs(raw) <= 5  → already decimal (e.g. -0.2494, 1.0449)
      abs(raw) >  5  → percentage points → divide by 100 (e.g. -32.95 → -0.3295)
    """
    raw = safe_num(raw)
    normalized = raw.copy()
    needs_div = raw.abs() > 5
    normalized[needs_div] = raw[needs_div] / 100.0
    return normalized


def load_log() -> pd.DataFrame:
    """
    Parses the CSV, handling BOTH the old 14-col schema and the new 23-col schema.

    Root cause of silent data loss: the bot upgraded and started appending 9 extra
    columns (trade_id … ranking_gap) to every row, but the file header was never
    updated.  pandas read_csv with on_bad_lines='skip' silently discards every
    23-col row as a 'bad' row.

    Fix: use csv.reader directly so we control row dispatch by column count,
    mapping each row to the correct column names regardless of row width.
    """
    if not LOG_PATH.exists():
        return pd.DataFrame()
    try:
        raw_rows = []
        with open(LOG_PATH, newline="", errors="replace") as fh:
            reader = csv.reader(fh)
            next(reader, None)          # skip stale 14-col header
            for parts in reader:
                n = len(parts)
                if n == 14:
                    raw_rows.append(dict(zip(_BASE_COLS, parts)))
                elif n >= 23:
                    raw_rows.append(dict(zip(_FULL_COLS, parts[:len(_FULL_COLS)])))
                # rows shorter than 14 or 15–22 cols are genuinely malformed → skip

        if not raw_rows:
            return pd.DataFrame()

        df = pd.DataFrame(raw_rows)
    except Exception:
        return pd.DataFrame()

    # ── Timestamp — always UTC-aware, drop unparseable ────────────────────────
    df["ts"] = pd.to_datetime(
        df.get("timestamp", pd.Series(dtype=str)), utc=True, errors="coerce"
    )
    df = df.dropna(subset=["ts"])

    # Fill remaining string columns with ""
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].fillna("")

    # ── Normalize action ──────────────────────────────────────────────────────
    if "action" in df.columns:
        df["action"] = df["action"].astype(str).str.upper().str.strip()

    # ── score / p_win backward compatibility ─────────────────────────────────
    if "score" in df.columns and "p_win" not in df.columns:
        df["p_win"] = df["score"]
    elif "p_win" in df.columns and "score" not in df.columns:
        df["score"] = df["p_win"]

    if "score" in df.columns:
        df["score"] = safe_num(df["score"])

    # ── EV — compute if missing ───────────────────────────────────────────────
    if "ev" not in df.columns and "score" in df.columns:
        df["ev"] = df["score"] * B_MEDIAN - (1 - df["score"]) * LOSS
    elif "ev" in df.columns:
        df["ev"] = safe_num(df["ev"])

    # ── pnl_pct normalization ─────────────────────────────────────────────────
    # Priority 1: stored pnl_pct from new rows (already decimal, e.g. -0.2494)
    # Priority 2: extract from reason field (e.g. "stop_loss pnl=-32.95%")
    extracted_raw = pd.Series(dtype=float, index=df.index)
    if "reason" in df.columns:
        extracted_str = df["reason"].astype(str).str.extract(r"pnl=([-\d.]+)%")[0]
        extracted_raw = pd.to_numeric(extracted_str, errors="coerce")

    if "pnl_pct" not in df.columns:
        df["pnl_pct"] = _normalize_pnl_pct(extracted_raw)
    else:
        df["pnl_pct"] = safe_num(df["pnl_pct"])
        df["pnl_pct"] = _normalize_pnl_pct(df["pnl_pct"])
        missing_mask = df["pnl_pct"].isna()
        df.loc[missing_mask, "pnl_pct"] = _normalize_pnl_pct(
            extracted_raw[missing_mask]
        )

    # ── pnl_usd ───────────────────────────────────────────────────────────────
    # New rows: bot already stored exact pnl_usd — use it as-is.
    # Old rows: fallback only for SELL_ALL (pnl_pct × DEFAULT_POSITION_USD).
    # SELL_50_PCT / SELL_REST on old rows: leave NaN (no safe approximation).
    if "pnl_usd" not in df.columns:
        df["pnl_usd"] = pd.NA

    df["pnl_usd"] = safe_num(df["pnl_usd"])

    sell_all_mask = df["action"] == "SELL_ALL"
    missing_usd   = df["pnl_usd"].isna()
    df.loc[sell_all_mask & missing_usd, "pnl_usd"] = (
        df.loc[sell_all_mask & missing_usd, "pnl_pct"] * DEFAULT_POSITION_USD
    )

    return df


def load_log_v2() -> pd.DataFrame:
    """Load paper_trading_log_v2.csv (proper header, flat schema)."""
    if not LOG_PATH_V2.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(LOG_PATH_V2, low_memory=False)
    except Exception:
        return pd.DataFrame()
    if "action" in df.columns:
        df["action"] = df["action"].astype(str).str.upper().str.strip()
    if "score" in df.columns:
        df["score"] = safe_num(df["score"])
    if "pnl_pct" in df.columns:
        df["pnl_pct"] = _normalize_pnl_pct(df["pnl_pct"])
    return df


def filter_by_window(df: pd.DataFrame, window: str) -> pd.DataFrame:
    if window == "All" or df.empty:
        return df
    if "ts" not in df.columns:
        df["ts"] = pd.NaT
    hours_map = {"30 min": 0.5, "1h": 1.0, "4h": 4.0, "12h": 12.0}
    now_utc = pd.Timestamp.now(tz="UTC")
    cutoff  = now_utc - pd.Timedelta(hours=hours_map[window])
    ts = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df[ts.notna() & (ts >= cutoff)].copy()


def _sell_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "action" not in df.columns:
        return pd.DataFrame()
    out = df[df["action"].isin(SELL_ACTIONS)].copy()
    for c in ["pnl_pct", "pnl_usd", "holding_time_sec"]:
        if c in out.columns:
            out[c] = safe_num(out[c])
    return out


def _buy_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "action" not in df.columns:
        return pd.DataFrame()
    out = df[df["action"] == "BUY"].copy()
    for c in ["score", "p_win", "ev", "position_size_usd"]:
        if c in out.columns:
            out[c] = safe_num(out[c])
    return out


# ── Open position tracker ─────────────────────────────────────────────────────
def compute_open_positions(df_raw: pd.DataFrame):
    """
    Returns (open_exposure_usd, open_count, is_exact).
    Per-token state machine: BUY opens, SELL_ALL closes,
    SELL_50_PCT halves remaining, SELL_REST closes remaining.
    is_exact=False if any partial sells exist (cost-basis ambiguous).

    IMPORTANT: filter to trade-action rows first. df_raw can have 60k+ unique
    tokens (mostly SEEN rows); iterating all groups was taking 9+ seconds and
    freezing the dashboard. Only ~58 tokens ever have BUY/SELL actions.
    """
    if df_raw.empty or "action" not in df_raw.columns or "token" not in df_raw.columns:
        return None, None, False

    _TRADE_ACTIONS = {"BUY", "SELL_ALL", "SELL_50_PCT", "SELL_REST"}
    trade_df = df_raw[df_raw["action"].isin(_TRADE_ACTIONS)].copy()

    if trade_df.empty:
        return 0.0, 0, True

    if "ts" in trade_df.columns:
        trade_df = trade_df.sort_values("ts")

    has_partials = trade_df["action"].isin({"SELL_50_PCT", "SELL_REST"}).any()
    open_usd   = 0.0
    open_count = 0

    for token, grp in trade_df.groupby("token", sort=False):
        remaining = 0.0
        for a in grp["action"]:
            if a == "BUY":
                remaining += DEFAULT_POSITION_USD
            elif a == "SELL_ALL":
                remaining = 0.0
            elif a == "SELL_50_PCT":
                remaining *= 0.5
            elif a == "SELL_REST":
                remaining = 0.0
        if remaining > 0:
            open_usd  += remaining
            open_count += 1

    return open_usd, open_count, not has_partials


# ── SECTION -1 — SYSTEM SNAPSHOT ─────────────────────────────────────────────
def render_system_snapshot(df_raw: pd.DataFrame) -> None:
    section_label("System Snapshot", "All-time counts from full log")

    if df_raw.empty or "action" not in df_raw.columns:
        st.info("No data available.")
        return

    tokens_seen = int((df_raw["action"] == "SEEN").sum())
    if tokens_seen == 0 and "token" in df_raw.columns:
        tokens_seen = int(df_raw["token"].nunique())

    candidates    = int((df_raw["action"] == "CANDIDATE").sum())
    buys          = int((df_raw["action"] == "BUY").sum())
    closed_trades = int(df_raw["action"].isin(SELL_ACTIONS).sum())
    conv_rate     = buys / tokens_seen if tokens_seen > 0 else float("nan")

    sells     = _sell_rows(df_raw)
    pnl_usd_s = (sells["pnl_usd"].dropna()
                 if (not sells.empty and "pnl_usd" in sells.columns)
                 else pd.Series(dtype=float))
    realized_pnl = float(pnl_usd_s.sum()) if len(pnl_usd_s) > 0 else 0.0
    open_exposure, _open_count, _is_exact = compute_open_positions(df_raw)

    if open_exposure is not None:
        cash_now         = BANKROLL_START + realized_pnl - open_exposure
        estimated_equity = cash_now + open_exposure
        cash_label       = fmt_usd(cash_now)
        equity_label     = fmt_usd(estimated_equity)
    else:
        cash_label   = "pending"
        equity_label = "pending"

    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    with c1:
        st.metric("Bankroll Start", f"${BANKROLL_START:,.0f}", delta_color="off")
    with c2:
        st.metric("Cash Now", cash_label, delta_color="off")
    with c3:
        st.metric("Est. Equity", equity_label, delta_color="off")
    with c4:
        st.metric("Tokens Seen", f"{tokens_seen:,}", delta_color="off")
    with c5:
        st.metric("Candidates", f"{candidates:,}", delta_color="off")
    with c6:
        st.metric("Buys", f"{buys:,}", delta_color="off")
    with c7:
        st.metric("Closed Trades", f"{closed_trades:,}", delta_color="off")
    with c8:
        st.metric("Conversion Rate",
                  f"{conv_rate:.4%}" if pd.notna(conv_rate) else "n/a",
                  delta="Buys / Seen", delta_color="off")


# ── SECTION 0 — BANKROLL SUMMARY ─────────────────────────────────────────────
def render_bankroll_summary(df_raw: pd.DataFrame) -> None:
    section_label("Bankroll Summary",
                  "All-time financials — uses full log, not time window")

    if df_raw.empty or "action" not in df_raw.columns:
        st.info("No data available.")
        return

    sells     = _sell_rows(df_raw)
    pnl_usd_s = (sells["pnl_usd"].dropna()
                 if (not sells.empty and "pnl_usd" in sells.columns)
                 else pd.Series(dtype=float))

    realized_pnl = float(pnl_usd_s.sum())          if len(pnl_usd_s) > 0 else 0.0
    total_gains  = float(pnl_usd_s[pnl_usd_s > 0].sum()) if len(pnl_usd_s) > 0 else 0.0
    total_losses = float(pnl_usd_s[pnl_usd_s < 0].sum()) if len(pnl_usd_s) > 0 else 0.0

    open_exposure, open_count, is_exact = compute_open_positions(df_raw)

    if open_exposure is not None:
        cash_now         = BANKROLL_START + realized_pnl - open_exposure
        estimated_equity = cash_now + open_exposure
        open_label       = fmt_usd(open_exposure) + ("" if is_exact else " (approx)")
        cash_label       = fmt_usd(cash_now)
        equity_label     = fmt_usd(estimated_equity)
    else:
        open_label   = "pending"
        cash_label   = "pending"
        equity_label = "pending"
        open_count   = None

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    with c1:
        st.metric("Bankroll Start", f"${BANKROLL_START:,.0f}", delta_color="off")
    with c2:
        st.metric("Realized PnL", fmt_usd(realized_pnl),
                  delta_color="normal" if realized_pnl >= 0 else "inverse")
    with c3:
        st.metric("Total Gains",
                  fmt_usd(total_gains) if total_gains else "$0.00",
                  delta_color="normal")
    with c4:
        st.metric("Total Losses",
                  fmt_usd(total_losses) if total_losses else "$0.00",
                  delta_color="inverse" if total_losses < 0 else "off")
    with c5:
        st.metric("Cash Now", cash_label, delta_color="off")
    with c6:
        oc_str = f"{open_count} positions" if open_count is not None else ""
        st.metric("Open Exposure", open_label,
                  delta=oc_str if oc_str else None, delta_color="off")
    with c7:
        st.metric("Est. Equity", equity_label, delta_color="off")


# ── FINANCIAL TRUTH TABLE ─────────────────────────────────────────────────────
def render_financial_truth(df_raw: pd.DataFrame) -> None:
    section_label("Financial Truth", "Compact audit — always rendered, uses df_raw")

    sells     = _sell_rows(df_raw)
    pnl_usd_s = (sells["pnl_usd"].dropna()
                 if (not sells.empty and "pnl_usd" in sells.columns)
                 else pd.Series(dtype=float))

    realized_pnl = float(pnl_usd_s.sum())                 if len(pnl_usd_s) > 0 else 0.0
    total_gains  = float(pnl_usd_s[pnl_usd_s > 0].sum())  if len(pnl_usd_s) > 0 else 0.0
    total_losses = float(pnl_usd_s[pnl_usd_s < 0].sum())  if len(pnl_usd_s) > 0 else 0.0

    open_exposure, open_count, is_exact = compute_open_positions(df_raw)

    has_exact_usd   = "pnl_usd" in sells.columns and sells["pnl_usd"].notna().any()
    pnl_usd_source  = "pnl_usd col (new rows)" if has_exact_usd else "pnl_pct × $40 (old rows)"
    realized_status = "exact" if has_exact_usd else "estimated"

    if open_exposure is not None:
        cash_now         = BANKROLL_START + realized_pnl - open_exposure
        estimated_equity = cash_now + open_exposure
        open_status      = "exact" if is_exact else "approx (partial sells present)"
        cash_status      = "estimated"
        equity_status    = "estimated"
    else:
        cash_now = estimated_equity = None
        open_status = equity_status = cash_status = "pending"

    rows = [
        ("Bankroll Start",  f"${BANKROLL_START:,.0f}",       "config",         "exact"),
        ("Realized PnL",    fmt_usd(realized_pnl),            pnl_usd_source,   realized_status),
        ("Total Gains",     fmt_usd(total_gains),             pnl_usd_source,   realized_status),
        ("Total Losses",    fmt_usd(total_losses),            pnl_usd_source,   realized_status),
        ("Cash Now",
         fmt_usd(cash_now) if cash_now is not None else "pending",
         "bankroll + realized − open_exposure",               cash_status),
        ("Open Exposure",
         fmt_usd(open_exposure) + f" ({open_count} pos)" if open_exposure is not None else "pending",
         "per-token state machine",                           open_status),
        ("Estimated Equity",
         fmt_usd(estimated_equity) if estimated_equity is not None else "pending",
         "cash + open_exposure",                              equity_status),
    ]

    dark_table(pd.DataFrame(rows, columns=["Metric", "Value", "Source", "Status"]))


# ── SECTION 1 — PERFORMANCE ───────────────────────────────────────────────────
def render_performance(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("Performance", "Are we making money?")

    buy_rows  = df[df["action"] == "BUY"].copy() if "action" in df.columns else pd.DataFrame()
    sell_rows = df[df["action"].str.startswith("SELL")].copy() if "action" in df.columns else pd.DataFrame()
    for c in ["score", "p_win", "ev"]:
        if c in buy_rows.columns:
            buy_rows[c] = safe_num(buy_rows[c])
    for c in ["pnl_pct", "pnl_usd"]:
        if c in sell_rows.columns:
            sell_rows[c] = safe_num(sell_rows[c])

    total_buys     = len(buy_rows)
    total_invested = total_buys * DEFAULT_POSITION_USD

    ev_series  = (buy_rows["ev"].dropna()
                  if (not buy_rows.empty and "ev" in buy_rows.columns)
                  else pd.Series(dtype=float))
    avg_ev_buy = float(ev_series.mean()) if len(ev_series) > 0 else float("nan")
    pct_pos_ev = float((ev_series > 0).mean() * 100) if len(ev_series) > 0 else float("nan")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total Invested", f"${total_invested:,.0f}",
                  delta=f"{total_buys} BUYs × ${DEFAULT_POSITION_USD:.0f}", delta_color="off")
    with c2:
        st.metric("Total Buys", str(total_buys), delta_color="off")
    with c3:
        st.metric("Avg EV (BUY)",
                  f"{avg_ev_buy:+.4f}" if pd.notna(avg_ev_buy) else "n/a",
                  delta="score×2.84 − (1−score)×0.85", delta_color="off")
    with c4:
        st.metric("% EV > 0",
                  f"{pct_pos_ev:.1f}%" if pd.notna(pct_pos_ev) else "n/a",
                  delta_color="off")

    pnl_s   = (sell_rows["pnl_pct"].dropna()
               if (not sell_rows.empty and "pnl_pct" in sell_rows.columns)
               else pd.Series(dtype=float))
    has_pnl = (not sell_rows.empty and "pnl_usd" in sell_rows.columns
               and sell_rows["pnl_usd"].notna().any())

    if has_pnl:
        total_pnl = float(sell_rows["pnl_usd"].sum())
        roi       = total_pnl / total_invested if total_invested > 0 else float("nan")
        win_rate  = float((pnl_s > 0).mean()) if len(pnl_s) > 0 else float("nan")
        avg_win   = float(pnl_s[pnl_s > 0].mean()) if (pnl_s > 0).any() else float("nan")
        avg_loss  = float(pnl_s[pnl_s <= 0].mean()) if (pnl_s <= 0).any() else float("nan")

        cp1, cp2, cp3, cp4 = st.columns(4)
        with cp1:
            st.metric("Total PnL", fmt_usd(total_pnl), delta="paper",
                      delta_color="normal" if total_pnl >= 0 else "inverse")
        with cp2:
            st.metric("ROI", fmt_pct(roi), delta="on invested",
                      delta_color="normal" if (pd.notna(roi) and roi >= 0) else "inverse")
        with cp3:
            st.metric("Win Rate",
                      f"{win_rate:.0%}" if pd.notna(win_rate) else "n/a",
                      delta=f"{int((pnl_s > 0).sum())}W / {int((pnl_s <= 0).sum())}L",
                      delta_color="off")
        with cp4:
            st.metric("Avg Win / Loss",
                      f"{fmt_pct(avg_win)} / {fmt_pct(avg_loss)}", delta_color="off")
    else:
        st.info("PnL: pending closed trades — no SELL rows with pnl data in this window.")


# ── SECTION 2 — SYSTEM EDGE ───────────────────────────────────────────────────
def render_system_edge(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("System Edge",
                  "Is the system selecting better than random baseline?")

    if "ev" not in df.columns or df["ev"].isna().all():
        st.info("No EV data yet — run bot to collect scores.")
        return

    buy_rows = df[df["action"] == "BUY"].copy() if "action" in df.columns else pd.DataFrame()
    if not buy_rows.empty and "ev" in buy_rows.columns:
        buy_rows["ev"] = safe_num(buy_rows["ev"])
    mean_ev_all = float(df["ev"].mean())
    buy_ev      = (buy_rows["ev"].dropna()
                   if (not buy_rows.empty and "ev" in buy_rows.columns)
                   else pd.Series(dtype=float))
    mean_ev_buy = float(buy_ev.mean()) if len(buy_ev) > 0 else float("nan")
    delta_ev    = (mean_ev_buy - mean_ev_all) if pd.notna(mean_ev_buy) else float("nan")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("EV — All Scored", f"{mean_ev_all:+.4f}",
                  delta="baseline (all rows)", delta_color="off")
    with c2:
        st.metric("EV — BUY Rows",
                  f"{mean_ev_buy:+.4f}" if pd.notna(mean_ev_buy) else "n/a",
                  delta="mean at decision time", delta_color="off")
    with c3:
        st.metric("Delta EV (BUY − All)",
                  f"{delta_ev:+.4f}" if pd.notna(delta_ev) else "n/a",
                  delta="positive = model selects above baseline",
                  delta_color="normal" if (pd.notna(delta_ev) and delta_ev > 0) else "inverse")


# ── SECTION 3 — CLOSED TRADES (uses df_raw) ───────────────────────────────────
def render_closed_trades(df_raw: pd.DataFrame) -> None:
    if len(df_raw) == 0:
        st.info("No data available.")
        return

    sell_rows = df_raw[df_raw["action"].str.startswith("SELL")].copy() if "action" in df_raw.columns else pd.DataFrame()
    for c in ["pnl_pct", "pnl_usd", "holding_time_sec"]:
        if c in sell_rows.columns:
            sell_rows[c] = safe_num(sell_rows[c])

    if len(sell_rows) == 0:
        st.info("No SELL rows in log.")
        return

    section_label("Closed Trades", "Uses full log (df_raw) — not filtered by time window")

    if "pnl_pct" not in sell_rows.columns:
        st.info("SELL rows exist but pnl_pct not available yet.")
        return

    pnl_s = sell_rows["pnl_pct"].dropna()

    total_closed = len(sell_rows)
    win_rate     = float((pnl_s > 0).mean()) if len(pnl_s) > 0 else float("nan")
    avg_pnl      = float(pnl_s.mean())        if len(pnl_s) > 0 else float("nan")

    pnl_usd_s    = (sell_rows["pnl_usd"].dropna()
                    if "pnl_usd" in sell_rows.columns
                    else pd.Series(dtype=float))
    total_real   = float(pnl_usd_s.sum())                    if len(pnl_usd_s) > 0 else float("nan")
    total_gains  = float(pnl_usd_s[pnl_usd_s > 0].sum())    if (pnl_usd_s > 0).any() else 0.0
    total_losses = float(pnl_usd_s[pnl_usd_s < 0].sum())    if (pnl_usd_s < 0).any() else 0.0

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Closed Trades", str(total_closed), delta_color="off")
    with c2:
        st.metric("Win Rate",
                  f"{win_rate:.0%}" if pd.notna(win_rate) else "n/a",
                  delta=f"{int((pnl_s > 0).sum())}W / {int((pnl_s <= 0).sum())}L",
                  delta_color="off")
    with c3:
        st.metric("Avg PnL %", fmt_pct(avg_pnl),
                  delta_color="normal" if pd.notna(avg_pnl) and avg_pnl >= 0 else "inverse")
    with c4:
        st.metric("Total Realized", fmt_usd(total_real),
                  delta_color="normal" if pd.notna(total_real) and total_real >= 0 else "inverse")
    with c5:
        st.metric("Total Gains", f"${total_gains:+.2f}", delta_color="normal")
    with c6:
        st.metric("Total Losses", f"${total_losses:+.2f}",
                  delta_color="inverse" if total_losses < 0 else "off")

    want = ["ts", "token", "action", "reason", "trade_id",
            "entry_price", "exit_price", "pnl_pct", "pnl_usd", "score", "ev"]
    cols = [c for c in want if c in sell_rows.columns]
    disp = sell_rows[cols].sort_values("ts", ascending=False).reset_index(drop=True)

    if "ts" in disp.columns:
        disp["ts"] = disp["ts"].dt.strftime("%m-%d %H:%M")
    if "pnl_pct" in disp.columns:
        disp["pnl_pct"] = disp["pnl_pct"].map(lambda x: fmt_pct(x) if pd.notna(x) else "")
    if "pnl_usd" in disp.columns:
        disp["pnl_usd"] = disp["pnl_usd"].map(lambda x: fmt_usd(x) if pd.notna(x) else "")
    if "score" in disp.columns:
        disp["score"] = safe_num(disp["score"]).map(
            lambda x: f"{x:.6f}" if pd.notna(x) else "")
    if "ev" in disp.columns:
        disp["ev"] = safe_num(disp["ev"]).map(
            lambda x: f"{x:+.4f}" if pd.notna(x) else "")

    dark_table(disp)


# ── SECTION 3b — EMPIRICAL SCORE CALIBRATION (research / observability only) ──
def render_empirical_calibration(df_v2: pd.DataFrame) -> None:
    section_label(
        "Empirical Score Calibration",
        "Historical mapping: score buckets → realized outcomes  (research only, not used by bot)",
    )

    MIN_CLOSED = 10  # need at least this many closed trades to bucket

    if df_v2.empty or "action" not in df_v2.columns:
        st.info("Not enough closed trades yet for empirical calibration.")
        st.caption("Research only. Historical mapping from score buckets to realized outcomes. "
                   "Not used by the live bot.")
        return

    # Keep only closed (SELL) trades that have both score and pnl_pct
    sells = df_v2[df_v2["action"].str.contains("SELL", na=False)].copy()
    if "score" not in sells.columns or "pnl_pct" not in sells.columns:
        st.info("Not enough closed trades yet for empirical calibration.")
        st.caption("Research only. Historical mapping from score buckets to realized outcomes. "
                   "Not used by the live bot.")
        return

    sells["score"]   = safe_num(sells["score"])
    sells["pnl_pct"] = safe_num(sells["pnl_pct"])
    sells = sells.dropna(subset=["score", "pnl_pct"])

    if len(sells) < MIN_CLOSED:
        st.info(
            f"Not enough closed trades yet for empirical calibration. "
            f"({len(sells)} with score+pnl_pct — need at least {MIN_CLOSED})"
        )
        st.caption("Research only. Historical mapping from score buckets to realized outcomes. "
                   "Not used by the live bot.")
        return

    sells["win"] = (sells["pnl_pct"] > 0).astype(int)

    try:
        sells["score_bin"] = pd.qcut(sells["score"], 5, duplicates="drop")
    except Exception:
        st.info("Not enough closed trades yet for empirical calibration (qcut failed).")
        st.caption("Research only. Historical mapping from score buckets to realized outcomes. "
                   "Not used by the live bot.")
        return

    def _avg_win(s):
        pos = s[s > 0]
        return pos.mean() if len(pos) > 0 else float("nan")

    def _avg_loss(s):
        neg = s[s <= 0]
        return neg.mean() if len(neg) > 0 else float("nan")

    grp = sells.groupby("score_bin", observed=True)
    cal = grp.agg(
        count    = ("pnl_pct", "count"),
        win_rate = ("win",     "mean"),
        avg_pnl  = ("pnl_pct", "mean"),
        median_pnl = ("pnl_pct", "median"),
        avg_win  = ("pnl_pct", _avg_win),
        avg_loss = ("pnl_pct", _avg_loss),
    ).reset_index()

    cal["ev_empirical"] = (
        cal["win_rate"] * cal["avg_win"]
        + (1 - cal["win_rate"]) * cal["avg_loss"]
    )

    # Format for display
    disp = cal.copy()
    disp["score_bin"]    = disp["score_bin"].astype(str)
    disp["win_rate"]     = disp["win_rate"].map(lambda x: f"{x:.0%}" if pd.notna(x) else "n/a")
    disp["avg_pnl"]      = disp["avg_pnl"].map(lambda x: fmt_pct(x))
    disp["median_pnl"]   = disp["median_pnl"].map(lambda x: fmt_pct(x))
    disp["avg_win"]      = disp["avg_win"].map(lambda x: fmt_pct(x))
    disp["avg_loss"]     = disp["avg_loss"].map(lambda x: fmt_pct(x))
    disp["ev_empirical"] = disp["ev_empirical"].map(lambda x: fmt_pct(x))

    dark_table(disp[["score_bin", "count", "win_rate", "avg_pnl",
                      "median_pnl", "avg_win", "avg_loss", "ev_empirical"]])

    # Simple bar chart: ev_empirical by score bucket
    chart_df = cal[["score_bin", "ev_empirical", "win_rate"]].copy()
    chart_df["score_bin"] = chart_df["score_bin"].astype(str)
    chart_df = chart_df.dropna(subset=["ev_empirical"])

    if not chart_df.empty:
        st.bar_chart(
            chart_df.set_index("score_bin")[["ev_empirical", "win_rate"]],
            use_container_width=True,
        )

    st.caption(
        "Research only. Historical mapping from score buckets to realized outcomes. "
        "Not used by the live bot."
    )


# ── SECTION 4 — SCORE BUCKET ANALYSIS ────────────────────────────────────────
def render_score_buckets(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("Score Bucket Analysis",
                  "BUY rows only — where does EV turn positive?")

    df_buy = df[df["action"] == "BUY"].copy() if "action" in df.columns else pd.DataFrame()

    if df_buy.empty:
        st.info("No BUY rows in this window.")
        return

    if "score" in df_buy.columns:
        df_buy["score"] = safe_num(df_buy["score"])
    if "ev" not in df_buy.columns or df_buy.get("ev", pd.Series()).isna().all():
        if "score" in df_buy.columns:
            df_buy["ev"] = df_buy["score"] * B_MEDIAN - (1 - df_buy["score"]) * LOSS
        else:
            st.info("No score/ev data on BUY rows yet.")
            return

    df_buy["ev"] = safe_num(df_buy["ev"])
    df_buy = df_buy.dropna(subset=["score", "ev"])

    if len(df_buy) < 10:
        st.info(f"Only {len(df_buy)} BUY rows with score data — need at least 10 to bucket.")
        return

    bins   = [0, 0.001, 0.01, 0.05, 0.1, 0.2, 1.0]
    labels = ["<0.001", "0.001–0.01", "0.01–0.05", "0.05–0.1", "0.1–0.2", ">0.2"]
    df_buy["bucket"] = pd.cut(df_buy["score"], bins=bins, labels=labels, right=False)

    grouped = (df_buy.groupby("bucket", observed=True)
               .agg(
                   trades=("ev", "count"),
                   mean_ev=("ev", "mean"),
                   pct_positive=("ev", lambda x: (x > 0).mean()),
               )
               .reset_index())
    grouped = grouped[grouped["trades"] > 0]

    if grouped.empty:
        st.info("No data after bucketing.")
        return

    display = grouped.copy()
    display["Mean EV"]    = display["mean_ev"].map(
        lambda x: f"{x:+.4f}" if pd.notna(x) else "n/a")
    display["% Positive"] = display["pct_positive"].map(
        lambda x: f"{x:.0%}" if pd.notna(x) else "n/a")
    display["Signal"]     = display["mean_ev"].map(
        lambda x: "POSITIVE" if (pd.notna(x) and x > 0) else "negative")
    dark_table(
        display.rename(columns={"bucket": "Score Bucket", "trades": "Trades"})
               [["Score Bucket", "Trades", "Mean EV", "% Positive", "Signal"]]
    )


# ── SECTION 5 — MARKET FUNNEL ─────────────────────────────────────────────────
def render_market_funnel(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("Market Funnel", "sequential — each stage ≤ previous")

    if "action" not in df.columns:
        st.info("No funnel data yet.")
        return

    # ── tokens_seen: SEEN action rows (not unique token count) ────────────────
    tokens_seen = int((df["action"] == "SEEN").sum())
    if tokens_seen == 0 and "token" in df.columns:
        tokens_seen = int(df["token"].nunique())   # fallback for older log formats

    # ── cumulative AND masks — each stage is a strict subset of the previous ──
    def _is_true(series: pd.Series) -> pd.Series:
        """True for 'True', '1', 'true' etc."""
        return series.astype(str).str.strip().str.lower().isin(["true", "1"])

    have_flags = any(c in df.columns for c in
                     ["passed_volume", "passed_buyers", "passed_avg_size", "passed_buy_sell"])

    if have_flags:
        pv_mask = (_is_true(df["passed_volume"])
                   if "passed_volume" in df.columns
                   else pd.Series(False, index=df.index))
        pb_mask = (pv_mask & _is_true(df["passed_buyers"])
                   if "passed_buyers" in df.columns
                   else pv_mask)
        pa_mask = (pb_mask & _is_true(df["passed_avg_size"])
                   if "passed_avg_size" in df.columns
                   else pb_mask)
        pr_mask = (pa_mask & _is_true(df["passed_buy_sell"])
                   if "passed_buy_sell" in df.columns
                   else pa_mask)

        pass_vol    = int(pv_mask.sum())
        pass_buyers = int(pb_mask.sum())
        pass_avg    = int(pa_mask.sum())
        pass_ratio  = int(pr_mask.sum())

        candidates = int((df["action"] == "CANDIDATE").sum())
        buys       = int((df["action"] == "BUY").sum())

        # Safety: clamp each stage to not exceed its predecessor
        pass_vol    = min(pass_vol,    tokens_seen)
        pass_buyers = min(pass_buyers, pass_vol)
        pass_avg    = min(pass_avg,    pass_buyers)
        pass_ratio  = min(pass_ratio,  pass_avg)
        candidates  = min(candidates,  pass_ratio)
        buys        = min(buys,        candidates)

        steps = [
            ("Tokens Seen",    tokens_seen, tokens_seen),
            ("Pass Volume",    pass_vol,    tokens_seen),
            ("Pass Buyers",    pass_buyers, pass_vol),
            ("Pass Avg Size",  pass_avg,    pass_buyers),
            ("Pass B/S Ratio", pass_ratio,  pass_avg),
            ("Candidates",     candidates,  pass_ratio),
            ("BUYs",           buys,        max(candidates, 1)),
        ]
    else:
        candidates = int((df["action"] == "CANDIDATE").sum())
        buys       = int((df["action"] == "BUY").sum())
        candidates = min(candidates, tokens_seen)
        buys       = min(buys, candidates)
        steps = [
            ("Tokens Seen", tokens_seen, tokens_seen),
            ("Candidates",  candidates,  tokens_seen),
            ("BUYs",        buys,        max(candidates, 1)),
        ]

    rows = []
    for lbl, cnt, prev in steps:
        if prev > 0:
            pct_str = f"{cnt/prev*100:.1f}%"
        else:
            pct_str = "—"
        rows.append({"Stage": lbl, "Count": cnt, "% of Previous": pct_str})

    tbl = pd.DataFrame(rows)
    st.dataframe(tbl, use_container_width=True, hide_index=True)


# ── SECTION 6 — FILTER ANALYSIS ───────────────────────────────────────────────
def render_filter_analysis(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("Filter Analysis", "why tokens are rejected")

    if "reason" not in df.columns:
        st.info("No filter data yet.")
        return

    reason_col = df["reason"].astype(str)

    filter_map = {
        "low_volume":   "Low Volume",
        "low_buyers":   "Low Buyers",
        "low_avg_size": "Low Avg Size",
        "low_buy_sell": "Low B/S Ratio",
    }

    rows = []
    for key, label in filter_map.items():
        count = int(reason_col.str.contains(key, case=False, regex=False).sum())
        rows.append({"Filter": label, "Fail Count": count})

    rows.sort(key=lambda r: r["Fail Count"], reverse=True)
    total_fails = sum(r["Fail Count"] for r in rows)

    if total_fails == 0:
        st.info("No filter failures found in this window.")
        return

    for r in rows:
        r["% of Total"] = (f"{r['Fail Count']/total_fails*100:.1f}%"
                           if total_fails > 0 else "—")

    st.dataframe(pd.DataFrame(rows)[["Filter", "Fail Count", "% of Total"]],
                 use_container_width=True, hide_index=True)


# ── SECTION 7 — SESSION TRACKING ─────────────────────────────────────────────
def render_sessions(df: pd.DataFrame) -> None:
    if len(df) == 0:
        st.info("No data available.")
        return

    section_label("Session Tracking", "gaps > 30 min = new session")

    if "ts" not in df.columns or df["ts"].isna().all():
        st.info("No session data yet.")
        return

    now_utc = pd.Timestamp.now(tz="UTC")
    all_ts  = df["ts"].dropna().sort_values().reset_index(drop=True)

    if all_ts.empty:
        st.info("No timestamps yet.")
        return

    total_secs    = int((all_ts.iloc[-1] - all_ts.iloc[0]).total_seconds())
    h, rem        = divmod(total_secs, 3600)
    gap           = all_ts.diff().dt.total_seconds().div(60)
    session_ids   = (gap > SESSION_GAP_MINUTES).cumsum()
    total_sessions = int(session_ids.max()) + 1

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Total Runtime", f"{h}h {rem // 60}m", delta_color="off")
    with col2:
        st.metric("Total Sessions", str(total_sessions), delta_color="off")

    rows    = []
    max_sid = session_ids.max()
    for sid, group in all_ts.groupby(session_ids):
        start     = group.iloc[0]
        end       = group.iloc[-1]
        is_active = (sid == max_sid
                     and (now_utc - end).total_seconds() / 60 < SESSION_GAP_MINUTES)
        mask      = df["ts"].notna() & (df["ts"] >= start) & (df["ts"] <= end)
        rows.append({
            "Session": f"#{int(sid) + 1}",
            "Start":   start.strftime("%Y-%m-%d %H:%M"),
            "End":     "NOW (active)" if is_active else end.strftime("%Y-%m-%d %H:%M"),
            "Tokens":  int(df.loc[mask, "token"].nunique()) if "token" in df.columns else 0,
            "Buys":    int((df.loc[mask, "action"] == "BUY").sum()) if "action" in df.columns else 0,
        })

    dark_table(pd.DataFrame(rows).iloc[::-1].reset_index(drop=True))


# ── CONFIG PANEL ──────────────────────────────────────────────────────────────
def render_config() -> None:
    if "show_config" not in st.session_state:
        st.session_state.show_config = False

    label = ("▼  BUILD CONFIGURATION  ·  click to collapse"
             if st.session_state.show_config
             else "▶  BUILD CONFIGURATION  ·  click to expand")
    if st.button(label, key="config_toggle"):
        st.session_state.show_config = not st.session_state.show_config
        st.rerun()

    if not st.session_state.show_config:
        return

    col1, col2 = st.columns(2)
    with col1:
        st.caption("MODEL")
        dark_table(pd.DataFrame([
            ("Type",    "XGBoost (raw classifier, ranking mode)"),
            ("Output",  "predict_proba()[1] — ranking score, not calibrated probability"),
            ("Features","23"),
            ("Data",    "32,000 tokens  (Jan 2025)"),
            ("Artifact","pumpfun_gb_model.joblib"),
        ], columns=["Parameter", "Value"]))

        st.caption("FILTERS")
        dark_table(pd.DataFrame([
            ("Min Volume 30m", ">= $2,000"),
            ("Min Buyers",     ">= 20"),
            ("Min Avg Trade",  ">= $20"),
            ("Min B/S Ratio",  ">= 0.8"),
        ], columns=["Filter", "Threshold"]))

    with col2:
        st.caption("RANKING ENGINE")
        dark_table(pd.DataFrame([
            ("Window",         "10 seconds"),
            ("Top N / window", "1"),
            ("Edge Ratio",     "5.0x  (top1 / top2)"),
            ("Min Score",      "1e-5"),
        ], columns=["Parameter", "Value"]))

        st.caption("DECISION POLICY")
        dark_table(pd.DataFrame([
            ("Strategy",   "ranking-based (top1/top2 >= 5x edge)"),
            ("Gate",       "relative conviction — no probability threshold"),
            ("EV logging", "observation only — never used in decisions"),
        ], columns=["Policy", "Detail"]))

        st.caption("PORTFOLIO")
        dark_table(pd.DataFrame([
            ("Bankroll",      "$2,000  (paper)"),
            ("Position size", "2%  =  $40"),
            ("Max positions", "5"),
            ("Stop loss",     "-30%"),
            ("Take profit 1", "+100%  -> sell 50%"),
            ("Take profit 2", "+200%  -> sell rest"),
            ("Time exit",     "90 min"),
        ], columns=["Parameter", "Value"]))

    st.warning("PAPER TRADING MODE — no real transactions")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="pump.fun · Live Monitor",
        page_icon="⬡",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.markdown("""
<style>

html, body, [data-testid="stAppViewContainer"],
[data-testid="stApp"], .main, .block-container {
    background-color: #0d0d1a !important;
    color: #d0d8e8 !important;
    font-family: 'IBM Plex Mono', monospace !important;
}

h1 { color: #ffffff !important; font-size: 1.6rem !important; }
h2 { color: #a0c4ff !important; font-size: 1.1rem !important; }
h3 { color: #a0c4ff !important; }

p, label, div {
    color: #d0d8e8 !important;
    font-family: 'IBM Plex Mono', monospace !important;
}

[data-testid="metric-container"] {
    background-color: #13132a !important;
    border: 1px solid #2a2a4a !important;
    border-radius: 6px !important;
    padding: 12px !important;
}

[data-testid="stMetricValue"] {
    color: #ffffff !important;
    font-size: 1.4rem !important;
}

[data-testid="stMetricLabel"] {
    color: #8899bb !important;
    font-size: 0.75rem !important;
    text-transform: uppercase !important;
}

[data-testid="stDataFrame"] {
    background-color: #13132a !important;
}

[data-testid="stAlert"] {
    background-color: #13132a !important;
    border-left: 3px solid #a0c4ff !important;
}

#MainMenu, footer, header {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────────────────────
    running, _pids = check_bot_running()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S  UTC")

    hdr_col, ctrl_col = st.columns([3, 1], gap="small")
    with hdr_col:
        st.title("pump.fun  Paper Trading Bot")
        st.caption(
            f"{bot_status_text(running)}  ·  "
            f"Last refresh: {now_str}  ·  "
            f"auto-refresh every {REFRESH_SECONDS}s"
        )
    with ctrl_col:
        window = st.radio(
            label="Time Window",
            options=["30 min", "1h", "4h", "12h", "All"],
            index=4,
            horizontal=False,
        )

    # ── Load full log (df_raw) ─────────────────────────────────────────────────
    df_raw = load_log()
    df_v2  = load_log_v2()   # v2 log — used for empirical calibration only
    if df_raw.empty:
        st.warning(f"Log file not found or empty: {LOG_PATH}\nStart pumpfun_bot.py first.")
        time.sleep(REFRESH_SECONDS)
        st.rerun()

    # ── Filtered view (df_window) — for per-window charts/tables ──────────────
    df_window = filter_by_window(df_raw, window)

    if df_window.empty and window != "All":
        st.warning(f"No data in selected time window ({window}).")

    st.caption(f"Rows in window: {len(df_window):,}  (total log: {len(df_raw):,})")

    # ── Debug info ────────────────────────────────────────────────────────────
    with st.expander("debug info", expanded=False):
        now_utc         = pd.Timestamp.now(tz="UTC")
        max_ts_raw      = df_raw["ts"].max()      if "ts" in df_raw.columns    else None
        max_ts_filtered = df_window["ts"].max()   if (not df_window.empty and "ts" in df_window.columns) else None
        buy_raw         = int((df_raw["action"] == "BUY").sum())  if "action" in df_raw.columns else 0
        sell_raw        = int(df_raw["action"].isin(SELL_ACTIONS).sum()) if "action" in df_raw.columns else 0

        st.write(f"**log path:** `{LOG_PATH}`")
        st.write(f"**now UTC:** {now_utc}")
        st.write(f"**max timestamp (raw):** {max_ts_raw}")
        st.write(f"**max timestamp (filtered / {window}):** {max_ts_filtered}")
        st.write(f"**row count raw:** {len(df_raw):,}")
        st.write(f"**row count filtered ({window}):** {len(df_window):,}")
        st.write(f"**BUY rows (raw):** {buy_raw}")
        st.write(f"**SELL rows (raw):** {sell_raw}")

        if max_ts_raw is not None:
            age_h = (now_utc - max_ts_raw).total_seconds() / 3600
            st.write(f"**log age:** {age_h:.1f}h since last entry")
            if window != "All":
                hours_map = {"30 min": 0.5, "1h": 1.0, "4h": 4.0, "12h": 12.0}
                window_h  = hours_map.get(window, 0)
                if age_h > window_h:
                    st.warning(
                        f"Log is {age_h:.1f}h old but window is {window_h}h — "
                        "all data predates the window. Switch to 'All'."
                    )

        # Schema drift note
        has_ext = any(c in df_raw.columns for c in _EXT_COLS)
        if has_ext:
            new_rows = df_raw["trade_id"].notna().sum() if "trade_id" in df_raw.columns else 0
            st.write(f"**schema:** mixed — {new_rows:,} new-schema rows (23-col) + "
                     f"{len(df_raw) - new_rows:,} old-schema rows (14-col) loaded via csv.reader")
        else:
            st.write("**schema:** old 14-col only — no trade_id column present")

    # ── -1: System Snapshot (df_raw) ──────────────────────────────────────────
    try:
        render_system_snapshot(df_raw)
    except Exception as exc:
        st.error(f"System Snapshot failed: {exc}")
    st.caption("Rendered: System Snapshot")
    divider()

    # ── 0: Bankroll Summary (df_raw) ──────────────────────────────────────────
    try:
        render_bankroll_summary(df_raw)
    except Exception as exc:
        st.error(f"Bankroll Summary failed: {exc}")
    st.caption("Rendered: Bankroll Summary")
    divider()

    # ── Financial Truth table (df_raw) ────────────────────────────────────────
    try:
        render_financial_truth(df_raw)
    except Exception as exc:
        st.error(f"Financial Truth failed: {exc}")
    st.caption("Rendered: Financial Truth")
    divider()

    # ── 1: Performance (df_window) ────────────────────────────────────────────
    try:
        render_performance(df_window)
    except Exception as exc:
        st.error(f"Performance failed: {exc}")
    st.caption("Rendered: Performance")
    divider()

    # ── 2: System Edge (df_window) ────────────────────────────────────────────
    try:
        render_system_edge(df_window)
    except Exception as exc:
        st.error(f"System Edge failed: {exc}")
    st.caption("Rendered: System Edge")
    divider()

    # ── 3: Closed Trades (df_raw — always full history) ───────────────────────
    try:
        render_closed_trades(df_raw)
    except Exception as exc:
        st.error(f"Closed Trades failed: {exc}")
    st.caption("Rendered: Closed Trades")
    divider()

    # ── 3b: Empirical Score Calibration (df_v2 — research only) ──────────────
    try:
        render_empirical_calibration(df_v2)
    except Exception as exc:
        st.error(f"Empirical Calibration failed: {exc}")
    st.caption("Rendered: Empirical Score Calibration")
    divider()

    # ── 4: Score Bucket Analysis (df_window) ──────────────────────────────────
    try:
        render_score_buckets(df_window)
    except Exception as exc:
        st.error(f"Score Buckets failed: {exc}")
    st.caption("Rendered: Score Buckets")
    divider()

    # ── 5 + 6: Funnel & Filter side by side (df_window) ──────────────────────
    col1, col2 = st.columns(2)
    with col1:
        try:
            render_market_funnel(df_window)
        except Exception as exc:
            st.error(f"Market Funnel failed: {exc}")
    with col2:
        try:
            render_filter_analysis(df_window)
        except Exception as exc:
            st.error(f"Filter Analysis failed: {exc}")
    st.caption("Rendered: Market Funnel + Filter Analysis")
    divider()

    # ── 7: Session Tracking (df_raw) ──────────────────────────────────────────
    try:
        render_sessions(df_raw)
    except Exception as exc:
        st.error(f"Session Tracking failed: {exc}")
    st.caption("Rendered: Session Tracking")
    divider()

    # ── Config ────────────────────────────────────────────────────────────────
    try:
        render_config()
    except Exception as exc:
        st.error(f"Config failed: {exc}")
    st.caption("Rendered: Config")

    # ── Footer ────────────────────────────────────────────────────────────────
    st.caption(
        f"pump.fun monitor  ·  paper only  ·  no real transactions  ·  {now_str}"
    )

    time.sleep(REFRESH_SECONDS)
    st.rerun()


if __name__ == "__main__":
    main()
