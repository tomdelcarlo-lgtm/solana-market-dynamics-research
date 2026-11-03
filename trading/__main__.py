"""Exercise the actual historical risk, journal and position dry-run path."""
import argparse
import json
from trading.execution.config import ExecutionConfig, ExecutionMode, LiveConfig
from trading.execution.types import ExecutionIntent, OrderAction
from trading.execution.adapter import execute_buy, execute_sell


def demo():
    config=ExecutionConfig(mode=ExecutionMode.DRY_RUN_LIVE, live=LiveConfig(
        position_size_usd=2.5, allowed_policy_tracks=['synthetic_demo'],
        min_pressure_overrides={'synthetic_demo':.8},
        entry_mcap_min_overrides={'synthetic_demo':8000},
        entry_mcap_max_overrides={'synthetic_demo':9000}))
    def intent(action,size=2.5):
        return ExecutionIntent(action=action,token='synthetic-token',
            policy_track='synthetic_demo',intended_size_usd=size,
            intended_entry_mcap=8500,pressure=.85,mode='dry_run_live',
            quote_metadata={'price_sol':0.00000005},extra={})
    denied=execute_buy(intent(OrderAction.BUY,10),config,trade_id='demo-denied')
    buy=execute_buy(intent(OrderAction.BUY),config,trade_id='demo-accepted')
    sell=execute_sell(intent(OrderAction.SELL),config,trade_id='demo-accepted')
    return {'kind':'synthetic dry-run; no transaction or fill',
            'oversize':denied.status.value,'buy':buy.status.value,'sell':sell.status.value}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--demo',action='store_true');p.parse_args()
    print(json.dumps(demo(),indent=2))


if __name__=='__main__':main()
