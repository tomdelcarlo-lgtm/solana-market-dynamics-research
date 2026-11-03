# Figure guide

All SVGs are self-contained, with accessible title/description text and no external assets. `python3 src/reproduce.py` generates them using the standard library. Figures distinguish empirical summaries from documented specifications and conceptual representations.

| Figure | Type | Source / calculation and inference limit |
|---|---|---|
| [Research timeline](research_timeline.svg) | Documented history | Timeline JSON; categorical spacing, initial start undated |
| [ML pipeline](ml_pipeline.svg) | Conceptual, source-grounded | Historical methods/evaluation boundaries; no model benchmark refit |
| [Phase-space definitions](phase_space_geometry.svg) | Conceptual mathematical | Four-axis implementation versus five-axis proposal; no invented trajectory |
| [Checkpoint classification](checkpoint_classification.svg) | Historical aggregates + rule | Count bars divide by 2,100; repeated observations, not independent tokens |
| [Threshold evolution](threshold_evolution.svg) | Documented specifications | April policy CSV; active paper lanes, no causal improvement claim |
| [Collection architecture](data_collection_architecture.svg) | Implemented/incomplete architecture | Dashed normalized branch; no guaranteed complete ingestion |
| [Execution pipeline](execution_pipeline.svg) | Historical architecture | Stages and boundaries; original source retained with live execution disabled |
| [Paper/live outcomes](paper_live_outcomes.svg) | Separate reported aggregates | Mean/median fractions ×100; unmatched populations, no execution-tax estimate |
| [Latency diagnostics](latency_diagnostics.svg) | Reported measured quantiles | Source ms /1000; selected n=39; confirmation is not inclusion |
| [Research decision](research_decision.svg) | Conceptual framework | Documented pause plus qualitative opportunity-cost interpretation; no invented ROI |
| [Phase association](phase_outcome_association.svg) | Verified aggregate association | Positives/count per phase; asymmetric cohorts; no chaos or return inference |
| [Retained histogram](retained_latency_distribution.png) | Previously verified historical figure | Actual selected 39-row diagnostic histogram; raw rows absent, not regenerated |
| [Retained phase figure](retained_phase_outcome_association.png) | Previously verified historical figure | Existing count/rate chart, retained and fingerprinted; new SVG is reproducible |

No chart claims a reconstructed attractor, causal execution decomposition or an out-of-sample trading advantage. Missing empirical series are represented by labeled conceptual diagrams rather than synthetic market observations.

## Published component map

[system_components.svg](system_components.svg) maps the actual imported collector/state/signal/trading/monitor packages and research boundary. Regenerate with `python3 -S -m monitor.architecture_figure`. It is an implementation diagram, not new market observations.
