# Original engineering source integration

This release includes the actual historical system, not only research illustrations. Source was statically inspected and copied/extracted from the researcher's original implementation. No original project file was changed. No private configuration, event database, credential, signing material, raw log or notebook output was imported.

## Source map and execution status

| Public component | Retained implementation | Status |
|---|---|---|
| `collector/parser.py` | Original Anchor discriminators, little-endian field decoder, SOL/token decimal conversion and program-log parser | Operational offline; synthetic binary replay |
| `collector/migration_collector.py` | Original Migrate/MigrateV2 detector, pool-account parsing, reserve polling, retries, expiry and garbage collection | Historical read-only collector; explicit network opt-in and endpoints required; not run against services for this release |
| `market_state/reconstruction.py` | Original bounded signature/content dedup and per-token observed-trade reconstruction | Operational offline; policy/exit callbacks are explicit no-ops in this extraction |
| `market_state/tape.py` | Original event-tape, paper-decision and rule-observability CSV schemas/writers | Operational offline; writes only local runtime outputs |
| `market_state/schema.py` | Original normalized SQL DDL, including event/snapshot/inference/label tables and audit-support tables | Schema only; caller explicitly supplies a DB connection; no populated database bundled |
| `market_state/positions.py`, `trading/execution/state.py` | Original persisted position lifecycle, atomic replacement, pending-exit queue, integrity probing and recovery fields | Operational dry-run; atomic file replacement is not a concurrent-writer transaction |
| `signals/features.py` | Original 30m feature construction, smoothed ratios/log transforms, clean buyer-trigger anchor, filters and nominal PnL | Operational offline; fixed fixture SOL/USD quote, no learned model |
| `signals/checkpoint_phase.py` | Original pandas phase assignment, checkpoint summaries, transitions and archetypes | Explicit input/output CLI; historical token rows withheld |
| `signals/policies.py` | Exact final runtime track snapshot | Historical parameters, not the earlier April specification table or validated future policy |
| `signals/historical_bot.py` | Full original ranking, decision, entry, exit, time/age guards, monitoring loops and runtime wiring | Historical integration path; optional dependencies; paper default; network opt-in required; private data-layer bridge excluded |
| `signals/ml_experiments.py` | Original executed notebook cell bodies for split, metrics, SMOTE, XGB search, isotonic calibration and SHAP | Callable historical experiment code; optional dependencies; no training rows, outputs, serialized model or new fit |
| `trading/execution/adapter.py`, `trading/risk/` | Original intent/result types, first-failure risk ordering, dry-run adapter, journals and exit/retry handling | Actual dry-run path tested; paper default; micro-live rejected |
| `trading/execution/pumpfun_native.py` | Original account layouts, PDA/instruction construction, curve parsing, compute settings, retry/routing logic | Historical inspection source; wallet loading, signing/submission and send entrypoints hard-disabled |
| `trading/ops/live_control.py` | Original arm/safe-mode/control persistence | Historical flags only; cannot override hard execution blocks |
| `monitor/dashboard.py` | Original Streamlit dashboard, PnL normalization, funnel, performance and calibration views | Local read-only viewer over local runtime files; no private process probing or external font loading |
| `trading/execution/market_regime.py` | Original candidate-density/pressure market-regime instrumentation | Historical logging-only regime, distinct from token Cold/Warm/Hot |

`docs/source_manifest.json` records source aliases, original byte hashes and mappings. Aliases are descriptive filenames, not private filesystem locations. Hashes support local source identity; they do not make private originals downloadable.

## Documented publication changes

1. Imports now use coherent `trading.execution`, `trading.risk` and `trading.ops` package paths. Generated files go under `SOLANA_OUTPUT_DIR`, default `.runtime/`, which is ignored by Git.
2. Missing endpoints no longer terminate module import. Read-only networking requires `SOLANA_ALLOW_NETWORK=1` plus explicit `LIVE_RPC_URL` and, for WebSockets, `SOLANA_WS_URL`. No endpoint or actual credential is provided.
3. Provider-specific credential URL assembly and the private normalized-data bridge were removed. The actual retained CSV collection path remains. The scaffold schema is published independently of the excluded bridge/database.
4. Paper is the default. `micro_live` config loading is rejected. Live adapter dispatch, native sends, wallet loaders and signing/broadcast functions fail before side effects. Read-only RPC helpers deny transaction methods even when collection is opted in. No public command enables capital exposure.
5. Jito tip accounts, recipient configuration and automatic endpoint spraying were excluded. System/token/Pump protocol program identifiers remain where needed to explain parsing, PDA and instruction construction; they are not personal wallets or signing material. Historical send internals are archival, not a deployable execution service.
6. The original event field layout totals **105 bytes**, while its minimum-length check said 97. The public decoders reject truncated 97–104-byte inputs before unpacking. Field offsets and decimal formulas are retained.
7. The original forgiving position read could mask corruption during integrity checking. The public integrity check first probes the actual file; malformed roots and unreadable state are rejected. Atomic replacement is preserved; the documentation no longer promises multi-writer transactional safety.
8. Extracted scalar features use standard-library `math.log1p` instead of NumPy's scalar equivalent. Core demo/tests need no third-party packages. Historical full modules remain separately available with optional dependency manifests.
9. Replay callbacks are a deliberate extraction seam, not a replacement trading algorithm. The complete original decision/ranking/exit implementation remains in `signals/historical_bot.py`. Demo results are synthetic method exercises, not historical trading outcomes.
10. Historical source comments can contain exploratory interpretations or obsolete stage descriptions. Their retention is not endorsement of a validated edge. The article, claim register and limitations govern scientific conclusions.

## Scientific boundary

The central hypothesis remains that token states form trajectories and that similarity to graduation-bound trajectories might add information beyond a static snapshot. The source contains state features, trajectory-oriented instrumentation and policy proxies. It does **not** establish a validated trajectory-similarity predictor, incremental predictive improvement or deterministic chaos. No such result was added during integration.

## Exclusions and attribution

Excluded: raw events/transactions, individual trade records, funded configuration, personal wallet identities/signing files, API credentials, operational databases, original model pickle/joblib files, notebook outputs, session/authentication files, private automation/watchdogs, and third-party CLI source. No excluded file is needed for offline replay, dry-run execution or public figure reproduction.

The new CLI/fixture/safety/test glue is publication-specific. Original custom implementation is attributed as historical project source in the manifest. Streamlit, pandas, NumPy, scikit-learn, XGBoost, imbalanced-learn, SHAP, solders, base58, requests, websockets, joblib and DuckDB are third-party dependencies, not represented as authored here or vendored. The CLI shim describes an external integration; no external CLI implementation or license is copied into this repository.

## Reproduction checks for preserved algorithms

The full runtime and extracted feature functions agreed on every synthetic feature within 1e-12. The original pandas checkpoint code matched all eight public synthetic expected labels. Six original DDL tables were created in-memory with installed DuckDB. No original populated database or historical fit was executed. Core tests remain independent of those optional packages.
