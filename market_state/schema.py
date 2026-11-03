"""Original four-layer DuckDB DDL. Schema only; no populated private database."""

_DDL_RAW_TRADE_EVENTS = """
CREATE TABLE IF NOT EXISTS raw_trade_events (

    -- ── Identity ──────────────────────────────────────────────────────────
    row_id                      VARCHAR     PRIMARY KEY,

    -- ── Token ─────────────────────────────────────────────────────────────
    mint                        VARCHAR     NOT NULL,

    -- ── Transaction identity ──────────────────────────────────────────────
    -- PROVISIONAL NATURAL KEY — documented as best-available fallback.
    --
    -- The current bot parser (pumpfun_bot.py, parse_trade / _parse_trade_event)
    -- does NOT extract tx_signature or slot from the Helius logsSubscribe
    -- notification. Both fields exist in the WS payload but are not captured.
    --
    -- While tx_signature is unavailable, deduplication uses the composite key:
    --   (mint, block_time, wallet, is_buy, sol_amount_lamports)
    -- Using sol_amount_lamports (integer) rather than sol_amount_sol (float)
    -- avoids float-comparison ambiguity in the UNIQUE constraint.
    --
    -- MIGRATION PATH: when parse_trade() begins capturing tx_signature, add it
    -- to this table (ALTER TABLE ... ADD COLUMN tx_signature VARCHAR) and
    -- replace the UNIQUE constraint with UNIQUE(tx_signature). The provisional
    -- key can then be kept as a secondary index or dropped.
    tx_signature                VARCHAR,                    -- NULL until parser captures it
    slot                        BIGINT,                     -- NULL until parser captures it

    -- ── Timing ────────────────────────────────────────────────────────────
    -- block_time: from TradeEvent.timestamp (i64 unix seconds, on-chain).
    --   Fallback: if timestamp < 1_000_000_000, the bot uses time.time()
    --   (local wall clock). See pumpfun_bot.py:776.
    block_time                  TIMESTAMPTZ NOT NULL,
    observed_at                 TIMESTAMPTZ NOT NULL,       -- wall-clock at parse time

    -- ── Trader ────────────────────────────────────────────────────────────
    wallet                      VARCHAR     NOT NULL,       -- TradeEvent.user pubkey (base58)

    -- ── Direction ─────────────────────────────────────────────────────────
    -- is_buy comes from an EXPLICIT bool in the on-chain TradeEvent struct
    -- (byte offset 48 of the 105-byte Anchor event payload (original schema comment said 97)). It is NOT inferred
    -- from token flow, pool delta, or any heuristic. See pumpfun_bot.py:754.
    -- side is a human-readable alias derived solely from is_buy.
    is_buy                      BOOLEAN     NOT NULL,
    side                        VARCHAR     NOT NULL,       -- 'buy' | 'sell'

    -- ── SOL amounts ───────────────────────────────────────────────────────
    -- Canonical naming: _lamports suffix = raw integer from event (u64).
    --                   _sol suffix     = lamports / 1e9 (human-readable).
    -- sol_amount_lamports is the field used in the UNIQUE key (integer, no
    -- float precision issues).
    sol_amount_lamports         BIGINT      NOT NULL,
    sol_amount_sol              DOUBLE      NOT NULL,       -- sol_amount_lamports / 1e9

    -- ── Token amounts ─────────────────────────────────────────────────────
    -- Stored as raw u64 from the TradeEvent — decimals are NOT applied.
    -- To convert to human-readable: token_amount_raw / 10^TOKEN_DECIMALS
    -- (TOKEN_DECIMALS = 6 for pump.fun tokens; see offline_assumptions.py).
    -- Dune's dex_solana.trades already applies decimals; when joining,
    -- divide token_amount_raw by 1e6 before comparing.
    token_amount_raw            BIGINT      NOT NULL,

    -- ── Price (post-trade bonding curve spot) ─────────────────────────────
    -- Formula: (virt_sol_reserves_lamports / 1e9) / (virt_tok_reserves_raw / 1e6)
    -- This is the MARGINAL price at the new reserve state after the trade,
    -- not the average execution price. For large trades these can differ.
    price_sol_per_token         DOUBLE      NOT NULL,

    -- ── Virtual reserves (raw, post-trade) ────────────────────────────────
    virt_sol_reserves_lamports  BIGINT      NOT NULL,
    virt_tok_reserves_raw       BIGINT      NOT NULL,

    -- ── USD approximation (runtime, oracle-dependent) ─────────────────────
    -- amount_usd_runtime = sol_amount_sol * sol_usd_price_runtime
    -- sol_usd_price_runtime is from CoinGecko, cached up to 5 minutes.
    -- This is NOT equivalent to Dune's amount_usd (different oracle).
    amount_usd_runtime          DOUBLE      NOT NULL,
    sol_usd_price_runtime       DOUBLE      NOT NULL,

    -- ── Provenance ────────────────────────────────────────────────────────
    source_name                 VARCHAR     NOT NULL,       -- e.g. 'helius_ws_logsSubscribe'
    parser_version              VARCHAR     NOT NULL,       -- e.g. '1.0.0'
    event_parse_ok              BOOLEAN     NOT NULL,
    ingested_at                 TIMESTAMPTZ NOT NULL,

    -- ── Provisional dedup key (see PROVISIONAL NATURAL KEY note above) ────
    UNIQUE (mint, block_time, wallet, is_buy, sol_amount_lamports)
)
"""

