"""Synthetic binary events exercise the original parser and state/tape path.

No fixture encodes an observed token, wallet, signature or market result.
"""
import base64
import json
import struct
import time
from collector.parser import PUMPFUN_PROGRAM, DISC_TRADE, parse_trade


def messages(count=8, start=None):
    start = int(time.time()) if start is None else int(start)
    for i in range(count):
        # Artificial byte sequences, not real observed accounts or signing keys.
        payload = (bytes([17])*32 + struct.pack('<QQB', 1_000_000_000,
                   10_000_000_000, 0 if i == 2 else 1) + bytes([30+i])*32
                   + struct.pack('<qQQ', start+i, 30_000_000_000,
                                 562_500_000_000_000))
        yield {'params': {'result': {'value': {'logs': [
            'Program '+PUMPFUN_PROGRAM+' invoke [1]',
            'Program data: '+base64.b64encode(DISC_TRADE+payload).decode(),
        ]}}}}


def replay():
    from market_state import reconstruction as state
    from market_state import tape
    from signals.features import compute_features, compute_rule_signal_metrics
    from trading.safety import runtime_root
    out=runtime_root(); (out/'logs').mkdir(parents=True,exist_ok=True)
    state.tokens.clear(); state._seen_trade_keys.clear(); state._seen_tx_sigs.clear()
    trades=[]
    for message in messages():
        trade=parse_trade(message)
        if trade is None:raise RuntimeError('Synthetic binary fixture failed to decode')
        trades.append(trade);state.handle_trade(trade)
    first=trades[0]
    summary={'kind':'synthetic method replay; not historical market results',
             'parsed_events':len(trades),'tokens_observed':len(state.tokens),
             'features':compute_features(first['mint'],first['ts'],trades),
             'signal_example':compute_rule_signal_metrics(trades,signal_unique_buyers=3)}
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary
