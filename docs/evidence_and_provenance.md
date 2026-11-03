# Evidence and provenance

The source of this publication is the completed local investigation dated 2026-11-03. Original project files, personal identifiers, logs, populated databases, exact local paths and original Git history remain private. Selected custom implementation is now published as sanitized source, with mappings and hashes. This repository is an explicit publication subset, not a copy of that investigation.

## Evidence levels

| Level | Included evidence | What a reader can reproduce |
|---|---|---|
| Public aggregate | Label/phase counts, paper/live summaries, timing quantiles | Arithmetic, conversion, charts and internal consistency |
| Public specification | Four-axis formulas, selected policy table, milestone chronology | Method logic and specification visualizations |
| Synthetic example | Eight labeled checkpoint cases | Algorithm behavior, boundary/missingness tests; no market inference |
| Private historical source | Original notebooks, raw feature/event data, execution logs, code/Git | Findings are documented in the sanitized claim register; raw re-extraction is unavailable publicly |
| Interpretation | Trajectory motivation, competitive interpretation, opportunity cost | Reasoning can be examined, not converted into invented measurements |

[The historical claim register](claim_register.md) preserves E01–E32 and their verification status. “VERIFIED” describes the completed local reconstruction's evidence; it does not mean that private data is downloadable or that a hypothesis was scientifically validated. PARTIALLY VERIFIED metrics and UNVERIFIED interpretations retain their labels.

| Publication claim | Evidence IDs / source role |
|---|---|
| 32,000/583 and executed ML methods | E01–E07: training parquet count; notebook source and saved outputs |
| Two phase definitions; 2,100 labels; warehouse separation | E09–E12: geometry specification, checkpoint source/output, warehouse aggregate queries |
| Paper duplicate and live pair reconstruction | E13–E16: event filters and filled-leg matching on private CSVs |
| Local timing, price-deviation semantics and retry grain | E17–E20: selected timing CSV, adapter/report source and logs |
| Pause and continued collection | E21–E22: dated closeout and Git changes |
| No formal chaos conclusion | E23–E24: later stopped protocol and gate tables |
| Schema/ingest evidence boundary | E25–E27: read-only table counts, schema and ingest source |
| Guard/methodology corrections | E28–E29: documented source revisions |
| Competitive percentages qualitative | E32: no population evidence supporting the informal numbers |

## Policy specifications

`data/policy_evolution.csv` transcribes the selected April v5/v6/v6.1/v6.2/v8/v8.1/v8.2/v8.3 specifications summarized in the completed bands-and-thresholds investigation. The original dated policy documents supported the private ledger's P33 onward entries. Intervals are lower-inclusive/upper-exclusive. Core mcap is [$8,000,$9,000) in the selected families; blank stake cells are omitted values, not zero. Specification dates do not prove exact runtime deployment.

`data/research_timeline.json` is an editorial condensation of the completed chronology. The initial notebook date remains undated; early paper runtime is evidenced by March 2026. A later August package is retrospective and is not presented as methodology deployed during trading.

## Release lineage and fingerprints

`data/verified_aggregates.json` derives from the investigation's explicitly sanitized public aggregate export. A publication-scope field was added; no individual records were introduced. `data/manifest.json` records its source-export hash and fingerprints all input fixtures/specifications and the two retained PNGs. It contains no private paths. A hash identifies retained bytes; it does not validate source truth or licensing.

`docs/claim_register.md` comes from sanitized public evidence notes; its obsolete local analysis pointer was replaced with a source-role description. New offline code implements the documented math and rebuilds publication figures. The expanded release also includes sanitized original implementation with explicit module mappings and safety changes in [source integration](source_integration.md). The former aggregate plotting script's count/rate logic informed the new standard-library SVG renderer.

The retained PNGs are the previously checked phase association and selected BUY histogram. Their bytes are fingerprinted; only the new SVGs are regenerated here. Quantiles cannot reconstruct a histogram, and no pseudo-timing rows have been generated to imply otherwise.

This release deliberately preserves known limits rather than reopening the historical viability inquiry. The unresolved historical headline is documented without being made a release claim requiring a new reconciliation.
