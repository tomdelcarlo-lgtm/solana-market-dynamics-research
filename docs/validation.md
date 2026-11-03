# Release validation

The expanded public release passes the original 16 research tests plus 17 engineering tests (33 total). Tests run with Python's site packages disabled. Core public replay and dry-run commands require no installed dependencies, endpoints, credentials or historical files.

Verified checks: binary field layout, truncated payload rejection, non-trade messages, feature windows/ratios, buyer/clean-entry anchor, dedup, persisted dry-run BUY/SELL lifecycle, pending exits, corrupt-state rejection, first-failure risk guards, exposure and quote gates, hard live/wallet blocks and read-only RPC restrictions. One real historical integrity-read issue and the 105-byte boundary correction are documented publication changes, not new trading results.

`python3 -S src/reproduce.py --check` verifies original input fingerprints, eight synthetic checkpoint examples and all eleven original deterministic SVGs. `python3 -S -m monitor.architecture_figure` regenerates the new component map. Two retained historical PNGs remain fingerprinted. Local Markdown links resolve. Full historical runtime, migration collector, native module and dashboard imported offline with existing optional packages; SMOTE/SHAP extras were not installed or executed.

A public-only clone is checked separately during publication. No private source/data paths are used by public tests or demos. No real collector, wallet, transaction, live bot, paid API or dependency installation is exercised. Optional native/HTTP/dashboard dependencies are declared separately from the stdlib core.

Scientific limitations and raw-source reconstruction boundaries remain in [limitations](limitations.md). Exact retained source mappings and modifications are in [source integration](source_integration.md). Publication/security checks are recorded in [PUBLICATION_AUDIT.md](PUBLICATION_AUDIT.md).

Optional installed-library checks also confirmed full-runtime/extracted-feature parity on synthetic trades, original pandas phase assignments on the eight public synthetic cases, and successful creation of six empty historical schema/audit tables in an in-memory database. These are code/contract checks, not new historical results.