_DDL_TOKEN_SNAPSHOT_30M = """
CREATE TABLE IF NOT EXISTS token_snapshot_30m (

    -- ── Identity ──────────────────────────────────────────────────────────
    -- row_id is referenced as FK by model_inference_log.snapshot_row_id.
    -- It uniquely identifies the exact feature snapshot seen by the model.
    row_id                          VARCHAR     PRIMARY KEY,

    -- ── Token ─────────────────────────────────────────────────────────────
    mint                            VARCHAR     NOT NULL,

    -- ── Window definition ─────────────────────────────────────────────────
    -- anchor_start_time is the timestamp of the FIRST OBSERVED TRADE for
    -- this mint, NOT the token creation time (CreateEvent is not processed
    -- by the current bot). anchor_type documents this explicitly.
    anchor_start_time               TIMESTAMPTZ NOT NULL,
    anchor_end_time                 TIMESTAMPTZ NOT NULL,   -- anchor_start_time + 30 min
    anchor_type                     VARCHAR     NOT NULL,   -- 'first_trade_observed' | 'legacy_log_stub'

    -- ── Calendar features (derived from anchor_start_time) ────────────────
    anchor_hour_utc                 INTEGER     NOT NULL,
    anchor_dayofweek                INTEGER     NOT NULL,   -- 1=Monday, 7=Sunday (isoweekday / ISO 8601)
    anchor_month                    INTEGER     NOT NULL,

    -- ── Trade counts ──────────────────────────────────────────────────────
    -- NULL in legacy_log_stub rows (no trade-level data available).
    n_buys_30m                      INTEGER,
    n_sells_30m                     INTEGER,
    n_trades_30m                    INTEGER,                -- n_buys + n_sells

    -- ── Unique participants ────────────────────────────────────────────────
    -- NULL in legacy_log_stub rows.
    unique_buyers_30m               INTEGER,                -- distinct wallets, is_buy=True
    unique_sellers_30m              INTEGER,                -- distinct wallets, is_buy=False

    -- ── Volume in SOL (oracle-free) ───────────────────────────────────────
    -- All volume metrics use sol_amount_sol to avoid oracle dependency.
    -- USD-denominated features can be derived offline by joining with
    -- the sol_usd_price_runtime from raw_trade_events if needed.
    -- NULL in legacy_log_stub rows.
    buy_volume_sol_30m              DOUBLE,
    sell_volume_sol_30m             DOUBLE,
    total_volume_sol_30m            DOUBLE,                 -- buy + sell

    -- ── Per-trade sizes ───────────────────────────────────────────────────
    -- NULL when the corresponding trade count is zero (no imputation here).
    -- Callers choose their own 0-imputation strategy for model input.
    avg_buy_size_sol_30m            DOUBLE,                 -- NULL if n_buys=0
    avg_sell_size_sol_30m           DOUBLE,                 -- NULL if n_sells=0
    avg_trade_size_sol_30m          DOUBLE,                 -- NULL if n_trades=0
    max_single_buy_sol_30m          DOUBLE,                 -- NULL in stub; 0.0 if n_buys=0 in real rows

    -- ── Wallet concentration (buy-side only) ──────────────────────────────
    -- Formula: max(wallet_buy_vol_sol) / sum(wallet_buy_vol_sol)
    -- over wallets with is_buy=True within [anchor_start, anchor_end).
    -- NULL if there is no buy volume (no buys in window).
    -- This definition matches the Dune SQL in queries 6841080/85/87.
    top1_wallet_pct_buy_sol_30m     DOUBLE,                 -- NULL if no buy volume

    -- ── Ratios (NULL when denominator is zero) ────────────────────────────
    -- buy_sell_trade_ratio_30m: count of buy trades / count of sell trades.
    --   Matches Dune SQL definition (COUNT ratio, not volume ratio).
    --   The current bot computes a VOLUME ratio — this is the corrected version.
    buy_sell_trade_ratio_30m        DOUBLE,                 -- NULL if n_sells=0
    -- buy_sell_volume_ratio_sol_30m: buy_volume_sol / sell_volume_sol.
    --   Explicit volume ratio kept separately from count ratio.
    buy_sell_volume_ratio_sol_30m   DOUBLE,                 -- NULL if sell_volume=0

    -- ── Edge-case flags ───────────────────────────────────────────────────
    -- NULL in legacy_log_stub rows.
    has_zero_sells_30m              BOOLEAN,
    has_zero_buy_volume_30m         BOOLEAN,

    -- ── Provenance ────────────────────────────────────────────────────────
    computed_at                     TIMESTAMPTZ NOT NULL,
    feature_schema_version          VARCHAR     NOT NULL,
    schema_version                  VARCHAR     NOT NULL,

    -- ── Dedup key ─────────────────────────────────────────────────────────
    -- One snapshot per mint × window-start × feature schema version.
    -- If features are recomputed with a new FEATURE_SCHEMA_VERSION, both
    -- rows coexist — allowing version comparison in audit queries.
    UNIQUE (mint, anchor_start_time, feature_schema_version)
)
"""

