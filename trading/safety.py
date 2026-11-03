"""Publication boundaries: no wallet access or transaction transmission.

Network opt-in permits only read-only collection. It never enables trading.
"""
import os
from pathlib import Path


def runtime_root():
    return Path(os.getenv('SOLANA_OUTPUT_DIR', str(Path(__file__).resolve().parents[1]/'.runtime'))).resolve()


def live_execution_disabled(*args, **kwargs):
    raise RuntimeError('Live execution and wallet access are disabled in this public release')


def require_network():
    if os.getenv('SOLANA_ALLOW_NETWORK') != '1':
        raise RuntimeError('Read-only networking requires explicit SOLANA_ALLOW_NETWORK=1')
    if not os.getenv('LIVE_RPC_URL'):
        raise RuntimeError('Read-only collection requires an explicit LIVE_RPC_URL; no endpoint is bundled')


def require_read_only_rpc(payload):
    require_network()
    if payload.get('method') not in {
        'getAccountInfo', 'getMultipleAccounts', 'getTransaction', 'getBalance',
        'getLatestBlockhash', 'getSignatureStatuses', 'getTokenAccountBalance',
        'getTokenAccountsByOwner', 'getSlot', 'getBlockHeight', 'getVersion',
    }:
        raise RuntimeError('Only allowlisted read-only RPC methods are permitted')
