# Methodology and metric definitions

This publication is a synthesis of a completed private forensic reconstruction. The release includes sanitized aggregates, a new offline method implementation and synthetic fixtures. It does not rerun historical trading or introduce new market observations.

## Units of analysis

| Artifact | Unit | Dependence / denominator |
|---|---|---|
| Training summary | One retained token-feature row | 32,000 rows, 583 labels; curated cohort |
| Materialized warehouse phase rates | One derived token-phase row in each cohort | Positives divided by each phase count; January/February differ |
| Checkpoint summary | Token at a checkpoint | 2,100 observations, 300 tokens, seven unequal checkpoints |
| Paper summary | SELL_ALL row | 3,360 raw rows, one duplicate ID; 3,359 under documented dedup rule |
| Live summary | Last filled BUY/SELL pair per ID | 188 completed pairs; unmatched exposure excluded |
| BUY diagnostics | Selected matched diagnostic row | 39; conditional on diagnostic coverage/completion |
| Public method fixture | Hand-authored synthetic case | Eight method demonstrations; no empirical inference |

Historical paper returns use fractional units: .07376569 means 7.376569%. Early export fields elsewhere sometimes used percentage points; those files are not ingested publicly. Mean return weights rows equally, while logged USD sums reflect variable sizes. Dollar profit factor is Σpositive USD / |Σnegative USD|; normalized-return PF instead uses fractional returns.

The live convention subtracts realized SELL USD from which realized BUY USD is deducted. Wallet balance deltas, contemporaneous SOL/USD quotes, fees/rent/refunds and fallback values complicate economic interpretation. The public fixture reproduces subtotal arithmetic only; it contains no raw transaction records.

Latency aggregates use milliseconds; public plotting divides by 1,000. Private reconstruction used linear-interpolated quantiles at (n−1)p. Raw timings are not released, so this code converts reported quantiles rather than recomputing them. Total p50 is not the sum of stage p50s.

The four-axis equations are in [chapter 3](../research/03_phase_space_and_dynamical_systems.md). The public wrapper rejects negative finite amounts/counts, invalid shares and infinity; missing values propagate into historical comparisons and are explicitly flagged. Floating-point comparisons have no added tolerance. The five-axis trapezoid helper demonstrates a proposal and is never fed into the checkpoint classification.

Timeline dates are documented milestones; they are not inferred from filesystem modification times. Policy table dates record specifications, not guaranteed deployment instants. Diagram spacing and card area carry no quantitative meaning unless an axis explicitly defines it.

Reproduction validates internal consistency and byte-identical figure generation. It cannot prove that withheld source data or prior extraction procedures are correct. The distinction is maintained in [provenance](evidence_and_provenance.md) and every figure caption.
