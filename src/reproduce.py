"""Reproduce public aggregate arithmetic and figures. Standard library only.

Usage: python3 src/reproduce.py [--check]
Paths derive from this file, not the current working directory or user's home.
--check verifies committed expected results, generated SVGs and the input manifest.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from phase_space import classify_checkpoint
from visuals import render_all

ROOT = Path(__file__).resolve().parents[1]


def compute(aggregates: dict, fixtures: list) -> dict:
    labels = aggregates['training_labels']
    phase = []
    for row in aggregates['warehouse_phase_rates']:
        rate = row['positives'] / row['n']
        if not math.isclose(rate, row['rate'], abs_tol=1e-12):
            raise ValueError('Stored phase rate does not match its counts')
        phase.append({'month': row['month'], 'phase': row['phase'],
                      'n': row['n'], 'positives': row['positives'], 'rate': rate})
    counts = aggregates['phase_snapshots']['phase_counts']
    if sum(counts.values()) != aggregates['phase_snapshots']['rows']:
        raise ValueError('Checkpoint counts do not sum to historical row total')
    live = aggregates['live_log_pair_summary']
    label_rate = labels['positive'] / labels['n']
    if not math.isclose(label_rate, labels['rate'], abs_tol=1e-12):
        raise ValueError('Stored prevalence does not match label counts')
    if not math.isclose(live['positive_pnl_usd'] + live['negative_pnl_usd'],
                        live['logged_proceeds_minus_cost_usd'], abs_tol=1e-9):
        raise ValueError('Live aggregate cashflow subtotals disagree')
    illustrated = []
    for row in fixtures:
        result = classify_checkpoint(row)
        if result['phase'] != row['expected_phase']:
            raise ValueError('Synthetic fixture expectation mismatch: '+row['case'])
        illustrated.append({'case': row['case'], **result})
    return {
        'scope': 'Public aggregate arithmetic and synthetic method demonstration only',
        'label_prevalence': label_rate,
        'phase_rates': phase,
        'historical_checkpoint_count_sum': sum(counts.values()),
        'paper_distinct_id_arithmetic': aggregates['paper']['n'] - aggregates['paper']['duplicate_nonempty_trade_ids'],
        'live_logged_usd_from_subtotals': round(live['positive_pnl_usd'] + live['negative_pnl_usd'], 4),
        'live_dollar_profit_factor_from_subtotals': live['positive_pnl_usd'] / -live['negative_pnl_usd'],
        # Unit conversion only: raw timing rows are not distributed.
        'reported_buy_latency_seconds': {
            k: aggregates['latency_ms']['signal_to_fill_ms'][k] / 1000
            for k in ('p50', 'p90', 'p99', 'max')},
        'synthetic_checkpoint_cases': illustrated,
    }


def serialize(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def load_inputs():
    aggregate = json.loads((ROOT/'data/verified_aggregates.json').read_text())
    with (ROOT/'data/checkpoint_method_fixtures.csv').open(newline='') as stream:
        fixtures = list(csv.DictReader(stream))
    with (ROOT/'data/policy_evolution.csv').open(newline='') as stream:
        policies = list(csv.DictReader(stream))
    timeline = json.loads((ROOT/'data/research_timeline.json').read_text())
    return aggregate, fixtures, policies, timeline


def verify_input_manifest():
    manifest = json.loads((ROOT/'data/manifest.json').read_text())
    for item in manifest['files']:
        path = ROOT / item['path']
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Manifest path escapes repository')
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Input fingerprint mismatch: '+item['path'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    verify_input_manifest()
    aggregate, fixtures, policies, timeline = load_inputs()
    result = serialize(compute(aggregate, fixtures))
    pictures = render_all(aggregate, fixtures, policies, timeline)
    if args.check:
        if result != (ROOT/'data/expected_results.json').read_text():
            raise SystemExit('Expected results differ; review inputs and implementation')
        for name, picture in pictures.items():
            if picture != (ROOT/'figures'/name).read_text():
                raise SystemExit('Figure differs: '+name)
        print(f'PASS: input hashes, expected arithmetic, {len(fixtures)} synthetic cases, {len(pictures)} SVGs')
    else:
        (ROOT/'data/expected_results.json').write_text(result)
        for name, picture in pictures.items():
            (ROOT/'figures'/name).write_text(picture)
        print(f'Wrote expected_results.json and {len(pictures)} deterministic SVGs; no private data or network used')


if __name__ == '__main__':
    main()
