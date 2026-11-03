# Offline reproducibility

Requires Python **3.9+**, standard library only. No installation or network is required, even for figures. Run from the repository root:

```sh
python3 src/reproduce.py --check
python3 -m unittest discover -s tests -v
```

The first command verifies input SHA256 fingerprints, computes aggregate arithmetic and synthetic classifications, and compares all eleven SVGs and `data/expected_results.json` byte for byte. It does not write. The tests check method boundaries/missingness, corrupted counts, cohort arithmetic, input integrity, SVG parsing/determinism, local links, public-content patterns and absence of operational imports. One computation test explicitly disables socket connections.

To intentionally rebuild generated outputs after reviewing an editorial or mathematical change:

```sh
python3 src/reproduce.py
python3 src/reproduce.py --check
```

Paths derive from the script location, so the script also works from another working directory. Reproduction writes only `data/expected_results.json` and eleven SVG files under `figures/`. It never accesses the historical project directories. Python may create ignored bytecode caches; set `PYTHONDONTWRITEBYTECODE=1` to suppress them.

## What is reproduced

| Result | Public reproduction | Historical raw-source status |
|---|---|---|
| Label prevalence | 583/32,000 = .01821875 | Training rows private; no model fit |
| Phase graduation rates | Six positives/count calculations | Warehouse materialization private |
| Checkpoint count sum | 792+1,214+94 = 2,100 | Actual 2,100 classification check performed privately |
| Four-axis method | Eight hand-authored synthetic cases, with quality flags | Not a replay of 300 historical token paths |
| Paper ID arithmetic | 3,360−1 = 3,359 | Historical duplicate detection private |
| Live subtotal arithmetic | 209.5518−376.4535 = −166.9017 | Filled-leg join and underlying cashflows private |
| BUY timing | Convert reported ms quantiles to seconds | Percentiles/histogram cannot be refitted from absent timing rows |
| Policy/timeline visuals | Exact selected specifications and editorial milestones | Source chronology established in completed investigation |
| SVG figures | All eleven deterministic outputs | Conceptual diagrams clearly labeled |

Expected check output begins `PASS: input hashes, expected arithmetic, 8 synthetic cases, 11 SVGs`. The committed expected-results file is an executable release contract, not independent evidence of the original data extraction. Regeneration is intentionally separate from checking so changed results are not silently accepted by a test run.

## Data contracts

See [data documentation](../data/README.md). Returns are fractions, timing aggregates milliseconds, policy caps USD, and synthetic fields explicitly named by units. Fixtures carry no wallet/token/transaction identifiers. Missing synthetic fields become null normalized coordinates in JSON; the historical classifier's comparisons and the missing-input flag are tested separately.

No original serialized estimator or notebook execution is needed. The expanded system includes actual historical implementation, offline demo entrypoints and optional read-only clients; see [system setup](system_setup.md). Live signing/submission and wallet access are hard-disabled. The public research reproduction under `src/` remains standard-library-only.

## Engineering reproduction

The original 16 research tests remain. Seventeen targeted engineering tests cover binary decoding, malformed inputs, feature windows, buyer anchors, deduplication, position/pending-exit lifecycle, corrupted state, risk limits, dry-run transitions and execution/network boundaries. Run `python3 -S -m unittest discover -s tests -v`. Core demos are `python3 -S -m collector --demo`, `python3 -S -m trading --demo`, and `python3 -S -m monitor --demo`. Optional third-party modules are separate from this dependency-free core.
