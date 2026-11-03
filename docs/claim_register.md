# Historical claim register

Sanitized source aliases. Underlying private data is not distributed; aggregate arithmetic is locally reproducible with retained inputs. These notes do not substitute for an independently downloadable dataset.

## E01
32,000 rows; 583 positive labels (1.821875%). **VERIFIED**.
Version: January analytical cohort. Period: January 2025 cohort; extraction date unverified. Method: Independent parquet row count and label sum; the historical training-label reconstruction.

## E02
Original split was random stratified 80/20, seed 1; 25,600/6,400. **VERIFIED**.
Version: V1 notebook. Period: Research date not embedded reliably. Method: Static source and persisted output.

## E03
LR and XGBoost executed; saved baseline AP 0.526 and 0.782. **PARTIALLY VERIFIED**.
Version: V1 notebook. Period: Undated cells. Method: Saved outputs; not refitted; AP called PR-AUC in notebook.

## E04
SMOTE actually resampled training rows and models were refit. **VERIFIED**.
Version: V1 notebook. Period: Undated cells. Method: Inspect fit_resample call and output class balance 50/50.

## E05
SHAP TreeExplainer and plots were executed. **VERIFIED**.
Version: V1 notebook. Period: Undated cells. Method: Code plus saved figure outputs; explanatory values not recomputed.

## E06
Threshold search reused y_test; tuned XGB random search cv=3; isotonic fit on SMOTE data. **VERIFIED**.
Version: V1 notebook. Period: Undated cells. Method: Static call/argument inspection; no clean final holdout identified.

## E07
Graduation is a historical approximately $69k proxy, not an executable-return label. **VERIFIED**.
Version: V1. Period: Historical definition. Method: Compare target documentation with trading objective.

## E08
README has inconsistent estimator/feature descriptions and an anachronistic April 2025 fix target. **VERIFIED**.
Version: Early README. Period: File date is not research date. Method: Compare notebook actual 23-feature set, README tables and temporal script.

## E09
Geometric five-axis membership and operability trapezoid are a calibration-pending specification. **VERIFIED**.
Version: Geometric v0. Period: Undated specification. Method: Read formulas, weights, alternative rules; distinguish proposal from runtime.

## E10
Seven-checkpoint implementation uses four normalized axes, hard boxes; 2,100 rows, 300 tokens, zero reproduced phase mismatches. **VERIFIED**.
Version: Checkpoint v0. Period: Historical sample; launch provenance unresolved. Method: Independent formula evaluation versus 02_phase_assignment.csv.

## E11
Warehouse Jan/Feb Cold/Warm/Hot graduation separation reproduces. **VERIFIED**.
Version: Warehouse snapshot. Period: 2026-04-07 memo; Jan/Feb cohorts. Method: Read-only SQL count,sum,avg(graduated).

## E12
Warehouse phase data is imported materialized research, not recomputed from raw events by warehouse builder. **VERIFIED**.
Version: Warehouse builder. Period: Build-date lineage. Method: Inspect CSV import and gold join.

## E13
3,360 paper SELL_ALL rows include one duplicate trade ID; 3,359 distinct IDs. **VERIFIED**.
Version: Paper policy v4–v8. Period: 2026-04-07–2026-05-19 UTC. Method: Streaming row count, ID grouping; preserve raw and last-exit-per-ID calculations.

## E14
Raw paper exit mean +7.3766%, median -0.3814%, win rate 49.6429%; nominal size $150–$500. **VERIFIED**.
Version: Pooled policy sample. Period: 2026-04-07–2026-05-19 UTC. Method: Arithmetic mean, median, positive fraction; NOT net realizable profit.

## E15
188 filled live pairs; logged proceeds-minus-cost -$166.9017; USD PF 0.55665. **VERIFIED**.
Version: micro_live. Period: 2026-04-17–2026-06-11 UTC. Method: Independent join; no multiple filled legs; two buys remain unmatched.

