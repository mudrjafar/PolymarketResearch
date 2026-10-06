"""Offline regressions. All state writes are redirected to temporary files."""
import ast
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ.setdefault('POLYMARKET_RPC_URL', 'http://127.0.0.1:1')

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

diamond = load('diamond_review', BASE/'scripts/diamond_filter_v3.py')
flow = load('flow_review', BASE/'scripts/flow_tracker.py')
bot = load('bot_review', BASE/'telegram_bot.py')
from machine_common import tail_jsonl, save_json_atomic

def fixture():
    now = datetime.now(timezone.utc)
    return {'schema_version':4, 'condition_id':'condition', 'updated_at':now.isoformat(),
        'market':{'token_id':'yes-token','question':'Test','outcome':'Yes','price':.42,
                  'last_trade_at':now.isoformat(),'end_date':(now+timedelta(days=7)).isoformat(),
                  'active':True,'closed':False,'accepting_orders':True},
        'state':'VERIFIED','verified':True,'confirmations':3,'data_confidence':90,
        'resolution_state':'FAR','remaining_seconds':604800,
        **{'flow_'+w:{'trade_count':25,'total_volume':12000,'net_flow':9000,
            'directional_strength':.75,'largest_trade_ratio':.1,'largest_trade':1200}
            for w in ['1m','5m','15m']}}

class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patchers = [patch.object(bot,'ENV_CHAT','123'),patch.object(bot,'PAPER',self.root/'paper.json'),
            patch.object(bot,'TRADES',self.root/'trades.jsonl'),patch.object(flow,'TRADES_FILE',self.root/'trades.jsonl'),
            patch.object(flow,'FLOW_STATE_FILE',self.root/'flow.json'),
            patch.object(flow,'VERIFICATION_STATE_FILE',self.root/'verify.json'),patch.object(flow,'market_states',{})]
        for item in self.patchers:
            item.start();self.addCleanup(item.stop)

    def test_fresh_strong_setup(self):
        self.assertTrue(diamond.analyze(fixture(),[])['diamond'])

    def test_stale_source_blocks_diamond_and_cashflow(self):
        x=fixture();x['updated_at']='2000-01-01T00:00:00Z'
        r=diamond.analyze(x,[])
        self.assertFalse(r['diamond']);self.assertFalse(r['cashflow_alert']['active'])

    def test_stale_price_blocks_diamond(self):
        x=fixture();x['market']['last_trade_at']='2000-01-01T00:00:00Z'
        self.assertFalse(diamond.analyze(x,[])['diamond'])

    def test_expired_and_closed_block_diamond(self):
        for changes in [{'remaining_seconds':0,'resolution_state':'EXPIRED'}]:
            x=fixture();x.update(changes);self.assertFalse(diamond.analyze(x,[])['diamond'])
        for changes in [{'active':False},{'closed':True},{'accepting_orders':False},{'end_date':'2000-01-01T00:00:00Z'}]:
            x=fixture();x['market'].update(changes);self.assertFalse(diamond.analyze(x,[])['diamond'])

    def test_legacy_snapshot_blocked(self):
        x=fixture();x.pop('schema_version');self.assertFalse(diamond.analyze(x,[])['diamond'])

    def test_sell_and_unauthorized_paper_blocked(self):
        r=diamond.analyze(fixture(),[])
        self.assertIsNotNone(bot.open_paper(r,25,'999')[1])
        r['direction']='SELL';self.assertIsNotNone(bot.open_paper(r,25,'123')[1])
        self.assertFalse(bot.PAPER.exists())

    def test_buy_paper_and_limits(self):
        r=diamond.analyze(fixture(),[])
        self.assertIsNone(bot.open_paper(r,25,'123')[1])
        self.assertIsNotNone(bot.open_paper(r,25,'123')[1])
        self.assertEqual(len(bot.paper()),1)

    def test_stale_paper_entry_blocked(self):
        r=diamond.analyze(fixture(),[]);r['source_updated_at']='2000-01-01T00:00:00Z'
        self.assertIsNotNone(bot.open_paper(r,25,'123')[1])

    def test_time_exit_waits_for_fresh_price(self):
        r=diamond.analyze(fixture(),[]);bot.open_paper(r,25,'123')
        rows=bot.paper();rows[0]['opened_at']='2000-01-01T00:00:00+00:00';bot.save(bot.PAPER,rows)
        self.assertEqual(bot.update_paper(),[])
        self.assertEqual(bot.paper()[0]['exit_pending'],'WAITING_FOR_FRESH_PRICE')
        bot.TRADES.write_text(json.dumps({'collector_version':4,'token_id':'yes-token','fill_price':.5,
            'detected_at':datetime.now(timezone.utc).isoformat()})+'\n',encoding='utf-8')
        self.assertEqual(len(bot.update_paper()),1)

    def test_confirmation_resets_on_reversal(self):
        for evidence in ['a','b','c']:
            flow.update_market_state('yes',True,True,evidence,'BUY')
        self.assertEqual(flow.market_states['yes']['state'],'VERIFIED')
        self.assertEqual(flow.update_market_state('yes',True,True,'d','SELL')['confirmations'],1)
        self.assertEqual(flow.update_market_state('yes',True,True,'d','SELL')['confirmations'],1)

    def test_token_separation_full_cycle_and_empty_cleanup(self):
        now=datetime.now(timezone.utc).isoformat()
        rows=[{'condition_id':'same','token_id':token,'outcome':outcome,'side_label':'BUY',
            'trade_usd':100,'detected_at':now,'transaction_hash':tx_hash,'log_index':1}
            for token,outcome,tx_hash in [('yes','Yes','0x'+'aa'*32),('no','No','0x'+'bb'*32)]]
        flow.TRADES_FILE.write_text('\n'.join(json.dumps(x) for x in rows+rows)+'\n',encoding='utf-8')
        with patch.object(flow.time,'sleep',side_effect=InterruptedError),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(InterruptedError):flow.main()
        state=json.loads(flow.FLOW_STATE_FILE.read_text())
        self.assertEqual(set(state),{'yes','no'})
        self.assertTrue(all(x['flow_5m']['total_volume']==100 for x in state.values()))
        flow.TRADES_FILE.write_text('',encoding='utf-8')
        with patch.object(flow.time,'sleep',side_effect=InterruptedError),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(InterruptedError):flow.main()
        self.assertEqual(json.loads(flow.FLOW_STATE_FILE.read_text()),{})

    def test_atomic_storage_and_tail_partial_record(self):
        path=self.root/'state.json';save_json_atomic(path,{'ok':True})
        self.assertEqual(json.loads(path.read_text()),{'ok':True})
        path=self.root/'tail.jsonl';path.write_text(''.join(json.dumps({'n':i})+'\n' for i in range(10000))+'{"n":',encoding='utf-8')
        result=tail_jsonl(path,10)
        self.assertEqual(result[-1]['n'],9999);self.assertLessEqual(len(result),10)

    def test_bot_chat_authorization(self):
        from types import SimpleNamespace
        self.assertFalse(bot.authorized(SimpleNamespace(effective_chat=SimpleNamespace(id=999))))
        self.assertTrue(bot.authorized(SimpleNamespace(effective_chat=SimpleNamespace(id=123))))

if __name__=='__main__':unittest.main()