_DDL_MODEL_INFERENCE_LOG = """
CREATE TABLE IF NOT EXISTS model_inference_log (

    -- ── Identity ──────────────────────────────────────────────────────────
    row_id                  VARCHAR     PRIMARY KEY,

    -- ── Token ─────────────────────────────────────────────────────────────
    mint                    VARCHAR     NOT NULL,
    scored_at               TIMESTAMPTZ NOT NULL,

    -- ── Model identity ────────────────────────────────────────────────────
    model_name              VARCHAR     NOT NULL,           -- e.g. 'pumpfun_gb_model'
    model_version           VARCHAR     NOT NULL,           -- from artifact or 'unknown'
    feature_schema_version  VARCHAR     NOT NULL,

    -- ── Explicit snapshot reference ───────────────────────────────────────
    -- FK → token_snapshot_30m.row_id
    -- This is the EXACT snapshot row that was fed to the model.
    -- Storing this reference (not just mint + time) ensures full
    -- reproducibility: given a model_inference_log row you can join to
    -- token_snapshot_30m.row_id and reconstruct exactly what the model saw,
    -- even if multiple snapshots exist for the same mint (e.g. recomputed
    -- with a different feature_schema_version).
    snapshot_row_id         VARCHAR     NOT NULL,

    -- ── Feature vector ────────────────────────────────────────────────────
    -- Full dict of features passed to predict_proba(), serialized as JSON.
    -- Includes all imputed/filled values so the record is self-contained.
    feature_vector_json     VARCHAR     NOT NULL,

    -- ── Model output ──────────────────────────────────────────────────────
    score_raw               DOUBLE      NOT NULL,           -- predict_proba output
    score_calibrated        DOUBLE,                         -- NULL if no calibration layer

    -- ── Decision ──────────────────────────────────────────────────────────
    decision                VARCHAR     NOT NULL,           -- 'BUY' | 'CANDIDATE' | 'SKIP'
    decision_reason         VARCHAR     NOT NULL,
    threshold_used          DOUBLE,                         -- NULL when ranking-based
    ev_estimate             DOUBLE,                         -- NULL if not computed

    -- ── Filter result ─────────────────────────────────────────────────────
    passed_filters          BOOLEAN     NOT NULL,
    filter_failure_reason   VARCHAR,                        -- NULL if all filters passed

    -- ── Dedup key ─────────────────────────────────────────────────────────
    UNIQUE (mint, scored_at, model_version)
)
"""

