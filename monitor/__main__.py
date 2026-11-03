"""Offline status view over the actual replay tape and original position store."""
import argparse
import csv
import json
from trading.safety import runtime_root


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--demo',action='store_true');p.parse_args()
    from trading.execution.state import get_open_positions, get_pending_exits
    root=runtime_root();summary=root/'summary.json'
    def count(name):
        path=root/'logs'/name
        if not path.exists():return 0
        with path.open() as f:return sum(1 for _ in csv.DictReader(f))
    report={'mode':'offline observation only',
            'market_replay':json.loads(summary.read_text()) if summary.exists() else 'Run python -m collector --demo first',
            'open_positions':len(get_open_positions()),'pending_exits':len(get_pending_exits()),
            'market_tape_rows':count('outcome_v2_tape.csv'),
            'execution_event_rows':count('live_execution_log.csv')}
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
