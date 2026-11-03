# License and attribution review

The MIT license in [LICENSE](LICENSE) covers contributed custom source and accompanying documentation/figures in this repository. It is the selected default for this authorized public research release. It does not relicense dependencies or grant rights to excluded historical datasets.

Historical custom implementation is mapped in `docs/source_manifest.json` and explained in [source integration](docs/source_integration.md). New CLI, synthetic fixture generation, safety boundaries, tests and diagrams are publication-specific engineering. Third-party packages retain their licenses: Streamlit, pandas, NumPy, scikit-learn, XGBoost, imbalanced-learn, SHAP, solders, base58, requests, websockets, joblib and DuckDB. They are dependencies, not vendored code or claimed as authored here. The CLI shim documents an external CLI integration; that CLI source/binary is not bundled.

Program identifiers and instruction/account layouts are retained where technically necessary. They are protocol references, not personal wallets or signing material. No original Dune export, private event tape, individual trade/transaction record, populated database, trained model, wallet file, credential-bearing configuration, authentication/session file or original Git history is distributed. Sanitized aggregates and synthetic method examples are included; no blanket right to redistribute excluded third-party data is claimed.

The retained PNGs and research diagrams come from this research publication workflow. No external font/image asset is bundled. External references imply neither affiliation nor endorsement. Optional ML dependencies were not installed or fits rerun during publication.
