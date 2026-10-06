"""Offline integration: real ABI decoding/SQLite; mocked RPC and Gamma."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
from eth_abi import encode

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ.setdefault('POLYMARKET_RPC_URL', 'http://127.0.0.1:1')
spec = importlib.util.spec_from_file_location('collector_sqlite_test', BASE/'scripts/live_active_trades.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
REAL_GET_LOGS = collector.get_order_filled_logs
from collector_storage_v4.storage import CollectorStore, StorageError
from collector_storage_v4.bridge import CollectorLock, publish_snapshot


def h(n):
    return '0x'+f'{n:064x}'


def header(n):
    return {'block_number':n,'block_hash':h(n),'parent_hash':h(n-1),
            'block_timestamp':int(time.time())-30+(n-101)*2}


def market(token='123'):
    return {'market_id':'1','condition_id':h(999),'question':'Offline fixture',
            'outcome':'Yes' if token=='123' else 'No','price':'.5',
            'volume24hr':1000,'volume':10000,'active':True,'closed':False,
            'accepting_orders':True,'end_date':'2027-01-01T00:00:00Z'}


def event(n=101, token=123, exchange=None, side=0, maker_event=False):
    exchange=exchange or collector.EXCHANGES[0]
    taker='0x'+'b'*40 if maker_event else exchange
    data=encode(['uint256']*5+['bytes32']*2,
                [side,token,25_000_000 if side==0 else 50_000_000,
                 50_000_000 if side==0 else 25_000_000,0,b'\0'*32,b'\0'*32])
    return {'blockNumber':hex(n),'blockHash':h(n),'transactionHash':h(n+1000),
            'logIndex':'0x0','data':'0x'+data.hex(), 'removed':False,'address':exchange,
            'topics':[collector.ORDER_FILLED_TOPIC,h(42),'0x'+'0'*24+'a'*40,
                      '0x'+'0'*24+taker[2:]],'_exchange_address':exchange}


class Live1FDiagnosticsTests(unittest.TestCase):
    """T1–T28: frozen lookup semantics and bounded, passive evidence."""
    def setUp(self):
        self.previous = collector._LIVE1C_CURRENT
        self.addCleanup(setattr, collector, '_LIVE1C_CURRENT', self.previous)
        collector._live1f_reset()
        self.record = {'gamma_lookup_ms': 0.0, 'checkpoint_before': 100,
                       'safe_tip': 120, 'from_block': 101, 'to_block': 105}
        collector._LIVE1C_CURRENT = self.record

    def response(self, payload=None, elapsed=None):
        class Response:
            def raise_for_status(self): pass
            def json(self): return payload
        r = Response()
        if elapsed is not None: r.elapsed = elapsed
        return r

    def payload(self):
        return [{'id': '1', 'conditionId': h(999), 'question': 'fixture',
                 'clobTokenIds': ['123'], 'outcomes': ['Yes'],
                 'outcomePrices': ['0.5'], 'active': True, 'closed': False}]

    def lookup(self, responses):
        with patch.object(collector.requests, 'get', side_effect=responses) as get:
            result = collector.lookup_market('123')
        return result, get

    def summary(self):
        return collector._live1f_summary(self.record)

    def sample(self):
        return self.summary()['samples'][-1]

    def test_live1f_T01_open_only(self):
        expected = collector.build_token_map(self.payload())['123']
        result, get = self.lookup([self.response(self.payload())])
        self.assertEqual(result, expected)
        self.assertEqual(get.call_count, 1)
        s = self.sample()
        self.assertEqual(s['resolved_stage'], 'OPEN')
        self.assertTrue(s['open_attempted']); self.assertFalse(s['closed_attempted'])
        self.assertEqual(s['api_request_count'], 1)
        self.assertEqual(s['closed_request_count'], 0)

    def test_live1f_T02_closed_fallback(self):
        result, get = self.lookup([self.response([]), self.response(self.payload())])
        self.assertEqual(result, collector.build_token_map(self.payload())['123'])
        self.assertEqual(get.call_count, 2)
        s = self.sample()
        self.assertEqual(s['resolved_stage'], 'CLOSED_FALLBACK')
        self.assertEqual((s['open_request_count'], s['closed_request_count']), (1, 1))

    def test_live1f_T03_complete_miss(self):
        with patch.object(collector.requests, 'get', side_effect=[self.response([])]*2) as get:
            with self.assertRaisesRegex(RuntimeError, '^Aggressor token metadata unavailable; batch will be retried$'):
                collector.lookup_market('123')
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.sample()['failure_class'], 'NOT_FOUND')
        self.assertEqual(self.summary()['lookup_failed_count'], 1)

    def test_live1f_T04_invalid_shape(self):
        with patch.object(collector.requests, 'get', return_value=self.response({})) as get:
            with self.assertRaisesRegex(RuntimeError, '^Gamma token lookup returned an invalid response$'):
                collector.lookup_market('123')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.sample()['failure_class'], 'INVALID_SHAPE')
        self.assertEqual(self.sample()['resolved_stage'], 'MALFORMED_OR_AMBIGUOUS')

    def test_live1f_T05_timer_failure(self):
        with patch.object(collector.time, 'perf_counter', side_effect=RuntimeError('timer')):
            result, get = self.lookup([self.response(self.payload())])
        self.assertEqual(result, collector.build_token_map(self.payload())['123'])
        self.assertEqual(get.call_count, 1)

    def test_live1f_T06_record_and_aggregation_failures(self):
        for name in ('_live1f_observe', '_live1f_event', '_live1f_start'):
            with self.subTest(name=name), patch.object(collector, name, side_effect=RuntimeError('diagnostic')):
                result, get = self.lookup([self.response(self.payload())])
                self.assertEqual(result, collector.build_token_map(self.payload())['123'])
                self.assertEqual(get.call_count, 1)
        class BrokenSamples(list):
            def append(self, value): raise RuntimeError('append')
        self.record['_live1f'] = {'samples': BrokenSamples(), 'counts': {},
                                 'totals': {}, 'failures': {}, 'invalid_timing': 0}
        result, get = self.lookup([self.response(self.payload())])
        self.assertEqual(result, collector.build_token_map(self.payload())['123'])
        self.assertEqual(get.call_count, 1)
        self.assertFalse(self.summary()['successful_token_observation_complete'])
        class BrokenStore:
            def checkpoint(self): return {'block_number': 100}
        r = collector._live1c_begin(BrokenStore())
        with patch.object(collector, '_live1f_summary', side_effect=RuntimeError('aggregation')), \
             patch.object(collector, '_emit_live1c_record', side_effect=RuntimeError('serialization')):
            self.assertIs(collector._live1c_finish(r, 'SUCCESS', BrokenStore()), r)

    def test_live1f_T07_control_exceptions_propagate(self):
        for kind in (KeyboardInterrupt, SystemExit):
            for location in ('_live1f_start', '_live1f_event', '_live1f_observe'):
                with self.subTest(kind=kind, location=location), \
                     patch.object(collector, location, side_effect=kind), \
                     patch.object(collector.requests, 'get', return_value=self.response(self.payload())):
                    with self.assertRaises(kind): collector.lookup_market('123')

    def scan(self, failure=False):
        with tempfile.TemporaryDirectory() as tmp, CollectorStore(Path(tmp)/'db') as st:
            st.initialize(100, h(100))
            mapping = collector.GammaTokenMap({})
            r = collector._live1c_begin(st)
            order = []
            commit = st.commit_batch
            def commit_observed(*a, **k):
                value = commit(*a, **k); order.append('commit'); return value
            with patch.object(collector, 'get_latest_block', return_value=123), \
                 patch.object(collector, 'get_block_header', side_effect=header), \
                 patch.object(collector, 'get_order_filled_logs', return_value=[event(), event(102)]), \
                 patch.object(collector.requests, 'get', return_value=self.response([] if failure else self.payload())) as get, \
                 patch.object(st, 'commit_batch', side_effect=commit_observed), \
                 patch.object(collector, 'publish_snapshot', side_effect=lambda *a: order.append('publish') or 2), \
                 patch.object(collector, '_emit_live1c_record'):
                if failure:
                    with self.assertRaises(RuntimeError): collector.scan_once(st, mapping)
                else: collector.scan_once(st, mapping)
                collector._live1c_finish(r, 'RETRYABLE_FAILURE' if failure else 'SUCCESS', st)
            return st.checkpoint()['block_number'], st.recent_trades(0), order, get.call_count, r

    def test_live1f_T08_existing_cache_insert(self):
        cp, rows, order, calls, r = self.scan()
        self.assertEqual(calls, 1); self.assertEqual(len(rows), 2)
        self.assertEqual(r['gamma_lookup_diagnostics']['successful_token_relookup_count'], 0)

    def test_live1f_T09_direct_relookup_not_suppressed(self):
        self.lookup([self.response(self.payload())]); _, get = self.lookup([self.response(self.payload())])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.summary()['successful_token_relookup_count'], 1)

    def test_live1f_T10_refresh_unchanged(self):
        self.assertEqual(collector.MARKET_REFRESH_INTERVAL, 300)
        with patch.object(collector.requests, 'get', return_value=self.response({'markets': self.payload(), 'next_cursor': None})) as get:
            collector.load_market_map()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.summary()['lookup_count'], 0)

    def test_live1f_T11_sqlite_checkpoint(self):
        cp, rows, order, calls, r = self.scan()
        self.assertEqual(cp, 103); self.assertEqual(len(rows), 2)
        self.assertEqual(r['gamma_lookup_count'], 1)

    def test_live1f_T12_commit_publish(self):
        self.assertEqual(self.scan()[2], ['commit', 'publish'])

    def test_live1f_T13_failed_lookup_atomic(self):
        cp, rows, order, calls, r = self.scan(True)
        self.assertEqual((cp, rows, order, calls), (100, [], [], 2))

    def test_live1f_T14_request_shape(self):
        self.lookup([self.response([]), self.response(self.payload())])
        shapes = self.sample()['request_shapes']
        self.assertEqual([s['stage'] for s in shapes], ['OPEN', 'CLOSED_FALLBACK'])
        self.assertTrue(all(s['limit'] == 100 and s['endpoint'] == '/markets' and s['request_ordinal'] == 1 for s in shapes))
        self.assertFalse(self.summary()['pagination_observed'])

    def test_live1f_T15_measured_latency(self):
        clock = iter(i/1000 for i in range(100))
        with patch.object(collector.time, 'perf_counter', side_effect=lambda: next(clock)):
            self.lookup([self.response([]), self.response(self.payload())])
        s = self.sample()
        self.assertAlmostEqual(s['lookup_total_ms'], 13.0)
        self.assertAlmostEqual(s['open_stage_ms'], 5.0)
        self.assertAlmostEqual(s['closed_stage_ms'], 5.0)
        self.assertAlmostEqual(s['local_processing_ms'], 2.0)
        self.assertAlmostEqual(s['open_request_ms'], 1.0)
        self.assertEqual(self.summary()['lookup_timing_sum_ms'], s['lookup_total_ms'])

    def test_live1f_T16_retention_baseline(self):
        m = collector.GammaTokenMap({})
        m.remember_fallback('123', market())
        refreshed = collector.merge_gamma_refresh(m, {})
        self.assertIn('123', refreshed)
        self.assertEqual(refreshed['123']['condition_id'], h(999))
        self.assertIsNone(refreshed['123']['active'])
        fresh = collector.merge_gamma_refresh(refreshed, {'123': market()})
        self.assertTrue(fresh['123']['active'])
        self.assertIsNot(fresh['123'], refreshed['123'])

    def test_live1f_T17_no_global_transport_patch(self):
        import ast
        tree = ast.parse((BASE/'scripts/live_active_trades.py').read_text())
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for target in targets:
                text = ast.unparse(target)
                self.assertNotIn(text, ('requests.get', 'requests.request', 'requests.Session', 'requests.post'))
        original = collector.requests.get
        self.lookup([self.response(self.payload())])
        self.assertIs(collector.requests.get, original)

    def test_live1f_T18_elapsed_present(self):
        from datetime import timedelta
        self.lookup([self.response(self.payload(), timedelta(milliseconds=12))])
        self.assertEqual(self.sample()['open_response_elapsed_ms'], 12)

    def test_live1f_T19_elapsed_unavailable(self):
        for elapsed in (None, object()):
            self.lookup([self.response(self.payload(), elapsed)])
            self.assertIsNone(self.sample()['open_response_elapsed_ms'])

    def test_live1f_T20_exact_arguments(self):
        _, get = self.lookup([self.response([]), self.response(self.payload())])
        from unittest.mock import call
        self.assertEqual(get.call_args_list, [call(f'{collector.GAMMA_API}/markets', params={'clob_token_ids':['123'], 'closed':closed, 'limit':100}, timeout=30) for closed in (False, True)])
        self.summary(); self.assertEqual(get.call_count, 2)

    def test_live1f_T21_immutability(self):
        from copy import deepcopy
        payload = self.payload(); before = deepcopy(payload)
        result, _ = self.lookup([self.response(payload)])
        self.summary()
        self.assertEqual(payload, before)
        self.assertEqual(result, collector.build_token_map(before)['123'])

    def test_live1f_T22_bounds_and_incomplete_duplicate_proof(self):
        for i in range(4100):
            sample = collector._live1f_start()
            sample.update(resolved=True, resolved_stage='OPEN', lookup_total_ms=1.0)
            collector._live1f_observe(sample, str(i))
        d = self.summary()
        self.assertEqual(d['diagnostic_sample_count'], 4096)
        self.assertEqual(d['diagnostic_sample_overflow_count'], 4)
        self.assertEqual(len(d['samples']), 4096)
        self.assertLessEqual(len(collector._LIVE1F_STATE['seen']), 4096)
        self.assertFalse(d['successful_token_observation_complete'])
        self.assertIsNone(d['successful_token_relookup_count'])
        self.assertEqual(d['lookup_count'], 4100)

    def test_live1f_T23_consistency(self):
        self.lookup([self.response(self.payload())])
        total = self.summary()['lookup_timing_sum_ms']
        self.record['gamma_lookup_ms'] = total
        d = self.summary(); self.assertEqual(d['lookup_timing_delta_ms'], 0)
        self.assertTrue(d['lookup_timing_consistent'])
        self.record['gamma_lookup_ms'] = total + 100
        self.assertFalse(self.summary()['lookup_timing_consistent'])

    def test_live1f_T24_open_cost_closed_subset(self):
        self.lookup([self.response([]), self.response(self.payload())])
        closed = self.sample()['open_stage_ms']
        self.lookup([self.response(self.payload())])
        d = self.summary()
        self.assertEqual(d['count_tokens_resolved_by_closed'], 1)
        self.assertEqual(d['open_ms_spent_on_tokens_resolved_by_closed'], closed)
        self.assertEqual(d['average_open_ms_before_closed_resolution'], closed)

    def test_live1f_T25_failure_enum_exception_identity(self):
        cases = [(collector.requests.Timeout('x'), 'REQUEST_TIMEOUT'),
                 (collector.requests.ConnectionError('x'), 'REQUEST_CONNECTION'),
                 (collector.requests.RequestException('x'), 'REQUEST_OTHER'),
                 (ValueError('json'), 'INVALID_JSON'),
                 (StorageError('identity'), 'IDENTITY_ERROR'),
                 (TypeError('existing'), 'OTHER_EXISTING_FAILURE')]
        for status, name in [(429,'HTTP_429'),(503,'HTTP_5XX'),(400,'HTTP_4XX'),(302,'HTTP_OTHER')]:
            cases.append((collector.requests.HTTPError(response=Mock(status_code=status)), name))
        for exc, expected in cases:
            with self.subTest(expected=expected):
                response = self.response(self.payload())
                if expected == 'INVALID_JSON': response.json = Mock(side_effect=exc)
                elif expected == 'IDENTITY_ERROR':
                    pass
                else: response.raise_for_status = Mock(side_effect=exc)
                with patch.object(collector.requests, 'get', return_value=response) as get, \
                     patch.object(collector, 'build_token_map', side_effect=exc if expected == 'IDENTITY_ERROR' else collector.build_token_map):
                    with self.assertRaises(type(exc)) as raised: collector.lookup_market('123')
                self.assertIs(raised.exception, exc)
                self.assertEqual(get.call_count, 1)
                self.assertEqual(self.sample()['failure_class'], expected)
        exc = collector.requests.Timeout('original')
        with patch.object(collector.requests, 'get', side_effect=exc), \
             patch.object(collector, '_live1f_observe', side_effect=RuntimeError('diagnostic')):
            with self.assertRaises(collector.requests.Timeout) as raised: collector.lookup_market('123')
        self.assertIs(raised.exception, exc)

    def test_live1f_T26_potential_truncation_only(self):
        self.lookup([self.response(self.payload()*100)])
        self.assertTrue(self.sample()['request_shapes'][0]['potential_truncation_signal'])
        self.assertFalse(self.summary()['pagination_observed'])

    def test_live1f_T27_nearest_rank(self):
        for values, expected in [([], (None,None,None,None)), ([8],(8,8,8,8)), ([2,1],(1,2,2,2)), (list(range(1,101)),(50,90,95,99))]:
            d = collector._live1f_percentiles(values)
            self.assertEqual(d['sample_count'], len(values))
            self.assertEqual(tuple(d[f'p{p}'] for p in (50,90,95,99)), expected)

    def test_live1f_T28_batch_correlation_and_raw_samples(self):
        with patch('builtins.print') as output:
            self.lookup([self.response(self.payload())])
        output.assert_not_called()
        d = self.summary()
        self.assertEqual(d['lag_before'], 20)
        self.assertEqual((d['from_block'], d['to_block']), (101,105))
        json.dumps(d)
        old_sequence = d['samples'][0]['lookup_sequence']
        collector._LIVE1C_CURRENT = {'gamma_lookup_ms': 0.0}
        self.lookup([self.response(self.payload())])
        d2 = collector._live1f_summary(collector._LIVE1C_CURRENT)
        self.assertEqual(len(d2['samples']), 1)
        self.assertGreater(d2['samples'][0]['lookup_sequence'], old_sequence)
        self.assertEqual(d2['diagnostic_sample_count'], 2)


class CollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'collector.db'
        self.json_path=self.root/'live_trades.jsonl'
        self.store=CollectorStore(self.path)
        self.store.initialize(100,h(100))
        self.addCleanup(lambda:self.store.close() if self.store else None)
        self.token_map={'123':market(),'456':market('456')}
        for p in [patch.object(collector,'LIVE_TRADES_FILE',self.json_path),
                  patch.object(collector,'get_latest_block',return_value=123),
                  patch.object(collector,'get_block_header',side_effect=header),
                  patch.object(collector,'get_order_filled_logs',return_value=[event()]),
                  patch.object(collector,'CONFIRMATION_BLOCKS',20),
                  patch.object(collector,'INITIAL_LOOKBACK_BLOCKS',500),
                  patch.object(collector,'START_BLOCK',''),
                  patch.object(collector.requests,'post',side_effect=AssertionError('No real RPC in tests')),
                  patch.object(collector.requests,'get',side_effect=AssertionError('No real Gamma in tests'))]:
            p.start();self.addCleanup(p.stop)

    def reopen(self):
        self.store.close()
        self.store=CollectorStore(self.path)

    def rows(self):
        return self.store.recent_trades(0)

    def test_real_decoding_both_exchanges_and_json_contract(self):
        logs=[event(),event(102,456,collector.EXCHANGES[1],side=1),event(103,maker_event=True)]
        with patch.object(collector,'get_order_filled_logs',return_value=logs):
            self.assertEqual(collector.scan_once(self.store,self.token_map),2)
        rows=[json.loads(line) for line in self.json_path.read_text().splitlines()]
        self.assertEqual([r['side_label'] for r in rows],['BUY','SELL'])
        self.assertEqual([r['trade_usd'] for r in rows],[25,25])
        self.assertEqual([r['token_amount'] for r in rows],[50,50])
        self.assertEqual([r['token_id'] for r in rows],['123','456'])
        self.assertEqual(self.store.checkpoint()['block_number'],103)
        self.assertTrue(all(r['block_timestamp'] and r['collector_version']==4 for r in rows))

    def test_second_exchange_rpc_failure_does_not_commit(self):
        # Exercise the real two-exchange implementation, bypassing setup mock.
        responses=Mock(side_effect=[[event()],RuntimeError('Second exchange failed')])
        with patch.object(collector,'get_order_filled_logs',side_effect=REAL_GET_LOGS), \
             patch.object(collector,'rpc_call',responses):
            with self.assertRaises(RuntimeError):collector.scan_once(self.store,self.token_map)
        self.assertEqual(responses.call_count,2)
        self.assertEqual(self.store.checkpoint()['block_number'],100)
        self.assertEqual(self.rows(),[])

    def test_bad_decode_mid_batch_retries_without_partial_history(self):
        bad=event(102);bad['data']='0x00'
        with patch.object(collector,'get_order_filled_logs',return_value=[event(),bad]):
            with self.assertRaises(Exception):collector.scan_once(self.store,self.token_map)
        self.assertEqual(self.store.checkpoint()['block_number'],100)
        self.assertEqual(self.rows(),[])
        self.assertEqual(collector.scan_once(self.store,self.token_map),1)

    def test_resume_uses_saved_block_and_waits_for_confirmations(self):
        collector.scan_once(self.store,self.token_map)
        self.reopen()
        collector.initialize_store(self.store,2000)
        self.assertEqual(self.store.checkpoint()['block_number'],103)
        with patch.object(collector,'get_latest_block',return_value=126), \
             patch.object(collector,'get_order_filled_logs',return_value=[]) as logs:
            collector.scan_once(self.store,self.token_map)
        logs.assert_called_once_with(104,106)
        self.assertEqual(self.store.checkpoint()['block_number'],106)

    def test_first_run_anchor_and_existing_cursor_not_reset(self):
        with CollectorStore(self.root/'first.db') as other:
            collector.initialize_store(other,1000)
            self.assertEqual(other.checkpoint()['block_number'],480)
            with patch.object(collector,'START_BLOCK','900'):
                collector.initialize_store(other,1000)
            self.assertEqual(other.checkpoint()['block_number'],480)

    def test_metadata_lookup_and_missing_metadata_hold_cursor(self):
        with patch.object(collector,'lookup_market',return_value=market()) as lookup:
            collector.scan_once(self.store,{})
            lookup.assert_called_once_with('123')
        with patch.object(collector,'get_latest_block',return_value=126), \
             patch.object(collector,'get_order_filled_logs',return_value=[event(104,789)]), \
             patch.object(collector,'lookup_market',side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):collector.scan_once(self.store,self.token_map)
        self.assertEqual(self.store.checkpoint()['block_number'],103)

    def test_lookup_closed_market_and_validate_token(self):
        response=Mock();response.raise_for_status.return_value=None
        response.json.side_effect=[[],[{'id':'1','conditionId':h(999),'clobTokenIds':'["123"]',
                                      'outcomes':'["Yes"]','closed':True}]]
        with patch.object(collector.requests,'get',return_value=response) as get:
            self.assertTrue(collector.lookup_market('123')['closed'])
        self.assertEqual(get.call_args_list[0].kwargs['params']['closed'],False)
        self.assertEqual(get.call_args_list[1].kwargs['params']['closed'],True)

    def test_reorg_or_log_hash_conflict_stops_batch(self):
        bad=event();bad['blockHash']=h(765)
        with patch.object(collector,'get_order_filled_logs',return_value=[bad]):
            with self.assertRaises(StorageError):collector.scan_once(self.store,self.token_map)
        self.assertEqual(self.store.checkpoint()['block_number'],100)
        changed=header(100);changed['block_hash']=h(765)
        with patch.object(collector,'get_block_header',return_value=changed):
            with self.assertRaises(StorageError):collector.initialize_store(self.store,123)

    def test_json_publish_failure_recovers_from_committed_sqlite(self):
        real=collector.publish_snapshot
        calls=0
        def interrupted(*args):
            nonlocal calls
            calls+=1
            if calls==1:raise OSError('Interrupted JSON publication')
            return real(*args)
        with patch.object(collector,'publish_snapshot',side_effect=interrupted):
            with self.assertRaises(OSError):collector.scan_once(self.store,self.token_map)
        self.assertEqual(self.store.checkpoint()['block_number'],103)
        self.assertFalse(self.json_path.exists())
        self.reopen()
        collector.initialize_store(self.store,123)
        collector.publish_snapshot(self.store,self.json_path)
        self.assertEqual(len(self.json_path.read_text().splitlines()),1)
        self.assertEqual(len(self.rows()),1)

    def test_abrupt_collector_death_after_commit_repairs_json_on_restart(self):
        source='''
import os,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_collector_sqlite import collector,header,event,market,CollectorStore
real=collector.publish_snapshot
calls=0
def crash_after_commit(*args):
    global calls
    calls+=1
    if calls==1:os._exit(75)
    return real(*args)
with CollectorStore(sys.argv[1]) as store:
    with patch.object(collector,'LIVE_TRADES_FILE',Path(sys.argv[2])), \\
         patch.object(collector,'get_latest_block',return_value=123), \\
         patch.object(collector,'get_block_header',side_effect=header), \\
         patch.object(collector,'get_order_filled_logs',return_value=[event()]), \\
         patch.object(collector,'CONFIRMATION_BLOCKS',20), \\
         patch.object(collector,'publish_snapshot',side_effect=crash_after_commit):
        collector.scan_once(store,{'123':market()})
'''
        result=subprocess.run([sys.executable,'-B','-c',source,str(self.path),str(self.json_path)],
                              cwd=BASE,capture_output=True,text=True)
        self.assertEqual(result.returncode,75,result.stderr)
        self.reopen()
        self.assertEqual(self.store.checkpoint()['block_number'],103)
        self.assertFalse(self.json_path.exists())
        collector.initialize_store(self.store,123)
        collector.publish_snapshot(self.store,self.json_path)
        self.assertEqual(len(self.json_path.read_text().splitlines()),1)
        self.assertEqual(len(self.rows()),1)

    def test_atomic_json_preserves_original_on_failed_replace(self):
        original=b'{"old_history":true}\n'
        self.json_path.write_bytes(original)
        # Backup replacement succeeds; snapshot replacement then fails.
        from collector_storage_v4 import bridge
        real=bridge.replace_file
        def replace(temp,dest):
            if Path(dest)==self.json_path:raise PermissionError('Locked file')
            real(temp,dest)
        with patch.object(bridge,'replace_file',side_effect=replace):
            with self.assertRaises(PermissionError):publish_snapshot(self.store,self.json_path)
        self.assertEqual(self.json_path.read_bytes(),original)
        self.assertEqual((self.root/'live_trades_before_sqlite.jsonl').read_bytes(),original)
        collector.scan_once(self.store,self.token_map)
        publish_snapshot(self.store,self.json_path)
        self.assertEqual((self.root/'live_trades_before_sqlite.jsonl').read_bytes(),original)

    def test_second_collector_lock_refused_and_released(self):
        path=self.root/'collector.lock'
        with CollectorLock(path):
            with self.assertRaises(RuntimeError):
                with CollectorLock(path):pass
        with CollectorLock(path):pass

    def test_rpc_exception_never_exposes_url(self):
        secret_url='https://example.invalid/PRIVATE_ENDPOINT_TEST'
        with patch.object(collector.requests,'post',side_effect=collector.requests.ConnectionError(secret_url)):
            with self.assertRaises(RuntimeError) as error:
                collector.rpc_call('eth_blockNumber',[])
        self.assertNotIn('PRIVATE_ENDPOINT_TEST',str(error.exception))

    def test_old_blocks_excluded_from_json_but_retained_in_database(self):
        old=header(101);old['block_timestamp']=1700000000
        row=collector.normalize_trade(event(),collector.decode_order_filled(event()),market(),old)
        self.store.commit_batch([old],[row])
        publish_snapshot(self.store,self.json_path)
        self.assertEqual(self.json_path.read_text(),'')
        self.assertEqual(len(self.rows()),1)



    def test_retryable_collector_error_uses_first_outer_backoff_before_retry(self):
        with (
            patch.object(collector, "CollectorLock"),
            patch.object(collector, "CollectorStore") as store_cls,
            patch.object(collector, "rpc_call", return_value="0x89"),
            patch.object(collector, "initialize_store", return_value={"block_number": 100}),
            patch.object(collector, "publish_snapshot"),
            patch.object(collector, "load_market_map", return_value={}),
            patch.object(collector, "scan_once", side_effect=[collector.RetryableCollectorError("temporary failure"), KeyboardInterrupt]) as scan,
            patch.object(collector.time, "sleep") as sleep_mock,
        ):
            store_cls.return_value.__enter__.return_value = self.store
            collector.main()

        self.assertEqual(scan.call_count, 2)
        sleep_mock.assert_called_once_with(1)


    def _run_main_retry_case(self, scan_effect, checkpoint=None):
        cp = checkpoint if checkpoint is not None else {'block_number': 100}
        store = Mock()
        store.checkpoint.side_effect = lambda: dict(cp)
        with (
            patch.object(collector, 'CollectorLock'),
            patch.object(collector, 'CollectorStore') as store_cls,
            patch.object(collector, 'rpc_call', return_value='0x89'),
            patch.object(collector, 'initialize_store', return_value={'block_number': 100}),
            patch.object(collector, 'publish_snapshot'),
            patch.object(collector, 'load_market_map', return_value={}),
            patch.object(collector, 'scan_once', side_effect=scan_effect) as scan,
            patch.object(collector.time, 'sleep') as sleep_mock,
        ):
            store_cls.return_value.__enter__.return_value = store
            self._a2b_scan = scan
            self._a2b_sleep = sleep_mock
            collector.main()
        return scan, sleep_mock, store

    def test_a2b_rpc_retryable_transport_and_http_errors_keep_classification(self):
        cases = [
            collector.requests.ConnectionError('offline'),
            collector.requests.HTTPError(response=Mock(status_code=429)),
            collector.requests.HTTPError(response=Mock(status_code=503)),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__, status=getattr(getattr(exc, 'response', None), 'status_code', None)):
                post = Mock(side_effect=exc)
                with patch.object(collector.requests, 'post', post), patch.object(collector.time, 'sleep') as sleep_mock:
                    with self.assertRaises(Exception) as error:
                        collector.rpc_call('eth_blockNumber', [])
                self.assertEqual(type(error.exception).__name__, 'RetryableCollectorError')
                self.assertEqual(post.call_count, 3)
                self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, 2])

    def test_a2b_rpc_401_403_are_fatal_config_without_inner_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                exc = collector.requests.HTTPError(response=Mock(status_code=status))
                post = Mock(side_effect=exc)
                with patch.object(collector.requests, 'post', post), patch.object(collector.time, 'sleep') as sleep_mock:
                    with self.assertRaises(Exception) as error:
                        collector.rpc_call('eth_blockNumber', [])
                self.assertEqual(type(error.exception).__name__, 'FatalConfigError')
                self.assertEqual(post.call_count, 1)
                sleep_mock.assert_not_called()

    def test_a2b_temporary_jsonrpc_provider_error_is_retryable_batch(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': 'temporary'}}
        with patch.object(collector.requests, 'post', return_value=response), patch.object(collector.time, 'sleep') as sleep_mock:
            with self.assertRaises(Exception) as error:
                collector.rpc_call('eth_getLogs', [])
        self.assertEqual(type(error.exception).__name__, 'RetryableCollectorError')
        sleep_mock.assert_not_called()

    def test_a2b_outer_retry_backoff_is_1_2_5_10_30_and_caps(self):
        retry_type = getattr(collector, 'RetryableCollectorError', RuntimeError)
        effects = [retry_type('temporary') for _ in range(6)] + [KeyboardInterrupt()]
        scan, sleep_mock, _ = self._run_main_retry_case(effects)
        self.assertEqual(scan.call_count, 7)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, 2, 5, 10, 30, 30])


    def test_a2b_timeout_is_retryable_batch(self):
        post = Mock(side_effect=collector.requests.Timeout('slow'))
        with patch.object(collector.requests, 'post', post), patch.object(collector.time, 'sleep') as sleep_mock:
            with self.assertRaises(Exception) as error:
                collector.rpc_call('eth_blockNumber', [])
        self.assertEqual(type(error.exception).__name__, 'RetryableCollectorError')
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, 2])

    def test_a2b_sqlite_busy_is_retryable_batch(self):
        busy = sqlite3.OperationalError('busy')
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        scan, sleep_mock, _ = self._run_main_retry_case([busy, KeyboardInterrupt()])
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1])

    def test_a2b_storage_error_is_fatal_without_retry(self):
        with self.assertRaises(SystemExit) as stopped:
            self._run_main_retry_case([StorageError('history conflict')])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self._a2b_scan.call_count, 1)
        self._a2b_sleep.assert_not_called()

    def test_a2b_unknown_runtime_error_is_fatal_not_retryable(self):
        with self.assertRaises(SystemExit) as stopped:
            self._run_main_retry_case([RuntimeError('decoder/programming failure'), KeyboardInterrupt()])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self._a2b_scan.call_count, 1)
        self._a2b_sleep.assert_not_called()

    def test_a2b_backoff_resets_only_after_checkpoint_advances(self):
        retry_type = getattr(collector, 'RetryableCollectorError', RuntimeError)
        cp = {'block_number': 100}
        calls = 0
        def effects(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise retry_type('temporary')
            if calls == 2:
                cp['block_number'] = 105
                return 0
            if calls == 3:
                raise retry_type('temporary again')
            raise KeyboardInterrupt()
        scan, sleep_mock, _ = self._run_main_retry_case(effects, checkpoint=cp)
        self.assertEqual(scan.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, collector.CHECK_INTERVAL, 1])

    def test_a2b_no_work_success_does_not_reset_backoff(self):
        retry_type = getattr(collector, 'RetryableCollectorError', RuntimeError)
        cp = {'block_number': 100}
        effects = [retry_type('temporary'), 0, retry_type('temporary again'), KeyboardInterrupt()]
        scan, sleep_mock, _ = self._run_main_retry_case(effects, checkpoint=cp)
        self.assertEqual(scan.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, collector.CHECK_INTERVAL, 2])

    def test_a2b_post_commit_snapshot_failure_is_local_degrade(self):
        cp = {'block_number': 100}
        calls = 0
        def effects(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                cp['block_number'] = 105
                raise OSError('snapshot export failed after commit')
            raise KeyboardInterrupt()
        scan, sleep_mock, _ = self._run_main_retry_case(effects, checkpoint=cp)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [collector.CHECK_INTERVAL])

    def test_a2b_invalid_start_block_is_fatal_config(self):
        other_path = self.root / 'invalid-start.db'
        with CollectorStore(other_path) as other, patch.object(collector, 'START_BLOCK', '999999'):
            with self.assertRaises(Exception) as error:
                collector.initialize_store(other, 1000)
        self.assertEqual(type(error.exception).__name__, 'FatalConfigError')


    def test_a2b2_inner_retry_recovers_without_outer_backoff(self):
        def response(payload):
            r = Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = payload
            return r

        success_response = response({'jsonrpc': '2.0', 'id': 1, 'result': '0x123'})
        real_rpc_call = collector.rpc_call

        class PostRouter:
            def __init__(self):
                self.call_count = 0
                self.effects = [collector.requests.Timeout('slow'), success_response]

            def __call__(self, *args, **kwargs):
                self.call_count += 1
                effect = self.effects[self.call_count - 1]
                if isinstance(effect, BaseException):
                    raise effect
                return effect

        post = PostRouter()
        store = Mock()
        store.checkpoint.return_value = {'block_number': 100}
        scan_calls = 0

        def rpc_dispatch(method, params):
            if method == 'eth_chainId':
                return '0x89'
            return real_rpc_call(method, params)

        def scan_effect(*_):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                self.assertEqual(collector.rpc_call('eth_blockNumber', []), '0x123')
                return 0
            raise KeyboardInterrupt()

        with (
            patch.object(collector, 'CollectorLock'),
            patch.object(collector, 'CollectorStore') as store_cls,
            patch.object(collector, 'rpc_call', side_effect=rpc_dispatch),
            patch.object(collector, 'initialize_store', return_value={'block_number': 100}),
            patch.object(collector, 'publish_snapshot'),
            patch.object(collector, 'load_market_map', return_value={}),
            patch.object(collector, 'scan_once', side_effect=scan_effect) as scan,
            patch.object(collector.requests, 'post', side_effect=post) as post_mock,
            patch.object(collector.time, 'sleep') as sleep_mock,
        ):
            store_cls.return_value.__enter__.return_value = store
            collector.main()

        self.assertEqual(scan.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(
            [c.args[0] for c in sleep_mock.call_args_list],
            [1, collector.CHECK_INTERVAL],
        )

    def test_a2b2_http500_inner_exhaustion_then_outer_backoff(self):
        real_rpc_call = collector.rpc_call
        http500 = collector.requests.HTTPError(response=Mock(status_code=500))
        post = Mock(side_effect=[http500, http500, http500])
        store = Mock()
        store.checkpoint.return_value = {'block_number': 100}
        scan_calls = 0

        def rpc_dispatch(method, params):
            if method == 'eth_chainId':
                return '0x89'
            return real_rpc_call(method, params)

        def scan_effect(*_):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                collector.rpc_call('eth_blockNumber', [])
                return 0
            raise KeyboardInterrupt()

        with (
            patch.object(collector, 'CollectorLock'),
            patch.object(collector, 'CollectorStore') as store_cls,
            patch.object(collector, 'rpc_call', side_effect=rpc_dispatch),
            patch.object(collector, 'initialize_store', return_value={'block_number': 100}),
            patch.object(collector, 'publish_snapshot'),
            patch.object(collector, 'load_market_map', return_value={}),
            patch.object(collector, 'scan_once', side_effect=scan_effect) as scan,
            patch.object(collector.requests, 'post', post),
            patch.object(collector.time, 'sleep') as sleep_mock,
        ):
            store_cls.return_value.__enter__.return_value = store
            collector.main()

        self.assertEqual(scan.call_count, 2)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, 2, 1])

    def test_a2b2_jsonrpc_permanent_and_unknown_errors_fail_closed(self):
        cases = [
            (-32600, 'invalid request'),
            (-32601, 'method not found'),
            (-32602, 'invalid params'),
            (-32099, 'unknown provider code'),
        ]
        for code, message in cases:
            with self.subTest(code=code):
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'error': {'code': code, 'message': message},
                }
                post = Mock(return_value=response)
                with (
                    patch.object(collector.requests, 'post', post),
                    patch.object(collector.time, 'sleep') as sleep_mock,
                ):
                    with self.assertRaises(Exception) as error:
                        collector.rpc_call('eth_getLogs', [])
                self.assertEqual(post.call_count, 1)
                sleep_mock.assert_not_called()
                self.assertNotIsInstance(error.exception, collector.RetryableCollectorError)

    def test_a2b2_sqlite_locked_retryable_and_ioerr_fatal(self):
        locked = sqlite3.OperationalError('database is locked')
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        scan, sleep_mock, _ = self._run_main_retry_case([locked, KeyboardInterrupt()])
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1])

        ioerr = sqlite3.OperationalError('disk I/O error')
        ioerr.sqlite_errorcode = sqlite3.SQLITE_IOERR
        with self.assertRaises(SystemExit) as stopped:
            self._run_main_retry_case([ioerr])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self._a2b_scan.call_count, 1)
        self._a2b_sleep.assert_not_called()

    def test_a2b2_programming_error_after_real_checkpoint_progress_is_fatal(self):
        real_scan_once = collector.scan_once
        scan_calls = 0

        def scan_effect(store, token_map):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                return real_scan_once(store, token_map)
            raise ValueError('programming failure after prior committed progress')

        with (
            patch.object(collector, 'CollectorLock'),
            patch.object(collector, 'CollectorStore') as store_cls,
            patch.object(collector, 'rpc_call', return_value='0x89'),
            patch.object(collector, 'initialize_store', return_value={'block_number': 100}),
            patch.object(collector, 'load_market_map', return_value=self.token_map),
            patch.object(collector, 'get_latest_block', return_value=123),
            patch.object(collector, 'get_block_header', side_effect=header),
            patch.object(collector, 'get_order_filled_logs', return_value=[event()]),
            patch.object(collector, 'CONFIRMATION_BLOCKS', 20),
            patch.object(collector, 'publish_snapshot'),
            patch.object(collector, 'scan_once', side_effect=scan_effect) as scan,
            patch.object(collector.time, 'sleep') as sleep_mock,
        ):
            store_cls.return_value.__enter__.return_value = self.store
            with self.assertRaises(SystemExit) as stopped:
                collector.main()

        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(self.store.checkpoint()['block_number'], 103)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [collector.CHECK_INTERVAL])

if __name__=='__main__':
    unittest.main(verbosity=2)


# ============================================================
# LIVE-1C PERFORMANCE INSTRUMENTATION SAFETY DELTA
# ============================================================

class Live1CInstrumentationTests(unittest.TestCase):
    """LIVE-1C instrumentation-only contracts. No strategy assertions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "live1c.db"
        self.json_path = self.root / "live_trades.jsonl"
        self.store = CollectorStore(self.db)
        self.addCleanup(self.store.close)
        self.store.initialize(100, h(100))
        self.token_map = {"123": market()}

    def _begin(self, outer_retries=0):
        return collector._live1c_begin(self.store, outer_retries=outer_retries)

    def _finish(self, record, status="SUCCESS", backoff_ms=0, flag=None):
        return collector._live1c_finish(
            record, status, self.store, backoff_ms=backoff_ms, flag=flag
        )

    def test_live1c_01_final_header_recheck_no_commit_no_extra_header_rpc(self):
        record = self._begin()
        calls = []
        seen_to = 0
        observed_105_hashes = []
        self.store.commit_batch = Mock(wraps=self.store.commit_batch)

        def changing_header(number):
            nonlocal seen_to
            calls.append(number)
            value = header(number)
            if number == 105:
                seen_to += 1
                if seen_to == 2:
                    value = dict(value)
                    value["block_hash"] = h(999999)
            if number == 105:
                observed_105_hashes.append(value["block_hash"])
            return value

        with patch.object(collector, "get_latest_block", return_value=125) as latest, \
             patch.object(collector, "get_block_header", side_effect=changing_header) as headers, \
             patch.object(collector, "get_order_filled_logs", return_value=[]), \
             patch.object(collector, "publish_snapshot") as publish:
            with self.assertRaises(StorageError):
                collector.scan_once(self.store, self.token_map)

        result = self._finish(record, "FATAL_FAILURE")
        self.assertEqual(self.store.checkpoint()["block_number"], 100)
        publish.assert_not_called()
        latest.assert_called_once_with()
        self.assertEqual(headers.call_count, 6)
        self.assertEqual(calls.count(101), 1)
        self.assertEqual(calls.count(102), 1)
        self.assertEqual(calls.count(103), 1)
        self.assertEqual(calls.count(104), 1)
        self.assertEqual(calls.count(105), 2)
        self.assertEqual(seen_to, 2)
        self.assertEqual(observed_105_hashes, [h(105), h(999999)])
        self.store.commit_batch.assert_not_called()
        self.assertEqual(result["status"], "FATAL_FAILURE")

    def test_live1c_04_success_range_durations_counts_and_no_extra_rpc(self):
        record = self._begin()
        with patch.object(collector, "get_latest_block", return_value=125) as latest, \
             patch.object(collector, "get_block_header", side_effect=header) as headers, \
             patch.object(collector, "get_order_filled_logs", return_value=[event(101)]), \
             patch.object(collector, "publish_snapshot", return_value=7) as publish:
            collector.scan_once(self.store, self.token_map)

        result = self._finish(record)
        self.assertEqual(
            (result["from_block"], result["to_block"], result["blocks"]),
            (101, 105, 5),
        )
        self.assertEqual(result["safe_tip"], 105)
        self.assertEqual(result["checkpoint_before"], 100)
        self.assertEqual(result["checkpoint_after"], 105)
        self.assertEqual(result["lag_before"], 5)
        self.assertEqual(result["lag_after"], 0)
        self.assertEqual(result["compatibility_rows"], 7)
        self.assertEqual(result["raw_log_count"], 1)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["unique_tx_count"], 1)
        for field in (
            "batch_ms", "get_logs_ms", "timestamp_ms", "decode_ms",
            "enrichment_ms", "sqlite_ms", "compatibility_ms",
            "gamma_lookup_ms", "gamma_refresh_ms", "rpc_ms",
        ):
            self.assertGreaterEqual(result[field], 0)
        self.assertGreaterEqual(result["blocks_per_second"], 0)
        latest.assert_called_once_with()
        self.assertEqual(headers.call_count, 6)
        publish.assert_called_once_with(self.store, collector.LIVE_TRADES_FILE)

    def test_live1c_02_rpc_retry_counter_preserves_exact_attempts_and_sleep(self):
        record = self._begin()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x89"}
        post = Mock(side_effect=[collector.requests.Timeout("slow"), response])

        with patch.object(collector.requests, "post", post), \
             patch.object(collector.time, "sleep") as sleep_mock:
            self.assertEqual(collector.rpc_call("eth_blockNumber", []), "0x89")

        result = self._finish(record)
        self.assertEqual(post.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1])
        self.assertEqual(result["rpc_calls"], 2)
        self.assertEqual(result["rpc_retries"], 1)
        self.assertIn("RPC_RETRY", result["flags"])
        self.assertGreaterEqual(result["rpc_ms"], 0)

    def test_live1c_05_gamma_token_lookup_cache_unchanged(self):
        token_map = {}
        record1 = self._begin()
        with patch.object(collector, "get_latest_block", return_value=125), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[event(101)]), \
             patch.object(collector, "lookup_market", return_value=market()) as lookup, \
             patch.object(collector, "publish_snapshot", return_value=0):
            collector.scan_once(self.store, token_map)
        first = self._finish(record1)

        record2 = self._begin()
        with patch.object(collector, "get_latest_block", return_value=130), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[event(106)]), \
             patch.object(collector, "lookup_market", return_value=market()) as lookup2, \
             patch.object(collector, "publish_snapshot", return_value=0):
            collector.scan_once(self.store, token_map)
        second = self._finish(record2)

        lookup.assert_called_once_with("123")
        lookup2.assert_not_called()
        self.assertEqual(first["gamma_lookup_count"], 1)
        self.assertIn("TOKEN_LOOKUP", first["flags"])
        self.assertEqual(second["gamma_lookup_count"], 0)

    def test_live1c_08_commit_before_publish_and_rows_from_existing_return(self):
        order = []
        real_commit = self.store.commit_batch

        def commit(blocks, trades):
            order.append("commit")
            return real_commit(blocks, trades)

        def publish(store, destination):
            order.append("publish")
            return 23

        record = self._begin()
        with patch.object(self.store, "commit_batch", side_effect=commit) as commit_mock, \
             patch.object(collector, "get_latest_block", return_value=125), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[]), \
             patch.object(collector, "publish_snapshot", side_effect=publish) as publish_mock:
            collector.scan_once(self.store, self.token_map)

        result = self._finish(record)
        self.assertEqual(order, ["commit", "publish"])
        self.assertEqual(commit_mock.call_count, 1)
        self.assertEqual(publish_mock.call_count, 1)
        self.assertEqual(result["compatibility_rows"], 23)

    def test_live1c_11_metric_logging_failure_cannot_change_committed_batch(self):
        record = self._begin()
        with patch.object(collector, "get_latest_block", return_value=125), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[]), \
             patch.object(collector, "publish_snapshot", return_value=4) as publish:
            collector.scan_once(self.store, self.token_map)

        with patch.object(
            collector, "_emit_live1c_record", side_effect=RuntimeError("diagnostic sink failed")
        ):
            result = self._finish(record)

        self.assertEqual(self.store.checkpoint()["block_number"], 105)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(result["checkpoint_after"], 105)
        self.assertEqual(result["compatibility_rows"], 4)

    def test_live1c_03_06_10_outer_backoff_gamma_schedule_and_success_sleep_unchanged(self):
        cp = {"block_number": 100}
        store = Mock()
        store.checkpoint.side_effect = lambda: dict(cp)
        retry = collector.RetryableCollectorError("temporary")
        emitted = []

        # Startup time, before-refresh attempt, refresh boundary, refresh reset, next loop.
        clock = iter([1000.0, 1299.0, 1300.0, 1300.0, 1301.0])

        with patch.object(collector, "CollectorLock"), \
             patch.object(collector, "CollectorStore") as store_cls, \
             patch.object(collector, "rpc_call", return_value="0x89"), \
             patch.object(collector, "get_latest_block", return_value=123), \
             patch.object(collector, "initialize_store", return_value={"block_number": 100}), \
             patch.object(collector, "publish_snapshot"), \
             patch.object(collector, "load_market_map", side_effect=[{}, {}]) as refresh, \
             patch.object(collector, "scan_once", side_effect=[retry, 0, KeyboardInterrupt]), \
             patch.object(collector.time, "time", side_effect=lambda: next(clock)), \
             patch.object(collector.time, "sleep") as sleep_mock, \
             patch.object(collector, "_emit_live1c_record", side_effect=lambda r: emitted.append(dict(r))):
            store_cls.return_value.__enter__.return_value = store
            collector.main()

        self.assertEqual(collector.CHECK_INTERVAL, 1.5)
        self.assertEqual(refresh.call_count, 2)  # startup + exactly one scheduled refresh
        sleeps = [c.args[0] for c in sleep_mock.call_args_list]
        self.assertEqual(sleeps[:2], [1, collector.CHECK_INTERVAL])
        self.assertEqual(emitted[0]["status"], "RETRYABLE_FAILURE")
        self.assertEqual(emitted[0]["outer_retries"], 0)
        self.assertEqual(emitted[0]["backoff_ms"], 1000)
        self.assertEqual(emitted[1]["status"], "SUCCESS")
        self.assertIn("GAMMA_REFRESH", emitted[1]["flags"])
        self.assertGreaterEqual(emitted[1]["gamma_refresh_ms"], 0)