_DDL_TOKEN_OUTCOME_LABELS = """
CREATE TABLE IF NOT EXISTS token_outcome_labels (

    -- ── Identity ──────────────────────────────────────────────────────────
    row_id                          VARCHAR     PRIMARY KEY,

    -- ── Token ─────────────────────────────────────────────────────────────
    mint                            VARCHAR     NOT NULL,

    -- ── Label provenance ──────────────────────────────────────────────────
    -- label_source: e.g. 'dune_query_6841080'
    -- dataset_month: YYYY-MM month this label covers, from the manifest.
    --   'unknown' permitted only for pre-migration legacy staging rows.
    --   All new ingests must supply a valid YYYY-MM value.
    -- label_env: 'production' | 'staging' | 'quarantined'
    --   production = passed all validation gates from a trusted manifest
    --   staging    = provisional or legacy ingest, not cleared for training
    --   quarantined = post-ingest correction; not used by the ingest path
    -- source_file: basename of the CSV file that produced this row
    -- manifest_sha256: sha256 of the manifest JSON at ingest time
    label_source                    VARCHAR     NOT NULL,
    label_query_id                  VARCHAR,                -- Dune query ID or NULL
    dataset_month                   VARCHAR     NOT NULL,   -- YYYY-MM or 'unknown' (legacy)
    label_env                       VARCHAR     NOT NULL,   -- 'production'|'staging'|'quarantined'
    source_file                     VARCHAR     NOT NULL,   -- CSV basename
    manifest_sha256                 VARCHAR     NOT NULL,   -- sha256 of manifest JSON

    -- ── Dust filter configuration ─────────────────────────────────────────
    -- Documents exactly which dust filter was applied when computing peak_mcap.
    -- Dune queries 6841080/85/87 use: token_amount >= 1.0 AND amount_usd >= 0.01
    -- Older queries used: token_amount >= 1000 AND amount_usd >= 100
    dust_filter_applied             BOOLEAN     NOT NULL,
    dust_filter_token_amount_min    DOUBLE,                 -- NULL if unknown
    dust_filter_amount_usd_min      DOUBLE,                 -- NULL if unknown

    -- ── Peak market cap (offline, full token lifetime) ────────────────────
    -- These fields are populated from Dune exports, not from runtime data.
    -- peak_mcap_usd_final: implied mcap = price_usd_at_peak * 1e9
    peak_mcap_usd_final             DOUBLE,                 -- NULL if not observed
    peak_mcap_trade_time_final      TIMESTAMPTZ,            -- NULL if not observed
    peak_mcap_amount_usd_final      DOUBLE,                 -- NULL if not observed
    -- Raw token amount at peak trade (u64 equivalent, pre-decimals).
    -- Dune provides human-readable; multiply by 1e6 to align with raw_trade_events.
    peak_mcap_token_amount_raw_final BIGINT,                -- NULL if not observed

    -- ── Label ─────────────────────────────────────────────────────────────
    graduated                       INTEGER     NOT NULL,   -- 0 or 1
    graduation_threshold_usd        DOUBLE      NOT NULL,   -- 69000.0

    -- ── Provenance ────────────────────────────────────────────────────────
    labeled_at                      TIMESTAMPTZ NOT NULL,

    -- ── Dedup key ─────────────────────────────────────────────────────────
    -- mint × label_source × label_env × dataset_month:
    -- Allows the same mint to appear in both staging and production, and in
    -- both Feb and Mar datasets under the same source without collision.
    UNIQUE (mint, label_source, label_env, dataset_month)
)
"""

_DDL_DATASET_VALIDATION_LOG = """
CREATE TABLE IF NOT EXISTS dataset_validation_log (

    -- ── Identity ──────────────────────────────────────────────────────────
    row_id                  VARCHAR     PRIMARY KEY,
    validated_at            TIMESTAMPTZ NOT NULL,

    -- ── Dataset identity ──────────────────────────────────────────────────
    source_file             VARCHAR     NOT NULL,
    manifest_path           VARCHAR,
    manifest_sha256         VARCHAR,
    dataset_month           VARCHAR,
    query_id                VARCHAR,

    -- ── Validation outcome ────────────────────────────────────────────────
    outcome                 VARCHAR     NOT NULL,   -- 'passed' | 'failed' | 'quarantined'
    strict_mode             BOOLEAN     NOT NULL,
    label_env_requested     VARCHAR     NOT NULL,

    -- ── Results ───────────────────────────────────────────────────────────
    failures                VARCHAR     NOT NULL,   -- JSON array, [] if passed
    warnings                VARCHAR     NOT NULL,   -- JSON array
    computed_row_count      INTEGER,
    computed_positive_rate  DOUBLE,

    -- ── Manifest snapshot ─────────────────────────────────────────────────
    -- Full manifest JSON at validation time — self-contained audit record.
    manifest_snapshot       VARCHAR
)
"""

_DDL_QUARANTINE_LOG = """
CREATE TABLE IF NOT EXISTS quarantine_log (

    -- ── Identity ──────────────────────────────────────────────────────────
    row_id              VARCHAR     PRIMARY KEY,
    quarantined_at      TIMESTAMPTZ NOT NULL,

    -- ── Link to full validation record ────────────────────────────────────
    validation_log_id   VARCHAR     NOT NULL,   -- FK → dataset_validation_log.row_id

    -- ── Summary ───────────────────────────────────────────────────────────
    source_file         VARCHAR     NOT NULL,
    failure_summary     VARCHAR     NOT NULL    -- first 3 failures, human-readable
)
"""

SCHEMA_STATEMENTS = (_DDL_RAW_TRADE_EVENTS, _DDL_TOKEN_SNAPSHOT_30M, _DDL_MODEL_INFERENCE_LOG, _DDL_TOKEN_OUTCOME_LABELS, _DDL_DATASET_VALIDATION_LOG, _DDL_QUARANTINE_LOG,)

def create_schema(connection):
    """Explicitly create the historical schema in a caller-supplied connection."""
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
