# Run the published system safely

## Core offline workflow — Python 3.9+, no installation

Run from the repository root:

```sh
python3 -S -m collector --demo
python3 -S -m trading --demo
python3 -S -m monitor --demo
python3 -S src/reproduce.py --check
python3 -S -m monitor.architecture_figure
python3 -S -m unittest discover -s tests -v
```

The collector demo synthesizes binary events and uses the original decoder, bounded deduplication, state reconstruction, CSV tape and feature/signal-anchor code. The trading demo uses the actual risk/adapter/journal/state path: an oversize order is denied, then synthetic BUY and SELL intents become `dry_run_sent`. They are not real fills. The monitor reports local tape/execution counts and position/pending-exit state.

All generated artifacts are local and ignored under `.runtime/`. Set `SOLANA_OUTPUT_DIR` to a different local runtime directory if needed. Run each CLI once into a fresh directory for a clean sample; tape files append by design. `SOLANA_FIXTURE_SOL_USD` supplies the offline conversion constant (default 150), not a current market quote. No token price forecast or strategy performance is inferred from the demo.

## Optional full historical modules

The core workflow above needs no packages or network. The optional pinned environment targets Python 3.9–3.12; its NumPy pin predates Python 3.13. If you separately choose to explore the full historical collector/runtime/dashboard, create an environment and install the declared dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-engine.txt
```

No dependency installation or live collection was performed during publication. Version pins reflect the installed import-check environment, not a recovered original notebook lockfile. Optional SMOTE/SHAP callables have a separate `requirements-ml.txt`; those extras were not installed and historical fits were not rerun.

### Read-only network collection

Copy no operational configuration from private projects. See `config.example.toml` for field names; the programs read environment variables, not that TOML file. The following is a command template, not a bundled provider URL:

```sh
SOLANA_ALLOW_NETWORK=1 \
LIVE_RPC_URL='YOUR_READ_ONLY_RPC_ENDPOINT' \
SOLANA_WS_URL='YOUR_READ_ONLY_WEBSOCKET_ENDPOINT' \
.venv/bin/python -m collector --network
```

Only use this if you intentionally want your own read-only data collection and understand your provider's billing. The default demo never requests these endpoints. The collector is experimental: its historical program/account layouts and reconnect/recovery behavior are not a coverage guarantee. No live collector was exercised as a publication check.

### Historical dashboard

```sh
.venv/bin/python -m streamlit run monitor/dashboard.py
```

It reads the local runtime log schemas, never starts a trading process, and no longer loads a remote font. Some display labels/configuration panels describe historical models and do not certify that an artifact is included or trained. The standard-library monitor works without Streamlit.

## Live trading is excluded

There is no public opt-in that enables signing, wallet-file access or transaction submission. Hard-blocked historical construction/send code is retained for engineering review. A paper or dry-run configuration cannot activate micro-live. No funded account setup or real wallet configuration is provided. `SOLANA_ALLOW_NETWORK=1` permits only explicitly configured read-only collection; it does not enable trading.

See [source integration](source_integration.md) for retained mechanisms and changes, [provenance](evidence_and_provenance.md) for research evidence, and [limitations](limitations.md) for observational/scientific scope.

## Original checkpoint analysis

With the optional dataframe dependencies, `python3 -m signals.checkpoint_phase --input YOUR_CHECKPOINT_CSV --output .runtime/phase` runs the actual assignment/summary/transition/archetype functions on caller-supplied data. Required columns match the documented original checkpoint contract in chapter 3; original token rows are withheld. This command does not recreate the historical 2,100-row result from absent data.