# ============================================================
# LIVE-1D GAMMA MISS + RPC ATTRIBUTION
# ============================================================

class Live1EIdentityRetentionTests(unittest.TestCase):
    def setUp(self):
        CollectorIntegrationTests.setUp(self)
        collector._LIVE1C_CURRENT = None
        self.addCleanup(lambda: setattr(collector, '_LIVE1C_CURRENT', None))

    def _scan(self, tokens, found):
        before = self.store.checkpoint()['block_number']
        with patch.object(collector, 'get_latest_block', return_value=before + 25), \
             patch.object(collector, 'get_order_filled_logs', return_value=[event(before + 1)]), \
             patch.object(collector, 'lookup_market', return_value=found) as lookup:
            collector.scan_once(self.store, tokens)
        return lookup

    def test_live1e_1_active_startup_accepts_optional_market_provenance(self):
        for provenance in (None, 'missing', '', 'provider:1', 1, 1.0):
            with self.subTest(provenance=provenance):
                active = market()
                if provenance == 'missing':
                    active.pop('market_id')
                else:
                    active['market_id'] = provenance
                with patch.object(collector, 'CollectorLock'), \
                     patch.object(collector, 'CollectorStore') as store_cls, \
                     patch.object(collector, 'rpc_call', return_value='0x89'), \
                     patch.object(collector, 'get_latest_block', return_value=123), \
                     patch.object(collector, 'initialize_store', return_value={'block_number': 100}), \
                     patch.object(collector, 'publish_snapshot'), \
                     patch.object(collector, 'load_market_map', return_value={'123': active}), \
                     patch.object(collector, 'scan_once', side_effect=KeyboardInterrupt) as scan:
                    store_cls.return_value.__enter__.return_value = self.store
                    collector.main()
                scan.assert_called_once()
                loaded = scan.call_args.args[1]['123']
                self.assertEqual(loaded['market_id'], None if provenance == 'missing' else provenance)
                self.assertEqual(loaded['condition_id'], h(999))

    def test_live1e_1_base_scheduled_refresh_without_market_id_succeeds(self):
        now = [1000.0]
        maps = []
        fresh = market()
        fresh.update(market_id=None, price='.8')
        def scan(store, tokens):
            maps.append(tokens)
            if len(maps) == 1:
                now[0] = 1300.0
                return 0
            raise KeyboardInterrupt
        with patch.object(collector, 'CollectorLock'), \
             patch.object(collector, 'CollectorStore') as store_cls, \
             patch.object(collector, 'rpc_call', return_value='0x89'), \
             patch.object(collector, 'get_latest_block', return_value=123), \
             patch.object(collector, 'initialize_store', return_value={'block_number': 100}), \
             patch.object(collector, 'publish_snapshot'), \
             patch.object(collector, 'load_market_map', side_effect=[{'123': market()}, {'123': fresh}]) as refresh, \
             patch.object(collector, 'scan_once', side_effect=scan), \
             patch.object(collector.time, 'time', side_effect=lambda: now[0]), \
             patch.object(collector.time, 'sleep'), \
             patch.object(collector, '_emit_live1c_record'):
            store_cls.return_value.__enter__.return_value = self.store
            collector.main()
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(len(maps), 2)
        self.assertIsNone(maps[1]['123']['market_id'])
        self.assertEqual(maps[1]['123']['price'], '.8')
        self.assertEqual(self.store.checkpoint()['block_number'], 100)

    def test_live1e_1_fallback_optional_market_id_is_valid_retained_and_ingested(self):
        for provenance in (None, 'missing', '', 'provider:1', 1.0):
            with self.subTest(provenance=provenance):
                found = market()
                if provenance == 'missing':
                    found.pop('market_id')
                else:
                    found['market_id'] = provenance
                expected = None if provenance == 'missing' else provenance
                tokens = collector.GammaTokenMap({})
                self._scan(tokens, found).assert_called_once_with('123')
                self.assertIn('123', tokens.fallback_identities)
                self.assertEqual(tokens['123']['market_id'], expected)
                retained = collector.merge_gamma_refresh(tokens, {})
                self.assertEqual(retained['123']['market_id'], expected)
                for field in ('active', 'closed', 'accepting_orders', 'price', 'volume',
                              'volume24hr', 'start_date', 'end_date'):
                    self.assertIsNone(retained['123'][field])
                self._scan(retained, found).assert_not_called()
                self.assertEqual(self.store.recent_trades(0)[-1]['market_id'], expected)

    def test_live1e_historical_identity_survives_without_second_lookup_or_stale_metadata(self):
        tokens = collector.GammaTokenMap({})
        old = market()
        old.update(active=False, closed=True, accepting_orders=False, extra_current='old')
        first = self._scan(tokens, old)
        first.assert_called_once_with('123')
        retained = collector.merge_gamma_refresh(tokens, {'456': market('456')})
        self.assertEqual(retained['123']['market_id'], '1')
        self.assertEqual(retained['123']['condition_id'], h(999))
        self.assertEqual(retained['123']['outcome'], 'Yes')
        for field in ('active', 'closed', 'accepting_orders', 'price', 'volume',
                      'volume24hr', 'start_date', 'end_date', 'extra_current', 'question'):
            self.assertIsNone(retained['123'].get(field))
        self.assertIn('456', retained)
        order = []
        commit = self.store.commit_batch
        with patch.object(self.store, 'commit_batch', side_effect=lambda *a: (order.append('commit'), commit(*a))[1]), \
             patch.object(collector, 'publish_snapshot', side_effect=lambda *a: order.append('publish')):
            second = self._scan(retained, old)
        second.assert_not_called()
        self.assertEqual(order, ['commit', 'publish'])
        self.assertEqual(self.store.checkpoint()['block_number'], 110)
        row = self.store.recent_trades(0)[-1]
        self.assertIsNone(row['active'])
        self.assertIsNone(row['volume24hr'])

    def test_live1e_active_open_fallback_clears_flags_then_fresh_metadata_wins(self):
        tokens = collector.GammaTokenMap({})
        old = market()
        self._scan(tokens, old)
        missing = collector.merge_gamma_refresh(tokens, {})
        for field in ('active', 'closed', 'accepting_orders', 'price', 'volume', 'volume24hr'):
            self.assertIsNone(missing['123'].get(field))
        fresh = market()
        fresh.update(active=False, closed=True, accepting_orders=False, price='.8',
                     volume=999, volume24hr=7, start_date='new-start', end_date='new-end')
        updated = collector.merge_gamma_refresh(missing, {'123': fresh, '456': market('456')})
        self.assertEqual(updated['123'], fresh)
        self.assertNotEqual(updated['123'], old)
        self.assertIn('456', updated)

    def test_live1e_semantic_identity_case_whitespace_and_market_provenance_are_not_conflicts(self):
        tokens = collector.GammaTokenMap({})
        self._scan(tokens, market())
        fresh = market()
        fresh.update(condition_id='  ' + h(999).upper() + ' ', outcome=' yEs ', market_id='2')
        merged = collector.merge_gamma_refresh(tokens, {' 123 ': fresh})
        self.assertIn('123', merged)
        self.assertEqual(merged['123']['market_id'], '2')
        self.assertEqual(merged['123']['condition_id'], h(999))
        self.assertEqual(merged['123']['outcome'], 'yEs')

    def test_live1e_real_identity_conflicts_are_explicit_atomic_and_fail_closed(self):
        for field, value in (('condition_id', h(1000)), ('outcome', 'No')):
            with self.subTest(field=field):
                tokens = collector.GammaTokenMap({'123': market()})
                before = dict(tokens['123'])
                changed = market()
                changed[field] = value
                with self.assertRaises(collector.GammaIdentityConflict) as error:
                    collector.merge_gamma_refresh(tokens, {'123': changed})
                self.assertIsInstance(error.exception, StorageError)
                self.assertEqual(tokens['123'], before)
                self.assertEqual(self.store.checkpoint()['block_number'], 100)

    def test_live1e_refresh_has_no_dict_aliasing(self):
        old = market()
        old['extra'] = {'value': 'old'}
        tokens = collector.GammaTokenMap({'123': old})
        tokens.remember_fallback('123', old)
        fresh = market()
        fresh['extra'] = {'value': 'fresh'}
        refreshed = collector.merge_gamma_refresh(tokens, {'123': fresh})
        refreshed['123']['extra']['value'] = 'changed'
        refreshed['123']['price'] = 'changed'
        self.assertEqual(old['extra']['value'], 'old')
        self.assertEqual(tokens['123']['extra']['value'], 'old')
        self.assertEqual(fresh['extra']['value'], 'fresh')
        self.assertEqual(fresh['price'], '.5')
        retained = collector.merge_gamma_refresh(refreshed, {})
        retained['123']['outcome'] = 'tampered'
        self.assertEqual(refreshed.fallback_identities['123']['outcome'], 'Yes')

    def test_live1e_failed_and_negative_lookup_insert_nothing_and_can_resolve_later(self):
        for failure in ('exception', 'negative', 'missing_identity'):
            with self.subTest(failure=failure):
                tokens = collector.GammaTokenMap({})
                before = self.store.checkpoint()['block_number']
                found = market()
                found['outcome'] = None
                kwargs = {'side_effect': RuntimeError('temporary')} if failure == 'exception' else {
                    'return_value': None if failure == 'negative' else found}
                with patch.object(collector, 'get_latest_block', return_value=before + 25), \
                     patch.object(collector, 'get_order_filled_logs', return_value=[event(before + 1)]), \
                     patch.object(collector, 'lookup_market', **kwargs), \
                     patch.object(self.store, 'commit_batch', wraps=self.store.commit_batch) as commit, \
                     patch.object(collector, 'publish_snapshot') as publish:
                    with self.assertRaises(Exception):
                        collector.scan_once(self.store, tokens)
                self.assertNotIn('123', tokens)
                self.assertNotIn('123', tokens.fallback_identities)
                self.assertEqual(self.store.checkpoint()['block_number'], before)
                commit.assert_not_called()
                publish.assert_not_called()
                self._scan(tokens, market()).assert_called_once_with('123')
                self.assertEqual(self.store.checkpoint()['block_number'], before + 5)

    def test_live1e_new_cache_instance_does_not_inherit_fallbacks(self):
        first = collector.GammaTokenMap({})
        self._scan(first, market())
        restarted = collector.GammaTokenMap({})
        self.assertEqual(restarted.fallback_identities, {})
        self.assertNotIn('123', restarted)
        self._scan(restarted, market()).assert_called_once_with('123')
        source = '''
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_collector_sqlite import collector
fresh=collector.GammaTokenMap({})
print(json.dumps({'map':dict(fresh),'fallbacks':fresh.fallback_identities}))
'''
        child = subprocess.run([sys.executable, '-B', '-c', source], cwd=BASE,
                               capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {'map': {}, 'fallbacks': {}})

    def test_live1e_unknown_retained_market_flags_block_existing_risk(self):
        from scripts.risk_engine import assess
        now = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)
        stamp = now.isoformat()
        row = {'schema_version': 4, 'source_updated_at': stamp, 'last_trade_at': stamp,
               'market_key': 'condition:condition|outcome:yes', 'question': 'TEST',
               'condition_id': 'condition', 'token_id': '123', 'outcome': 'Yes',
               'direction': 'BUY', 'price': .42, 'classification': 'DIAMOND', 'diamond': True,
               'why_not_diamond': [], 'metrics': {'verified': True, 'confirmations': 3,
               'largest_trade_ratio': .20}, 'resolution': {'remaining_seconds': 86400},
               'cashflow_alert': {'quality': 'BROAD'}}
        flow = {'schema_version': 4, 'token_id': '123', 'condition_id': 'condition',
                'outcome': 'Yes', 'direction': 'BUY', 'verified': True, 'confirmations': 3,
                'source_updated_at': stamp, 'last_trade_at': stamp, 'remaining_seconds': 86400,
                'evidence_id': '0xabc:4', 'evidence_cursor': [123, 4],
                'evidence_at': stamp,
                'market': {'active': True, 'closed': False, 'accepting_orders': True}}
        self.assertEqual(assess(row, {'123': flow}, now=now)['decision'], 'PASS')
        tokens = collector.GammaTokenMap({})
        without_provenance = market()
        without_provenance.pop('market_id')
        tokens.remember_fallback('123', without_provenance)
        retained = collector.merge_gamma_refresh(tokens, {})
        self.assertIsNone(retained['123']['market_id'])
        flow['market'] = retained['123']
        result = assess(row, {'123': flow}, now=now)
        self.assertEqual(result['decision'], 'BLOCK')
        self.assertTrue({'MARKET_NOT_ACTIVE', 'MARKET_CLOSED_UNSAFE', 'MARKET_NOT_ACCEPTING'}.issubset(result['reason_codes']))

    def test_live1e_base_main_uses_merge_at_existing_refresh_boundary(self):
        now = [1000.0]
        scans = []
        base_scan = collector._live1c_scan_impl
        def scan(store, tokens):
            scans.append(tokens)
            if len(scans) == 1:
                result = base_scan(store, tokens)
                now[0] = 1299.0
                return result
            if len(scans) == 2:
                now[0] = 1300.0
                return 0
            if len(scans) == 3:
                result = base_scan(store, tokens)
                now[0] = 1301.0
                return result
            raise KeyboardInterrupt
        def fixed_header(n):
            return {'block_number': n, 'block_hash': h(n), 'parent_hash': h(n-1), 'block_timestamp': 900}
        with patch.object(collector, 'CollectorLock'), \
             patch.object(collector, 'CollectorStore') as store_cls, \
             patch.object(collector, 'rpc_call', return_value='0x89'), \
             patch.object(collector, 'initialize_store', return_value={'block_number': 100}), \
             patch.object(collector, 'get_latest_block', side_effect=lambda: self.store.checkpoint()['block_number'] + 25), \
             patch.object(collector, 'get_block_header', side_effect=fixed_header), \
             patch.object(collector, 'get_order_filled_logs', side_effect=lambda a,b: [event(a)]), \
             patch.object(collector, 'lookup_market', return_value=market()) as lookup, \
             patch.object(collector, 'publish_snapshot'), \
             patch.object(collector, 'load_market_map', side_effect=[{}, {'456': market('456')}]) as refresh, \
             patch.object(collector, 'scan_once', side_effect=scan), \
             patch.object(collector.time, 'time', side_effect=lambda: now[0]), \
             patch.object(collector.time, 'sleep') as sleep, \
             patch.object(collector, '_emit_live1c_record'):
            store_cls.return_value.__enter__.return_value = self.store
            collector.main()
        self.assertEqual(refresh.call_count, 2)
        lookup.assert_called_once_with('123')
        self.assertIn('123', scans[2])
        self.assertIn('456', scans[2])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.5, 1.5, 1.5])
        self.assertEqual(collector.MARKET_REFRESH_INTERVAL, 300)
        self.assertEqual(self.store.checkpoint()['block_number'], 110)

    def test_live1e_duplicate_token_identity_conflict_in_gamma_response_is_not_silent(self):
        first = {'id': '1', 'conditionId': h(999), 'clobTokenIds': ['123'], 'outcomes': ['Yes']}
        second = dict(first, conditionId=h(1000))
        with self.assertRaises(collector.GammaIdentityConflict):
            collector.build_token_map([first, second])

    def test_live1e_refresh_conflict_stops_base_main_before_next_scan_or_commit(self):
        clock = [1000.0]
        changed = market()
        changed['condition_id'] = h(1000)
        def scan(store, tokens):
            clock[0] = 1300.0
            return 0
        with patch.object(collector, 'CollectorLock'), \
             patch.object(collector, 'CollectorStore') as store_cls, \
             patch.object(collector, 'rpc_call', return_value='0x89'), \
             patch.object(collector, 'initialize_store', return_value={'block_number': 100}), \
             patch.object(collector, 'publish_snapshot') as publish, \
             patch.object(collector, 'load_market_map', side_effect=[{'123': market()}, {'123': changed}]), \
             patch.object(collector, 'scan_once', side_effect=scan) as scans, \
             patch.object(self.store, 'commit_batch', wraps=self.store.commit_batch) as commit, \
             patch.object(collector.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(collector.time, 'sleep'), \
             patch.object(collector, '_emit_live1c_record'):
            store_cls.return_value.__enter__.return_value = self.store
            with self.assertRaises(SystemExit) as stopped:
                collector.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.store.checkpoint()['block_number'], 100)
        commit.assert_not_called()
        self.assertEqual(publish.call_count, 1)  # startup only

    def test_live1e_refresh_reports_mapping_sizes_without_extra_io(self):
        tokens = collector.GammaTokenMap({})
        tokens.remember_fallback('123', market())
        with patch('builtins.print') as output:
            refreshed = collector.merge_gamma_refresh(tokens, {'456': market('456')})
        text = ' '.join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn('active_token_mappings=1', text)
        self.assertIn('retained_fallback_identities=1', text)
        self.assertIn('total_mapping_size=2', text)
        self.assertEqual(len(refreshed), 2)


