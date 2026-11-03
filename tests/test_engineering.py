"""Offline tests of the imported parser, state, risk and execution implementation."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from collector.parser import parse_trade, _parse_trade_event, DISC_TRADE
from collector.demo import messages
from signals.features import compute_features, compute_rule_signal_metrics
from trading.execution.config import ExecutionConfig, ExecutionMode, LiveConfig, load_execution_config
from trading.execution.types import ExecutionIntent, OrderAction, OrderStatus
from trading.execution.adapter import execute_buy, execute_sell, _load_wallet_keypair
from trading.execution import state, journal
from trading.execution.quote import make_signal_quote
from trading.risk import engine as risk
from trading.safety import require_read_only_rpc


class ParserAndFeatureTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'SOLANA_FIXTURE_SOL_USD':'150','SOLANA_ALLOW_NETWORK':'0'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_actual_binary_layout(self):
        trade=parse_trade(next(messages(start=1_700_000_000)))
        self.assertEqual(trade['side'],'buy');self.assertEqual(trade['sol_amount'],1)
        self.assertEqual(trade['usd'],150);self.assertEqual(trade['ts'],1_700_000_000)
        self.assertAlmostEqual(trade['price'],30/562_500_000)

    def test_truncated_event_does_not_unpack_past_end(self):
        for size in [0,96,97,104]:self.assertIsNone(_parse_trade_event(bytes(size)))

    def test_malformed_or_irrelevant_logs(self):
        self.assertIsNone(parse_trade({}))
        msg=next(messages());msg['params']['result']['value']['logs']=[]
        self.assertIsNone(parse_trade(msg))

    def test_non_trade_discriminator(self):
        msg=next(messages());ls=msg['params']['result']['value']['logs']
        ls[1]='Program data: '+base64.b64encode(b'nottrade'+bytes(105)).decode()
        self.assertIsNone(parse_trade(msg))

    def test_original_feature_ratios_and_window(self):
        trades=[parse_trade(m) for m in messages(start=1_700_000_000)]
        future=dict(trades[0],ts=1_700_002_000,usd=1e9)
        f=compute_features('synthetic',trades[0]['ts'],trades+[future])
        self.assertEqual(f['volume_first_30min'],1200)
        self.assertEqual(f['unique_buyers_30min'],7)
        self.assertEqual(f['top1_wallet_pct_30min'],.125)
        self.assertAlmostEqual(f['buyers_per_trade'],7/9)

    def test_original_buyer_trigger_and_clean_anchor(self):
        trades=[parse_trade(m) for m in messages(start=1_700_000_000)]
        signal=compute_rule_signal_metrics(trades,signal_unique_buyers=3)
        self.assertEqual(signal['signal_time'],1_700_000_003)
        self.assertEqual(signal['buy_volume_share_to_signal'],.75)
        self.assertEqual(signal['entry_mcap_clean'],8000)
        self.assertIsNone(compute_rule_signal_metrics(trades,signal_unique_buyers=60))

    def test_original_dedup_and_state(self):
        from market_state import reconstruction as reconstruction
        reconstruction.tokens.clear();reconstruction._seen_trade_keys.clear()
        trade=parse_trade(next(messages(start=1_700_000_000)))
        with patch.object(reconstruction,'log_outcome_v2_tape'),patch.object(reconstruction,'log_csv'):
            reconstruction.handle_trade(trade);reconstruction.handle_trade(trade)
        self.assertEqual(len(reconstruction.tokens[trade['mint']]['trades']),1)


class StateAndExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        out=Path(self.temp.name);(out/'logs').mkdir();(out/'state').mkdir()
        self.patches=[patch.object(state,'STATE_DIR',out/'state'),patch.object(state,'STATE_PATH',out/'state/positions.json')]
        for name,filename in [('ORDER_JOURNAL_PATH','orders.csv'),('RISK_EVENTS_PATH','risk.csv'),('EXECUTION_LOG_PATH','executions.csv'),('STATE_TRANSITIONS_PATH','transitions.csv')]:
            self.patches.append(patch.object(journal,name,out/'logs'/filename))
        self.patches.append(patch.object(journal,'LOGS_DIR',out/'logs'))
        self.patches.append(patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden in tests')))
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.config=ExecutionConfig(ExecutionMode.DRY_RUN_LIVE,LiveConfig(
            position_size_usd=2.5,allowed_policy_tracks=['synthetic_demo'],
            min_pressure_overrides={'synthetic_demo':.8},entry_mcap_min_overrides={'synthetic_demo':8000},
            entry_mcap_max_overrides={'synthetic_demo':9000},max_open_positions=1,max_notional_total_usd=3))
        self.intent=ExecutionIntent(OrderAction.BUY,'synthetic-token','synthetic_demo',2.5,8500,.85,'dry_run_live',
            quote_metadata=make_signal_quote(.00000005),extra={})

    def test_default_paper_and_live_config_rejected(self):
        with patch.dict(os.environ,{},clear=True):self.assertEqual(load_execution_config().mode,ExecutionMode.PAPER)
        with patch.dict(os.environ,{'TRADING_EXECUTION_MODE':'micro_live'}):
            with self.assertRaises(ValueError):load_execution_config()

    def test_original_dry_run_buy_sell_lifecycle(self):
        result=execute_buy(self.intent,self.config,'synthetic-trade')
        self.assertEqual(result.status,OrderStatus.DRY_RUN_SENT)
        self.assertEqual(len(state.get_open_positions()),1)
        result=execute_sell(replace(self.intent,action=OrderAction.SELL),self.config,'synthetic-trade')
        self.assertEqual(result.status,OrderStatus.DRY_RUN_SENT)
        self.assertEqual(len(state.get_open_positions()),0)
        self.assertTrue(journal.STATE_TRANSITIONS_PATH.exists())

    def test_size_and_pressure_denied(self):
        for intent in [replace(self.intent,intended_size_usd=10),replace(self.intent,pressure=.7)]:
            self.assertEqual(execute_buy(intent,self.config,'denied').status,OrderStatus.DENIED)
        self.assertEqual(len(state.get_open_positions()),0)

    def test_upper_mcap_edge_exclusive(self):
        self.assertIsNone(risk._check_entry_mcap_band(replace(self.intent,intended_entry_mcap=8000),self.config))
        self.assertFalse(risk._check_entry_mcap_band(replace(self.intent,intended_entry_mcap=9000),self.config).allow)

    def test_exposure_checks_preserved(self):
        positions={'one':{'status':'open','size_usd':2.5}}
        self.assertFalse(risk._check_max_open_positions(self.intent,self.config,positions).allow)
        self.assertFalse(risk._check_notional_cap(self.intent,self.config,positions).allow)

    def test_pending_exit_consumed_once(self):
        state.queue_pending_exit('pending',{'mint':'synthetic-token','source':'demo'})
        self.assertEqual(state.pop_pending_exit('pending')['mint'],'synthetic-token')
        self.assertIsNone(state.pop_pending_exit('pending'))

    def test_corrupt_state_read_and_integrity(self):
        state.STATE_PATH.write_text('not json')
        self.assertEqual(state.load_positions()['positions'],{})
        self.assertFalse(state.check_state_integrity()['ok'])

    def test_direct_live_and_wallet_paths_fail_before_io(self):
        with self.assertRaises(RuntimeError):
            execute_buy(self.intent,ExecutionConfig(ExecutionMode.MICRO_LIVE,self.config.live),'forbidden')
        with self.assertRaises(RuntimeError):_load_wallet_keypair('not-a-file')

    def test_rpc_send_disallowed_even_with_read_optin(self):
        with patch.dict(os.environ,{'SOLANA_ALLOW_NETWORK':'1','LIVE_RPC_URL':'https://example.invalid'}):
            with self.assertRaises(RuntimeError):require_read_only_rpc({'method':'sendTransaction'})

    def test_missing_quote_freshness_fails_when_configured(self):
        self.config.live.quote_max_age_ms=1000
        decision=risk._check_quote_freshness(replace(self.intent,quote_metadata=None),self.config)
        self.assertFalse(decision.allow)


if __name__=='__main__':unittest.main()
