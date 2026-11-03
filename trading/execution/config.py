# Historical source publication note: algorithms/comments reflect the original experimental system.
# Live signing/submission is disabled; see docs/source_integration.md for packaging and safety changes.
"""
execution/config.py
────────────────────
Execution mode config loader.

Design rules:
- Default is always `paper`.
- Live config is explicit; it is never inferred from paper fields.
- Every live field must be set intentionally — missing fields fail loudly.
- No live RPC, wallet, or signing logic lives here yet (Phase 4).

Phase 4 additions:
- quote_max_age_ms: max acceptable quote age before QUOTE_STALE deny.
  0 (default) = unconfigured → fail-closed (all quotes denied as stale).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# ── Execution mode ────────────────────────────────────────────────────────────

class ExecutionMode(str, Enum):
    """The three supported execution modes.

    paper        — existing paper-trading behavior, no real capital (default)
    dry_run_live — full execution pipeline, no signing/sending (Phase 2+)
    micro_live   — real money, tiny amounts, requires explicit arm (Phase 4)
    """
    PAPER        = "paper"
    DRY_RUN_LIVE = "dry_run_live"
    MICRO_LIVE   = "micro_live"


def parse_execution_mode(raw: str) -> ExecutionMode:
    """Parse and validate TRADING_EXECUTION_MODE from env/config.

    Fails loudly on unrecognised values so misconfiguration is caught at
    startup rather than silently defaulting to an unintended mode.
    """
    normalised = raw.strip().lower()
    try:
        return ExecutionMode(normalised)
    except ValueError:
        valid = [m.value for m in ExecutionMode]
        raise ValueError(
            f"Unknown TRADING_EXECUTION_MODE={raw!r}. "
            f"Must be one of: {valid}"
        ) from None


# ── Live config ───────────────────────────────────────────────────────────────

@dataclass
class LiveConfig:
    """All fields required for live/dry-run execution.

    None of these fields are used in paper mode.
    Populated via environment variables (see config.example.toml).
    """
    # Risk caps
    position_size_usd: float        = 0.0
    position_size_overrides: Dict[str, float] = field(default_factory=dict)
    min_pressure_overrides: Dict[str, float] = field(default_factory=dict)
    entry_mcap_min_overrides: Dict[str, float] = field(default_factory=dict)
    entry_mcap_max_overrides: Dict[str, float] = field(default_factory=dict)
    max_open_positions: int         = 0
    max_notional_total_usd: float   = 0.0
    max_daily_loss_usd: float       = 0.0
    max_slippage_bps: int           = 0
    buy_6002_retry_enabled: bool    = False
    buy_6002_retry_count: int       = 0
    buy_6002_retry_shrink_factor: float = 1.0
    buy_6002_retry_min_size_usd: float = 0.0

    # Allowlisted policy tracks for live execution
    allowed_policy_tracks: List[str] = field(default_factory=list)

    # Operational gates
    require_preflight: bool         = True
    require_manual_arm: bool        = True
    allow_sells_only: bool          = False
    allow_low_lane_sunday: bool     = False

    # Connectivity / tx-builder config
    execution_backend: str          = "trade_api"
    rpc_url: Optional[str]          = None
    wallet_path: Optional[str]      = None
    trade_api_url: Optional[str]    = None
    trade_pool: str                 = "pump"
    priority_fee_sol: float         = 0.001
    quote_timeout_ms: int           = 3000
    order_timeout_ms: int           = 5000
    pumpfun_cli_repo_dir: Optional[str] = None
    pumpfun_cli_executable: Optional[str] = None
    pumpfun_cli_keyfile_path: Optional[str] = None
    pumpfun_cli_password_env: str   = "PUMPFUN_PASSWORD"
    pumpfun_cli_priority_fee_micro_lamports: Optional[int] = None
    pumpfun_native_loaded_accounts_limit_bytes: int = 16_384_000

    # Jito bundle routing for SELL transactions.
    jito_enabled: bool = False
    jito_tip_lamports: int = 50_000

    # Phase 4: quote freshness gate.
    # Maximum acceptable age (in ms) of a quote before it is treated as stale.
    # 0 = not configured → fail-closed: every micro_live order with a missing or
    # age-unverifiable quote is denied. Historical field retained; public live execution is disabled.
    quote_max_age_ms: int           = 0

    def position_size_for_track(self, track_name: str) -> float:
        """Return the configured live size for a policy track.

        Falls back to the global LIVE_POSITION_SIZE_USD when no per-track
        override is configured for the given track.
        """
        override = self.position_size_overrides.get((track_name or "").strip())
        if override is not None and override > 0:
            return override
        return self.position_size_usd

    def min_pressure_for_track(self, track_name: str) -> Optional[float]:
        """Return the optional minimum pressure gate for a track."""
        override = self.min_pressure_overrides.get((track_name or "").strip())
        if override is not None and override > 0:
            return override
        return None

    def entry_mcap_bounds_for_track(self, track_name: str) -> tuple[Optional[float], Optional[float]]:
        """Return optional (min,max) entry mcap bounds for a track."""
        name = (track_name or "").strip()
        lower = self.entry_mcap_min_overrides.get(name)
        upper = self.entry_mcap_max_overrides.get(name)
        lower = lower if lower is not None and lower > 0 else None
        upper = upper if upper is not None and upper > 0 else None
        return lower, upper


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float for {key}={raw!r}") from exc


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid int for {key}={raw!r}") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _env_track_float_map(key: str) -> Dict[str, float]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return {}

    mapping: Dict[str, float] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                f"Invalid {key} item {item!r}. Expected track:size pairs separated by commas"
            )
        track, value = item.split(":", 1)
        track = track.strip()
        if not track:
            raise ValueError(f"Invalid {key} item {item!r}. Track name is empty")
        try:
            size = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid float for {key} item {item!r}") from exc
        mapping[track] = size
    return mapping


def load_live_config() -> LiveConfig:
    """Load live config from environment variables.

    Called only when mode is not `paper`.
    """
    return LiveConfig(
        position_size_usd      = _env_float("LIVE_POSITION_SIZE_USD",      0.0),
        position_size_overrides= _env_track_float_map("LIVE_POSITION_SIZE_OVERRIDES"),
        min_pressure_overrides = _env_track_float_map("LIVE_MIN_PRESSURE_OVERRIDES"),
        entry_mcap_min_overrides = _env_track_float_map("LIVE_ENTRY_MCAP_MIN_OVERRIDES"),
        entry_mcap_max_overrides = _env_track_float_map("LIVE_ENTRY_MCAP_MAX_OVERRIDES"),
        max_open_positions     = _env_int(  "LIVE_MAX_OPEN_POSITIONS",       0),
        max_notional_total_usd = _env_float("LIVE_MAX_NOTIONAL_TOTAL_USD",  0.0),
        max_daily_loss_usd     = _env_float("LIVE_MAX_DAILY_LOSS_USD",      0.0),
        max_slippage_bps       = _env_int(  "LIVE_MAX_SLIPPAGE_BPS",         0),
        buy_6002_retry_enabled = _env_bool( "LIVE_BUY_6002_RETRY_ENABLED", False),
        buy_6002_retry_count   = _env_int(  "LIVE_BUY_6002_RETRY_COUNT",    0),
        buy_6002_retry_shrink_factor = _env_float("LIVE_BUY_6002_RETRY_SHRINK_FACTOR", 1.0),
        buy_6002_retry_min_size_usd = _env_float("LIVE_BUY_6002_RETRY_MIN_SIZE_USD", 0.0),
        allowed_policy_tracks  = _env_list( "LIVE_ALLOWED_POLICY_TRACKS"),
        require_preflight      = _env_bool( "LIVE_REQUIRE_PREFLIGHT",    True),
        require_manual_arm     = _env_bool( "LIVE_REQUIRE_MANUAL_ARM",   True),
        allow_sells_only       = _env_bool( "LIVE_ALLOW_SELLS_ONLY",     False),
        allow_low_lane_sunday  = _env_bool( "PARITY_ALLOW_LOW_LANE_SUNDAY", False),
        execution_backend      = (os.getenv("LIVE_EXECUTION_BACKEND", "trade_api").strip() or "trade_api").lower(),
        rpc_url                = os.getenv( "LIVE_RPC_URL"),
        wallet_path            = os.getenv( "LIVE_WALLET_PATH"),
        trade_api_url          = os.getenv( "LIVE_TRADE_API_URL"),
        trade_pool             = os.getenv( "LIVE_TRADE_POOL", "pump").strip() or "pump",
        priority_fee_sol       = _env_float("LIVE_PRIORITY_FEE_SOL",   0.001),
        quote_timeout_ms       = _env_int(  "LIVE_QUOTE_TIMEOUT_MS",     3000),
        order_timeout_ms       = _env_int(  "LIVE_ORDER_TIMEOUT_MS",     5000),
        pumpfun_cli_repo_dir   = os.getenv( "LIVE_PUMPFUN_CLI_REPO_DIR"),
        pumpfun_cli_executable = os.getenv( "LIVE_PUMPFUN_CLI_EXECUTABLE"),
        pumpfun_cli_keyfile_path = os.getenv( "LIVE_PUMPFUN_CLI_KEYFILE_PATH"),
        pumpfun_cli_password_env = (os.getenv("LIVE_PUMPFUN_CLI_PASSWORD_ENV", "PUMPFUN_PASSWORD").strip() or "PUMPFUN_PASSWORD"),
        pumpfun_cli_priority_fee_micro_lamports = (
            _env_int("LIVE_PUMPFUN_CLI_PRIORITY_FEE_MICRO_LAMPORTS", 0) or None
        ),
        pumpfun_native_loaded_accounts_limit_bytes = _env_int(
            "LIVE_PUMPFUN_NATIVE_LOADED_ACCOUNTS_LIMIT_BYTES",
            16_384_000,
        ),
        quote_max_age_ms       = _env_int(  "LIVE_QUOTE_MAX_AGE_MS",        0),
        jito_enabled           = _env_bool( "JITO_ENABLED",              False),
        jito_tip_lamports      = _env_int(  "JITO_TIP_LAMPORTS",        50_000),
    )


# ── Top-level execution config ────────────────────────────────────────────────

@dataclass
class ExecutionConfig:
    """Combined config for the execution layer.

    `mode` is always present; `live` is None in paper mode.
    `enable_runtime_llm` is a hard guardrail flag for external/runtime agents.
    The trading bot stays deterministic regardless.
    """
    mode: ExecutionMode
    live: Optional[LiveConfig] = None
    enable_runtime_llm: bool = False

    @property
    def is_paper(self) -> bool:
        return self.mode == ExecutionMode.PAPER

    @property
    def is_dry_run(self) -> bool:
        return self.mode == ExecutionMode.DRY_RUN_LIVE

    @property
    def is_live(self) -> bool:
        return self.mode == ExecutionMode.MICRO_LIVE


def load_execution_config() -> ExecutionConfig:
    """Load execution config from environment.

    TRADING_EXECUTION_MODE env var controls the mode.
    Defaults to `paper` so existing deployments are never changed.
    """
    raw_mode = os.getenv("TRADING_EXECUTION_MODE", ExecutionMode.PAPER.value)
    mode = parse_execution_mode(raw_mode)
    if mode == ExecutionMode.MICRO_LIVE:
        raise ValueError("micro_live is disabled in this public research release")

    live_cfg: Optional[LiveConfig] = None
    if mode != ExecutionMode.PAPER:
        live_cfg = load_live_config()

    return ExecutionConfig(
        mode=mode,
        live=live_cfg,
        enable_runtime_llm=_env_bool("ENABLE_RUNTIME_LLM", False),
    )