class Live1DAttributionTests(unittest.TestCase):
    def test_live1d_1_overlapping_rpc_exact_attribution_and_transport_identity(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        record = collector._live1c_begin(self.store)
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        attempts = {}
        sleeps = {}
        transport_changed = []
        patterns = {"eth_blockNumber": 1, "eth_getLogs": 2,
                    "eth_getBlockByNumber": 0}
        def post(url, **kwargs):
            method = kwargs["json"]["method"]
            with lock:
                attempts[method] = attempts.get(method, 0) + 1
                attempt = attempts[method]
                transport_changed.append(collector.requests.post is not transport)
            if attempt == 1:
                barrier.wait(timeout=5)
            if attempt <= patterns[method]:
                raise collector.requests.Timeout(method)
            response = Mock()
            response.json.return_value = {"result": method}
            return response
        def sleep(delay):
            with lock:
                sleeps.setdefault(threading.current_thread().name, []).append(delay)
        def call(method):
            threading.current_thread().name = method
            return collector.rpc_call(method, [])
        transport = Mock(side_effect=post)
        with patch.object(collector.requests, "post", transport), \
             patch.object(collector.time, "sleep", side_effect=sleep):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {method: pool.submit(call, method) for method in patterns}
                self.assertEqual({m: f.result(timeout=10) for m, f in futures.items()},
                                 {m: m for m in patterns})
            self.assertIs(collector.requests.post, transport)
        self.assertFalse(any(transport_changed))
        self.assertEqual(attempts, {m: n + 1 for m, n in patterns.items()})
        self.assertEqual(transport.call_count, 6)
        self.assertEqual(sleeps, {"eth_blockNumber": [1], "eth_getLogs": [1, 2]})
        self.assertEqual(record["rpc_methods"],
                         {m: {"calls": n + 1, "retries": n} for m, n in patterns.items()})
        self.assertEqual(record["rpc_calls"], 6)
        self.assertEqual(record["rpc_retries"], 3)
        self.assertEqual(sum(v["calls"] for v in record["rpc_methods"].values()), record["rpc_calls"])
        self.assertEqual(sum(v["retries"] for v in record["rpc_methods"].values()), record["rpc_retries"])
        collector._live1c_finish(record, "SUCCESS", self.store)

    def test_live1d_1_finish_failures_after_real_commit_are_fail_open(self):
        for failure in ("checkpoint", "read", "metrics", "logging"):
            with self.subTest(failure=failure):
                record = collector._live1c_begin(self.store)
                before = self.store.checkpoint()["block_number"]
                with patch.object(collector, "get_latest_block", return_value=before + 25), \
                     patch.object(collector, "get_block_header", side_effect=header), \
                     patch.object(collector, "get_order_filled_logs", return_value=[]), \
                     patch.object(self.store, "commit_batch", wraps=self.store.commit_batch) as commit, \
                     patch.object(collector, "publish_snapshot", return_value=0) as publish:
                    result = collector.scan_once(self.store, {})
                self.assertEqual(result, 0)
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(publish.call_count, 1)
                if failure == "checkpoint":
                    fault = patch.object(self.store, "checkpoint", side_effect=RuntimeError("diag"))
                elif failure == "read":
                    fault = patch.object(self.store, "checkpoint", return_value=object())
                elif failure == "metrics":
                    record["_started"] = object()
                    fault = patch.object(collector, "_emit_live1c_record")
                else:
                    fault = patch.object(collector, "_emit_live1c_record", side_effect=RuntimeError("diag"))
                with fault:
                    self.assertIs(collector._live1c_finish(record, "SUCCESS", self.store), record)
                self.assertIsNone(collector._LIVE1C_CURRENT)
                self.assertEqual(self.store.checkpoint()["block_number"], before + 5)
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(publish.call_count, 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CollectorStore(Path(self.temp.name) / "live1d.db")
        self.addCleanup(self.store.close)
        self.store.initialize(100, h(100))
        collector._LIVE1D_MISS_HISTORY = {}
        collector._LIVE1D_REFRESH_GENERATION = 0

    def _batch(self, token_map, block_number, resolved):
        r = collector._live1c_begin(self.store)
        with patch.object(collector, "get_latest_block", return_value=block_number + 24), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[event(block_number)]), \
             patch.object(collector, "lookup_market", return_value=resolved) as lookup, \
             patch.object(collector, "publish_snapshot", return_value=0):
            collector.scan_once(self.store, token_map)
        return collector._live1c_finish(r, "SUCCESS", self.store), lookup

    def test_live1d_gamma_first_repeat_refresh_classification_and_no_extra_lookup(self):
        active = market()
        active["active"], active["closed"] = True, False
        first, lookup1 = self._batch({}, 101, active)
        lookup1.assert_called_once_with("123")
        a = first["gamma_misses"][0]
        self.assertEqual(a["token_id"], "123")
        self.assertFalse(a["repeated_miss"])
        self.assertEqual(a["miss_number"], 1)
        self.assertEqual(a["refresh_generation"], 0)
        self.assertFalse(a["after_refresh"])
        self.assertEqual(a["market_classification"], "ACTIVE_OPEN")
        self.assertTrue(a["lookup_succeeded"])
        self.assertIsInstance(a["timestamp"], float)

        collector._live1d_note_refresh()

        historical = market()
        historical["active"], historical["closed"] = False, True
        second, lookup2 = self._batch({}, 106, historical)
        lookup2.assert_called_once_with("123")
        b = second["gamma_misses"][0]
        self.assertTrue(b["repeated_miss"])
        self.assertEqual(b["miss_number"], 2)
        self.assertEqual(b["refresh_generation"], 1)
        self.assertTrue(b["after_refresh"])
        self.assertTrue(b["fallback_lost_after_refresh"])
        self.assertEqual(b["previous_market_classification"], "ACTIVE_OPEN")
        self.assertEqual(b["market_classification"], "CLOSED_INACTIVE_HISTORICAL")

        r = collector._live1c_begin(self.store)
        with patch.object(collector, "get_latest_block", return_value=135), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[event(111)]), \
             patch.object(collector, "lookup_market") as cached_lookup, \
             patch.object(collector, "publish_snapshot", return_value=0):
            collector.scan_once(self.store, {"123": historical})
        cached = collector._live1c_finish(r, "SUCCESS", self.store)
        cached_lookup.assert_not_called()
        self.assertEqual(cached["gamma_misses"], [])

    def test_live1d_rpc_retry_attribution_by_method_without_extra_rpc(self):
        r = collector._live1c_begin(self.store)
        ok = Mock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x89"}
        post = Mock(side_effect=[
            collector.requests.Timeout("bn"), ok,
            collector.requests.Timeout("l1"),
            collector.requests.Timeout("l2"), ok,
            ok,
        ])
        with patch.object(collector.requests, "post", post), \
             patch.object(collector.time, "sleep") as sleep_mock:
            collector.rpc_call("eth_blockNumber", [])
            collector.rpc_call("eth_getLogs", [{}])
            collector.rpc_call("eth_getBlockByNumber", ["0x1", False])
        out = collector._live1c_finish(r, "SUCCESS", self.store)
        self.assertEqual(post.call_count, 6)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [1, 1, 2])
        self.assertEqual(out["rpc_calls"], 6)
        self.assertEqual(out["rpc_retries"], 3)
        self.assertEqual(out["rpc_methods"]["eth_blockNumber"]["retries"], 1)
        self.assertEqual(out["rpc_methods"]["eth_getLogs"]["retries"], 2)
        self.assertEqual(out["rpc_methods"]["eth_getBlockByNumber"]["retries"], 0)

    def test_live1d_checkpoint_commit_publish_unchanged(self):
        real_commit = self.store.commit_batch
        r = collector._live1c_begin(self.store)
        with patch.object(self.store, "commit_batch", wraps=real_commit) as commit, \
             patch.object(collector, "get_latest_block", return_value=125), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[]), \
             patch.object(collector, "publish_snapshot", return_value=0) as publish:
            collector.scan_once(self.store, {"123": market()})
        out = collector._live1c_finish(r, "SUCCESS", self.store)
        self.assertEqual(self.store.checkpoint()["block_number"], 105)
        self.assertEqual(out["checkpoint_after"], 105)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(publish.call_count, 1)

    def test_live1d_attribution_failure_is_fail_open(self):
        r = collector._live1c_begin(self.store)
        with patch.object(collector, "get_latest_block", return_value=125), \
             patch.object(collector, "get_block_header", side_effect=header), \
             patch.object(collector, "get_order_filled_logs", return_value=[event(101)]), \
             patch.object(collector, "lookup_market", return_value=market()) as lookup, \
             patch.object(collector, "_live1d_note_gamma_miss", side_effect=RuntimeError("diag")), \
             patch.object(collector, "publish_snapshot", return_value=0) as publish:
            collector.scan_once(self.store, {})
        out = collector._live1c_finish(r, "SUCCESS", self.store)
        lookup.assert_called_once_with("123")
        self.assertEqual(self.store.checkpoint()["block_number"], 105)
        self.assertEqual(out["checkpoint_after"], 105)
        self.assertEqual(publish.call_count, 1)
