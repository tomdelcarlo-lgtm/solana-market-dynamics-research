# Targeted dependency-security cleanup

The six open Dependabot alerts were inspected directly. They represent three advisories, each reported against both the engine manifest and the ML manifest that includes it. No affected package is merely transitive: GitHub marks Requests and Streamlit as direct dependencies in both resolved manifests.

| Alerts | Dependency / previous pin | Advisory | First patched version | Selected pin |
|---|---|---|---|---|
| 1, 4 | Requests 2.32.5 | [GHSA-gc5v-m9x4-r6x2](https://github.com/advisories/GHSA-gc5v-m9x4-r6x2): insecure temporary-file reuse; moderate | 2.33.0 | **2.33.0** |
| 2, 5 | Streamlit 1.50.0 | [GHSA-7p48-42j8-8846](https://github.com/advisories/GHSA-7p48-42j8-8846): unauthenticated SSRF on Windows / NTLM exposure; moderate | 1.54.0 | **1.54.0** |
| 3, 6 | Streamlit 1.50.0 | [GHSA-vqwp-45wm-r9r5](https://github.com/advisories/GHSA-vqwp-45wm-r9r5): cache hash collision; low | 1.53.1 | **1.54.0**, also required by the SSRF fix |

`requirements-engine.txt` introduces both exact pins. `requirements-ml.txt` includes that file with `-r requirements-engine.txt`, so it inherits the same fixes without a duplicate edit. `requirements.txt` remains the empty standard-library core manifest. Every other direct dependency pin is unchanged; no transitive dependency override or broad upgrade is introduced.

## Compatibility boundary

The patched [Requests metadata](https://pypi.org/pypi/requests/2.33.0/json) and [Streamlit metadata](https://pypi.org/pypi/streamlit/1.54.0/json) both require Python 3.10+. The optional engine setup therefore now documents Python 3.10–3.12 and uses Python 3.12 in its environment example. This is a necessary compatibility change, not something to conceal as an unchanged optional runtime requirement.

The research/core replay and dry-run workflow still supports Python 3.9+ without third-party packages. No research calculation, source API, function signature, fixture, historical finding or figure definition is changed. Public arithmetic and generated outputs are compared against the existing expectations. Upstream security behavior changes are intentional; equivalence is claimed only for the exercised project interfaces and reproducible outputs, not every behavior of the third-party libraries.

## Validation scope

The original 16 research tests and 17 engineering tests, public aggregate reproduction, all generated figures, and the secret/security audit are rerun. A disposable Python 3.12 environment uses the declared manifest with the two patched pins for optional import and interface checks; the existing installed environment is not upgraded. No live collector, paid API, wallet, transaction, historical fit or raw private dataset is used.

GitHub may need time to refresh its dependency graph after the push. The publication handoff reports the actual alert state observed after that refresh, separately from the local version-range checks.
