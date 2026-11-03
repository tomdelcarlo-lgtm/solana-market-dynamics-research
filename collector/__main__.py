"""Offline replay by default; read-only migration collection is explicit opt-in."""
import argparse
import asyncio
import json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--demo',action='store_true',help='Replay synthetic binary events (default)')
    p.add_argument('--network',action='store_true',help='Opt-in read-only collector; configured endpoints and environment gate required')
    args=p.parse_args()
    if args.network:
        from trading.safety import require_network
        require_network()
        import os
        if not os.getenv('SOLANA_WS_URL'):p.error('SOLANA_WS_URL must be explicitly configured')
        from collector.migration_collector import main as collect
        asyncio.run(collect())
    else:
        from collector.demo import replay
        print(json.dumps(replay(),indent=2))


if __name__=='__main__':main()