## E16
Historical 173–174 trades, 51 days, -$148.63 and PF .394 are documented but not reproduced. **PARTIALLY VERIFIED**.
Version: Historical audit. Period: Through June 8 claimed; internal revisions. Method: Compare audit against independent June 8 cutoff: n=172, -$134.1249, USD PF .59361.

## E17
BUY diagnostic signal-to-local-confirmation p50 3,720.8ms, p90 4,512.04ms, p99 22,512.43ms; n=39. **VERIFIED**.
Version: Native execution diagnostic. Period: 2026-06-01–2026-06-11 UTC. Method: Linear interpolated quantiles; confirm_timestamp endpoint, not block inclusion.

## E18
Logged slippage proxy is absolute price deviation; not purely adverse execution cost. **VERIFIED**.
Version: Current adapter. Period: Source snapshot at audit. Method: Inspect abs and wallet balance delta valuation.

## E19
5,079 failed SELL rows are retry/event records, not 5,079 failed trades. **VERIFIED**.
Version: micro_live. Period: Full file through June 11. Method: Count rows; separate 188 completed pairs from repeated attempts.

## E20
Jito 0/38 is an audit assertion; June 9 disabling is in Git. **PARTIALLY VERIFIED**.
Version: Jito experiment. Period: 2026-06-04–09. Method: Static implementation/history; no provider-side receipt audit.

## E21
Trading gate closed June 11; safe_mode decision preserved data collection. **VERIFIED**.
Version: Execution gate. Period: 2026-06-11. Method: Document plus watchdog change; not a present-process assertion.

## E22
Post-migration collector and July 15 checkpoint followed the trading pause. **VERIFIED**.
Version: Migration Fase B. Period: 2026-06-11–14. Method: Implementation and dated commit history; later checkpoint outcome not established.

## E23
Formal chaos analysis halted; no reliable D2/Lyapunov/IAAFT result. **VERIFIED**.
Version: Later Stage 3 retrospective research. Period: Frozen seed 20260821; distinct from live research. Method: Read manifest, methods, blocker record; recount gate table.

## E24
Gate table has 12,429 rows; 5,788 pass ADF/KPSS component; all full stationarity classifications blank. **VERIFIED**.
Version: Stage 3. Period: Retrospective package. Method: Independent CSV count; statistical tests not rerun.

## E25
Data-layer database has 0 raw events, 0 snapshots, 0 inference rows, 753,262 outcome labels. **VERIFIED**.
Version: Data-layer v2. Period: Current read-only snapshot. Method: SELECT count(*) in read_only connection; separate audit DB not opened.

## E26
Early schema distinguishes block_time and observed_at; ingestion supplied NULL signature/slot. **VERIFIED**.
Version: Data-layer v2. Period: Historical source. Method: DDL and ingest mapping; not proof of populated timing coverage.

## E27
Runtime signature dedupe is bounded and session-local; content fallback can collide. **VERIFIED**.
Version: Runtime current source. Period: Audited snapshot. Method: Static source; collision and multi-event transaction risk are interpretations.

## E28
June 10 entry-premium guard had a corrected 1000x unit bug. **VERIFIED**.
Version: Live guard. Period: 2026-06-10. Method: Git commit plus subsequent GUARD_DATA_ERROR changes.

## E29
June 10 audit corrected synthetic-latency trigger methodology. **VERIFIED**.
Version: Retrospective simulator. Period: 2026-06-10. Method: Treat prior 49,825-signal estimates as superseded/sensitivity analyses, not measured edge.

## E30
Expense ledger line items sum $1,133; displayed total $838; deposits $95 are not expenses. **VERIFIED**.
Version: Cost ledger. Period: April–May labels, year absent in cells. Method: Parse XML values including dollar strings; exclude B5/B23 ($95): service subtotal $1,038.

## E31
Live repository available history starts May 26, not April 14. **VERIFIED**.
Version: Live repository. Period: 2026-05-26–2026-06-14. Method: git log --reverse; source/log dates predate version history.

## E32
Market-wide 90/8/1 profitability percentages and MEV attribution are unverified. **UNVERIFIED**.
Version: Researcher interpretation. Period: Retrospective. Method: Do not transform qualitative competitive interpretation into statistics.
