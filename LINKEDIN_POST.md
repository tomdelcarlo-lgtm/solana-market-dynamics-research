# LinkedIn introduction

A token-graduation classifier and an executable Solana trade ask different questions. Building a system around both made that distinction concrete.

My research began with 32,000 early-token observations and 583 graduation-proxy positives. I explored Logistic Regression, boosting, SMOTE, average precision and SHAP, then moved toward a different representation: tokens as trajectories through participation, buying pressure, concentration, activity and operability.

That change in hypothesis changed the software. Monthly aggregates were insufficient, so I built event collection, per-token state, persistent market tapes and decision observability. Market-cap bands, pressure floors, exits and risk limits evolved—and some allocation decisions were later reversed.

The case study includes concrete boundaries: 2,100 checkpoint phase classifications reproduced, while deterministic chaos was not established. In 39 selected BUY diagnostics, median signal-to-local-confirmation timing was 3.721 seconds. That measures one execution path, not complete end-to-end market latency.

I paused trading exposure when further validation and infrastructure work no longer appeared worth the capital and attention I was prepared to commit. Collection continued for a narrower research option.

I've documented the mathematics, architecture, iterations and negative findings in a technical case study, with offline code, sanitized aggregates, reproducible figures and tests. It is a record of how the research developed and what its evidence supports.

**Case study and repository:** https://github.com/tomdelcarlo-lgtm/solana-market-dynamics-research

I'd welcome discussion with people working on Solana execution, market-state modeling and quantitative research—especially the observation and measurement choices that make those systems possible to evaluate.
