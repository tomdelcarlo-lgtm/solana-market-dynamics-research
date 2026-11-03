# Public data contracts

These files contain aggregate historical evidence, documented specifications and small synthetic examples. They are not a public trading dataset.

| File | Kind / source | Units / purpose |
|---|---|---|
| verified_aggregates.json | Sanitized export from completed reconstruction | Fractional returns, USD logged values, ms timings; historical label/phase counts |
| checkpoint_method_fixtures.csv | Eight hand-authored **synthetic** cases, not historical market observations | Buyers/counts, ratio, top-wallet buy share, USD volume/mean size; expected phase |
| policy_evolution.csv | Selected dated April policy specifications | USD market-cap intervals; buy-volume pressure share; nominal paper stake USD |
| research_timeline.json | Editorial condensation of documented chronology | Textual milestone dates; no numeric time scale |
| manifest.json | SHA256 input and retained-figure fingerprints | Reproduction integrity, not truth certification |
| expected_results.json | Deterministic output of the public script | Arithmetic checks, converted timings and synthetic normalized coordinates |

`buy_sell_ratio` in the checkpoint fixture is a ratio, while policy `pressure` intervals represent buy-volume share. They must not be interchanged. `top1_buy_share` is buy-side concentration. Blank fixture cells are intentional missing values. Blank paper stake cells in the policy table mean no value transcribed for that row, not a zero-dollar strategy.

The historical checkpoint totals and rates are aggregate facts, not results estimated from the eight synthetic cases. No artificial token histories, transaction records or timing samples have been created to reproduce historical counts.

Raw token/wallet identifiers, credentials, operational logs, proprietary endpoints, individual trades and original licensed exports are excluded. Aggregates retain limited historical periods for scientific context. [Provenance](../docs/evidence_and_provenance.md) documents the evidence boundary and [LICENSE_REVIEW.md](../LICENSE_REVIEW.md) describes the pending license choice.
