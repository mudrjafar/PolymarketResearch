import json
import os
import math
import sqlite3
import sys
import time
import threading
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from collector_storage_v4.storage import CollectorStore, StorageError
from collector_storage_v4.bridge import CollectorLock, publish_snapshot

import requests
from eth_abi import decode

RPC_URL = os.getenv("POLYMARKET_RPC_URL", "").strip()
if not RPC_URL:
    raise SystemExit("Missing POLYMARKET_RPC_URL environment variable.")

GAMMA_API = "https://gamma-api.polymarket.com"

# Polymarket CTF Exchange V2 + NegRisk CTF Exchange V2 (Polygon).
EXCHANGES = [
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xe2222d279d744050d28e00520010520000310F59",
]

# V2: OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"

CHECK_INTERVAL = 1.5
MARKET_LIMIT = 100
MARKET_REFRESH_INTERVAL = 300
LIVE_TRADES_FILE = BASE / "data/live_trades.jsonl"
DATABASE_FILE = BASE / "data/collector.sqlite3"
COLLECTOR_VERSION = 4
CONFIRMATION_BLOCKS = int(os.getenv('COLLECTOR_CONFIRMATION_BLOCKS', '20'))
INITIAL_LOOKBACK_BLOCKS = int(os.getenv('COLLECTOR_INITIAL_LOOKBACK_BLOCKS', '500'))
START_BLOCK = os.getenv('COLLECTOR_START_BLOCK', '').strip()
if CONFIRMATION_BLOCKS < 1 or INITIAL_LOOKBACK_BLOCKS < 1:
    raise SystemExit('Collector confirmation depth and initial lookback must be positive')


class RetryableCollectorError(RuntimeError):
    pass


class FatalConfigError(RuntimeError):
    pass


BATCH_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


def _http_status(exc):
    return getattr(getattr(exc, 'response', None), 'status_code', None)


def _sqlite_busy(exc):
    code = getattr(exc, 'sqlite_errorcode', None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _checkpoint_block(store):
    checkpoint = store.checkpoint()
    return None if checkpoint is None else checkpoint['block_number']


def rpc_call(method, params):
    last_exc = None
    for attempt in range(1, 4):
        try:
            r = _live1c_rpc_post(RPC_URL, json={"jsonrpc":"2.0","method":method,"params":params,"id":1}, timeout=30)
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict) or ("result" not in payload and "error" not in payload):
                raise RetryableCollectorError(f"RPC {method} returned an invalid response")
            if "error" in payload:
                error = payload.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if code in (-32000, -32603):
                    raise RetryableCollectorError(
                        f"RPC {method} returned a temporary provider error ({code})"
                    )
                raise RuntimeError(
                    f"RPC {method} returned a fatal JSON-RPC error ({code})"
                )
            return payload["result"]
        except (RetryableCollectorError, FatalConfigError):
            raise
        except requests.HTTPError as exc:
            status = _http_status(exc)
            if status in (401, 403):
                raise FatalConfigError(f"RPC {method} authentication/authorization failed (HTTP {status})") from None
            if status != 429 and not (isinstance(status, int) and 500 <= status <= 599):
                raise RuntimeError(f"RPC {method} returned non-retryable HTTP {status}") from None
            last_exc = exc
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
        if attempt < 3:
            print(f"[COLLECTOR] RPC {method} retry {attempt}/3 after {type(last_exc).__name__}")
            time.sleep(attempt)
    raise RetryableCollectorError(
        f"RPC {method} request failed after 3 attempts ({type(last_exc).__name__})"
    ) from None

def get_latest_block():
    return int(rpc_call("eth_blockNumber", []), 16)


def get_block_header(number):
    result = rpc_call('eth_getBlockByNumber', [hex(number), False])
    if not result:
        raise RetryableCollectorError('RPC block header is unavailable')
    if int(result['number'], 16) != number:
        raise StorageError('RPC block header number mismatch')
    return {'block_number': number, 'block_hash': result['hash'],
            'parent_hash': result['parentHash'],
            'block_timestamp': int(result['timestamp'], 16)}


def get_order_filled_logs(from_block, to_block):
    def fetch_exchange(exchange):
        result = rpc_call("eth_getLogs", [{
            "address": exchange,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": [ORDER_FILLED_TOPIC],
        }])
        if not isinstance(result, list):
            raise RetryableCollectorError("RPC log response must be a list")
        for log in result:
            if log.get("address", "").lower() != exchange.lower():
                raise StorageError("RPC returned a log from an unexpected exchange")
            log["_exchange_address"] = exchange
        return result

    with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(fetch_exchange, EXCHANGES))
    logs = [log for batch in batches for log in batch]
    logs.sort(key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))
    return logs

def get_active_markets():
    """Load active markets with Gamma keyset pagination.

    Legacy offset pagination now hard-stops around offset 2000. The current
    keyset endpoint continues with `after_cursor`. A bounded legacy fallback
    prevents a temporary keyset failure from killing the whole machine.
    """
    all_markets=[]
    seen_market_ids=set()
    seen_cursors=set()
    after_cursor=None

    try:
        for _page in range(100):
            params={
                "limit": MARKET_LIMIT,
                "active": True,
                "closed": False,
            }
            if after_cursor:
                params["after_cursor"]=after_cursor

            r=requests.get(
                f"{GAMMA_API}/markets/keyset",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            payload=r.json()

            if isinstance(payload, dict):
                markets=payload.get("markets", [])
                next_cursor=payload.get("next_cursor") or payload.get("nextCursor")
            elif isinstance(payload, list):
                markets=payload
                next_cursor=None
            else:
                markets=[]
                next_cursor=None

            if not markets:
                break

            for market in markets:
                market_id=str(market.get("id", ""))
                dedupe_key=market_id or str(market.get("conditionId", ""))
                if dedupe_key and dedupe_key in seen_market_ids:
                    continue
                if dedupe_key:
                    seen_market_ids.add(dedupe_key)
                all_markets.append(market)

            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                print("[COLLECTOR] Keyset cursor repeated; stopping pagination safely.")
                break

            seen_cursors.add(next_cursor)
            after_cursor=next_cursor

        if all_markets:
            print(f"[COLLECTOR] Gamma keyset discovery loaded {len(all_markets)} active markets.")
            return all_markets

    except requests.RequestException as exc:
        print(
            "[COLLECTOR] Keyset market discovery warning: "
            f"{exc}. Falling back to bounded offset pagination."
        )

    all_markets=[]
    seen_market_ids=set()

    for offset in range(0, 2001, MARKET_LIMIT):
        r=requests.get(
            f"{GAMMA_API}/markets",
            params={
                "limit": MARKET_LIMIT,
                "offset": offset,
                "active": True,
                "closed": False,
            },
            timeout=30,
        )

        if r.status_code == 422:
            print(
                f"[COLLECTOR] Gamma offset limit reached at {offset}; "
                "using markets collected so far."
            )
            break

        r.raise_for_status()
        markets=r.json()
        if not markets:
            break

        for market in markets:
            market_id=str(market.get("id", ""))
            dedupe_key=market_id or str(market.get("conditionId", ""))
            if dedupe_key and dedupe_key in seen_market_ids:
                continue
            if dedupe_key:
                seen_market_ids.add(dedupe_key)
            all_markets.append(market)

        if len(markets) < MARKET_LIMIT:
            break

    return all_markets


def parse_json_field(value, default):
    if isinstance(value, list): return value
    if value is None: return default
    try: return json.loads(value)
    except (json.JSONDecodeError, TypeError): return default


class GammaIdentityConflict(StorageError):
    """A Gamma token contradicts a previously validated canonical identity."""


def _gamma_identity(token_id, market):
    token = str(token_id).strip()
    if not token.isascii() or not token.isdecimal() or not isinstance(market, dict):
        raise StorageError('Gamma token identity is unavailable or invalid')
    condition = market.get('condition_id')
    outcome = market.get('outcome')
    market_id = market.get('market_id')
    if not isinstance(condition, str) or not isinstance(outcome, str) or not outcome.strip():
        raise StorageError('Gamma condition/outcome identity is required')
    condition = condition.strip().lower()
    if (len(condition) != 66 or not condition.startswith('0x')
            or any(c not in '0123456789abcdef' for c in condition[2:])):
        raise StorageError('Gamma condition identity is invalid')
    return {'token_id': token, 'market_id': market_id,
            'condition_id': condition, 'outcome': outcome.strip()}


def _check_gamma_identity(known, identity):
    if known is not None and (
        known['token_id'] != identity['token_id']
        or known['condition_id'] != identity['condition_id']
        or known['outcome'].casefold() != identity['outcome'].casefold()
    ):
        # market_id is provenance, not a canonical contradiction by itself.
        raise GammaIdentityConflict('Gamma token condition/outcome identity conflict')


def _fresh_gamma_entry(identity, market):
    entry = deepcopy(market)
    # Provenance is optional; normalization may safely emit an unknown value.
    entry.setdefault('market_id', None)
    entry['condition_id'] = identity['condition_id']
    entry['outcome'] = identity['outcome']
    return entry


class GammaTokenMap(dict):
    """Process-local identity ledger; only learned fallback tokens are retained.

    A new collector process starts with no learned fallbacks. The ledgers hold
    stable identity only, never mutable market state. No persistence or eviction.
    """
    def __init__(self, active_map):
        super().__init__()
        self.known_identities = {}
        self.fallback_identities = {}
        for token, market in active_map.items():
            identity = _gamma_identity(token, market)
            key = identity['token_id']
            _check_gamma_identity(self.known_identities.get(key), identity)
            self.known_identities[key] = dict(identity)
            self[key] = _fresh_gamma_entry(identity, market)

    def remember_fallback(self, token, market):
        identity = _gamma_identity(token, market)
        key = identity['token_id']
        _check_gamma_identity(self.known_identities.get(key), identity)
        entry = _fresh_gamma_entry(identity, market)
        self.known_identities[key] = dict(identity)
        self.fallback_identities[key] = dict(identity)
        self[key] = entry
        return entry


def merge_gamma_refresh(previous, fresh_active):
    # Build and validate before changing the caller's mapping or identity ledger.
    merged = GammaTokenMap(fresh_active)
    for token, identity in merged.known_identities.items():
        _check_gamma_identity(previous.known_identities.get(token), identity)
    merged.known_identities = {k: dict(v) for k, v in previous.known_identities.items()} | merged.known_identities
    retained = 0
    for token in previous.fallback_identities:
        identity = dict(merged.known_identities[token])
        merged.fallback_identities[token] = dict(identity)
        if token not in merged:
            # No fresh confirmation: every mutable field is unknown. Do not copy
            # the old market dictionary, including historical closed/open flags.
            merged[token] = dict(identity, question=None, active=None, closed=None,
                                 accepting_orders=None, price=None, volume=None,
                                 volume24hr=None, start_date=None, end_date=None)
            retained += 1
    try:
        print(f'[LIVE-1E] active_token_mappings={len(merged)-retained} '
              f'retained_fallback_identities={retained} total_mapping_size={len(merged)} '
              f'learned_fallback_identities={len(merged.fallback_identities)} '
              f'known_identity_count={len(merged.known_identities)}')
    except Exception:
        pass
    return merged


def build_token_map(markets):
    token_map={}
    for market in markets:
        token_ids=parse_json_field(market.get("clobTokenIds"), [])
        outcomes=parse_json_field(market.get("outcomes"), [])
        prices=parse_json_field(market.get("outcomePrices"), [])
        for i, token_id in enumerate(token_ids):
            token = str(token_id).strip()
            entry={
                "market_id": market.get("id"),
                "condition_id": market.get("conditionId"),
                "question": market.get("question"),
                "outcome": outcomes[i] if i < len(outcomes) else None,
                "price": prices[i] if i < len(prices) else None,
                "volume24hr": market.get("volume24hr"),
                "volume": market.get("volume"),
                "end_date": market.get("endDate"),
                "start_date": market.get("startDate"),
                "closed": market.get("closed"),
                "active": market.get("active"),
                "accepting_orders": market.get("acceptingOrders"),
            }
            if token in token_map:
                _check_gamma_identity(_gamma_identity(token, token_map[token]),
                                      _gamma_identity(token, entry))
            token_map[token] = entry
    return token_map


def decode_order_filled(log):
    data=bytes.fromhex(log["data"][2:])
    decoded=decode(["uint256","uint256","uint256","uint256","uint256","bytes32","bytes32"], data)
    topics=log["topics"]
    if len(topics) != 4 or topics[0].lower() != ORDER_FILLED_TOPIC:
        raise RuntimeError('Unexpected OrderFilled event topics')
    return {
        "order_hash": topics[1],
        "maker": "0x" + topics[2][-40:],
        "taker": "0x" + topics[3][-40:],
        "side": int(decoded[0]),
        "token_id": str(decoded[1]),
        "maker_amount": int(decoded[2]),
        "taker_amount": int(decoded[3]),
        "fee": int(decoded[4]),
    }


def calculate_trade_metrics(trade):
    """Normalize V2 OrderFilled amounts into USD, token shares and price.

    V2 BUY:  makerAmount = collateral/USD, takerAmount = outcome shares.
    V2 SELL: makerAmount = outcome shares, takerAmount = collateral/USD.
    """
    maker_amount=trade["maker_amount"] / 1_000_000
    taker_amount=trade["taker_amount"] / 1_000_000
    side=trade["side"]

    if side == 0:  # BUY
        side_label="BUY"
        trade_usd=maker_amount
        token_amount=taker_amount
    elif side == 1:  # SELL
        side_label="SELL"
        token_amount=maker_amount
        trade_usd=taker_amount
    else:
        return {"maker_amount":maker_amount,"taker_amount":taker_amount,"fill_price":None,"trade_usd":None,"token_amount":None,"side_label":"UNKNOWN"}

    fill_price=(trade_usd/token_amount) if token_amount and token_amount > 0 else None
    return {
        "maker_amount": maker_amount,
        "taker_amount": taker_amount,
        "fill_price": fill_price,
        "trade_usd": trade_usd,
        "token_amount": token_amount,
        "side_label": side_label,
    }


def safe_float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError): return None


# LIVE-1F.1: diagnostic-only process lifetime, activated by existing batch scope.
# Samples are emitted once in their batch record, never as per-token console lines.
MAX_LOOKUP_DIAGNOSTIC_SAMPLES = 4096
_LIVE1F_STATE = None


def _live1f_reset():
    global _LIVE1F_STATE
    _LIVE1F_STATE = {'sequence': 0, 'sample_count': 0, 'overflow': 0,
                    'seen': set(), 'seen_overflow': 0, 'relookups': 0,
                    'complete': True, 'errors': 0}


def _live1f_safe(function, *args):
    try:
        return function(*args)
    except Exception:
        # BaseException controls deliberately propagate, as in frozen finish.
        try:
            if isinstance(_LIVE1F_STATE, dict):
                _LIVE1F_STATE['complete'] = False
                _LIVE1F_STATE['errors'] += 1
        except Exception:
            pass
        return None


def _live1f_now():
    return _live1f_safe(time.perf_counter)


def _live1f_ms(start, end):
    if start is None or end is None:
        return None
    return max(0.0, (end - start) * 1000.0)


def _live1f_start():
    if _LIVE1C_CURRENT is None:
        return None
    if _LIVE1F_STATE is None:
        _live1f_reset()
    _LIVE1F_STATE['sequence'] += 1
    return {'lookup_sequence': _LIVE1F_STATE['sequence'], '_started': _live1f_now(),
            'resolved_stage': 'FAILED', 'resolved': False, 'failure_class': None,
            'open_attempted': False, 'closed_attempted': False,
            'api_request_count': 0, 'open_request_count': 0, 'closed_request_count': 0,
            'lookup_total_ms': None, 'open_stage_ms': 0.0, 'closed_stage_ms': 0.0,
            'open_request_ms': 0.0, 'closed_request_ms': 0.0,
            'local_processing_ms': 0.0, 'open_response_elapsed_ms': None,
            'closed_response_elapsed_ms': None, 'request_shapes': []}


def _live1f_failure(exc, phase):
    if isinstance(exc, requests.Timeout): return 'REQUEST_TIMEOUT'
    if isinstance(exc, requests.ConnectionError): return 'REQUEST_CONNECTION'
    if isinstance(exc, requests.HTTPError):
        status = _http_status(exc)
        if status == 429: return 'HTTP_429'
        if isinstance(status, int) and 500 <= status <= 599: return 'HTTP_5XX'
        if isinstance(status, int) and 400 <= status <= 499: return 'HTTP_4XX'
        return 'HTTP_OTHER' if status is not None else 'REQUEST_OTHER'
    if phase == 'json' and isinstance(exc, ValueError): return 'INVALID_JSON'
    if phase == 'shape': return 'INVALID_SHAPE'
    if phase == 'not_found': return 'NOT_FOUND'
    if isinstance(exc, StorageError): return 'IDENTITY_ERROR'
    if isinstance(exc, requests.RequestException): return 'REQUEST_OTHER'
    return 'OTHER_EXISTING_FAILURE'


def _live1f_event(sample, event, closed, value=None):
    if sample is None: return
    prefix = 'closed' if closed else 'open'
    stage = 'CLOSED_FALLBACK' if closed else 'OPEN'
    if event == 'stage_start':
        sample[prefix + '_attempted'] = True
        sample['_stage_started'] = _live1f_now()
    elif event == 'request_start':
        sample[prefix + '_request_count'] += 1
        sample['api_request_count'] += 1
        sample['request_shapes'].append({'stage': stage, 'endpoint': '/markets',
            'request_ordinal': 1, 'limit': 100, 'result_count': None,
            'potential_truncation_signal': False})
        sample['_request_started'] = _live1f_now()
    elif event == 'request_end':
        sample[prefix + '_request_ms'] = _live1f_ms(sample.get('_request_started'), _live1f_now())
    elif event == 'response':
        # Requests-provided elapsed only; no DNS/TCP/TLS interpretation.
        elapsed = getattr(value, 'elapsed', None)
        if elapsed is not None:
            try:
                ms = elapsed.total_seconds() * 1000.0
                if isinstance(ms, (int, float)) and math.isfinite(ms) and ms >= 0:
                    sample[prefix + '_response_elapsed_ms'] = ms
            except Exception:
                pass
    elif event == 'local_start': sample['_local_started'] = _live1f_now()
    elif event == 'local_end':
        elapsed = _live1f_ms(sample.get('_local_started'), _live1f_now())
        if elapsed is None or sample['local_processing_ms'] is None:
            sample['local_processing_ms'] = None
        else: sample['local_processing_ms'] += elapsed
    elif event == 'stage_end':
        sample[prefix + '_stage_ms'] = _live1f_ms(sample.get('_stage_started'), _live1f_now())
    elif event == 'lookup_end':
        sample['lookup_total_ms'] = _live1f_ms(sample.get('_started'), _live1f_now())
    elif event == 'phase': sample['_phase'] = value
    elif event == 'result_count':
        sample['request_shapes'][-1]['result_count'] = value
        sample['request_shapes'][-1]['potential_truncation_signal'] = value >= 100
    elif event == 'resolved': sample.update(resolved=True, resolved_stage=stage)
    elif event == 'failure':
        sample['failure_class'] = _live1f_failure(value, sample.get('_phase'))
        if sample['failure_class'] in ('INVALID_SHAPE', 'INVALID_JSON', 'IDENTITY_ERROR'):
            sample['resolved_stage'] = 'MALFORMED_OR_AMBIGUOUS'


def _live1f_observe(sample, token_id):
    if sample is None or _LIVE1C_CURRENT is None: return
    state = _LIVE1F_STATE
    batch = _LIVE1C_CURRENT.setdefault('_live1f', {'samples': [], 'counts': {}, 'totals': {},
                                                'failures': {}, 'invalid_timing': 0})
    counts, totals = batch['counts'], batch['totals']
    def count(name, n=1): counts[name] = counts.get(name, 0) + n
    count('lookup_count')
    count('lookup_open_resolved_count', int(sample['resolved_stage'] == 'OPEN'))
    count('lookup_closed_resolved_count', int(sample['resolved_stage'] == 'CLOSED_FALLBACK'))
    count('lookup_failed_count', int(not sample['resolved']))
    count('open_lookup_attempts', int(sample['open_attempted']))
    count('closed_fallback_invocations', int(sample['closed_attempted']))
    count('open_unresolved_count', int(sample['open_attempted'] and sample['resolved_stage'] != 'OPEN'))
    count('closed_unresolved_count', int(sample['closed_attempted'] and sample['resolved_stage'] != 'CLOSED_FALLBACK'))
    for output, source in [('lookup_request_count','api_request_count'),
                           ('lookup_open_request_count','open_request_count'),
                           ('lookup_closed_request_count','closed_request_count')]:
        count(output, sample[source])
    for output, source in [('lookup_timing_sum_ms','lookup_total_ms'),
                           ('lookup_open_sum_ms','open_stage_ms'),
                           ('lookup_closed_sum_ms','closed_stage_ms'),
                           ('local_processing_sum_ms','local_processing_ms'),
                           ('open_request_sum_ms','open_request_ms'),
                           ('closed_request_sum_ms','closed_request_ms')]:
        value = sample[source]
        if value is None: batch['invalid_timing'] += 1
        else: totals[output] = totals.get(output, 0.0) + value
    if sample['resolved_stage'] == 'CLOSED_FALLBACK':
        totals['open_ms_spent_on_tokens_resolved_by_closed'] = (
            totals.get('open_ms_spent_on_tokens_resolved_by_closed', 0.0) + (sample['open_stage_ms'] or 0.0))
    failure = sample['failure_class']
    if failure: batch['failures'][failure] = batch['failures'].get(failure, 0) + 1
    # Fixed-size digests bound both cardinality and token storage. Diagnostic only.
    token = hashlib.sha256(str(token_id).encode('utf-8')).digest()
    if token in state['seen']: state['relookups'] += 1
    elif sample['resolved']:
        if len(state['seen']) < MAX_LOOKUP_DIAGNOSTIC_SAMPLES: state['seen'].add(token)
        else:
            state['seen_overflow'] += 1
            state['complete'] = False
    if state['sample_count'] < MAX_LOOKUP_DIAGNOSTIC_SAMPLES:
        batch['samples'].append({k:v for k,v in sample.items() if not k.startswith('_')})
        state['sample_count'] += 1
    else: state['overflow'] += 1


def _live1f_percentiles(values):
    ordered = sorted(v for v in values if v is not None)
    n = len(ordered)
    return {'sample_count': n, 'method': 'NEAREST_RANK',
            **{f'p{p}': ordered[max(0, (p * n + 99) // 100 - 1)] if n else None
               for p in (50, 90, 95, 99)}, 'max': ordered[-1] if n else None}


def _live1f_summary(record):
    state = _LIVE1F_STATE or {'sample_count':0, 'overflow':0, 'seen_overflow':0,
                            'complete':True, 'relookups':0, 'errors':0}
    batch = record.get('_live1f', {'samples':[], 'counts':{}, 'totals':{}, 'failures':{}, 'invalid_timing':0})
    result = {name: batch['counts'].get(name, 0) for name in (
        'lookup_count', 'lookup_open_resolved_count', 'lookup_closed_resolved_count',
        'lookup_failed_count', 'lookup_request_count', 'lookup_open_request_count',
        'lookup_closed_request_count', 'open_lookup_attempts', 'closed_fallback_invocations',
        'open_unresolved_count', 'closed_unresolved_count')}
    result.update({name: batch['totals'].get(name, 0.0) for name in (
        'lookup_timing_sum_ms', 'lookup_open_sum_ms', 'lookup_closed_sum_ms',
        'local_processing_sum_ms', 'open_request_sum_ms', 'closed_request_sum_ms',
        'open_ms_spent_on_tokens_resolved_by_closed')})
    existing = record.get('gamma_lookup_ms', 0.0)
    delta = result['lookup_timing_sum_ms'] - existing
    count = result['lookup_closed_resolved_count']
    result.update(existing_gamma_lookup_ms=existing, lookup_timing_delta_ms=delta,
        lookup_timing_consistent=bool(not batch['invalid_timing'] and abs(delta) <= max(1.0, abs(existing)*0.005)),
        invalid_timing_count=batch['invalid_timing'], diagnostic_error_count=state['errors'],
        diagnostic_sample_count=state['sample_count'], diagnostic_sample_overflow_count=state['overflow'],
        batch_sample_count=len(batch['samples']), samples=batch['samples'], failure_classes=batch['failures'],
        successful_token_relookup_count=state['relookups'] if state['complete'] else None,
        successful_token_relookup_observed_count=state['relookups'],
        successful_token_observation_complete=state['complete'],
        successful_token_observation_overflow_count=state['seen_overflow'],
        successful_token_observed_count=len(state.get('seen', ())),
        count_tokens_resolved_by_closed=count,
        average_open_ms_before_closed_resolution=result['open_ms_spent_on_tokens_resolved_by_closed']/count if count else None,
        lookup_stage_page_loop_present=False, pagination_observed=False,
        current_client_batches_tokens=False, persistent_session_explicitly_used=False,
        percentile_population_complete=state['overflow'] == 0 and state['errors'] == 0 and not batch['invalid_timing'])
    for key in ('checkpoint_before', 'safe_tip', 'from_block', 'to_block'):
        result[key] = record.get(key)
    result['lag_before'] = (record['safe_tip'] - record['checkpoint_before']
        if record.get('safe_tip') is not None and record.get('checkpoint_before') is not None else None)
    samples = batch['samples']
    populations = {'lookup': [s['lookup_total_ms'] for s in samples],
        'open': [s['open_stage_ms'] for s in samples if s['open_attempted']],
        'closed': [s['closed_stage_ms'] for s in samples if s['closed_attempted']],
        'local': [s['local_processing_ms'] for s in samples],
        'requests_per_lookup': [s['api_request_count'] for s in samples],
        'open_before_closed': [s['open_stage_ms'] for s in samples if s['resolved_stage']=='CLOSED_FALLBACK']}
    for stage in ('OPEN', 'CLOSED_FALLBACK', 'FAILED', 'MALFORMED_OR_AMBIGUOUS'):
        populations[stage] = [s['lookup_total_ms'] for s in samples if s['resolved_stage']==stage]
    result['batch_percentiles'] = {key: _live1f_percentiles(values) for key, values in populations.items()}
    return result


def _live1f_attach(record):
    record['gamma_lookup_diagnostics'] = _live1f_summary(record)


def lookup_market(token_id):
    # Explicitly try open and closed markets: downtime can span market closure.
    diagnostic = _live1f_safe(_live1f_start)
    try:
        for closed in (False, True):
            _live1f_safe(_live1f_event, diagnostic, 'stage_start', closed)
            try:
                _live1f_safe(_live1f_event, diagnostic, 'request_start', closed)
                try:
                    response = requests.get(f'{GAMMA_API}/markets',
                        params={'clob_token_ids': [token_id], 'closed': closed, 'limit': 100}, timeout=30)
                finally:
                    _live1f_safe(_live1f_event, diagnostic, 'request_end', closed)
                _live1f_safe(_live1f_event, diagnostic, 'response', closed, response)
                response.raise_for_status()
                _live1f_safe(_live1f_event, diagnostic, 'local_start', closed)
                try:
                    _live1f_safe(_live1f_event, diagnostic, 'phase', closed, 'json')
                    markets = response.json()
                    _live1f_safe(_live1f_event, diagnostic, 'phase', closed, 'shape')
                    if not isinstance(markets, list):
                        raise RuntimeError('Gamma token lookup returned an invalid response')
                    _live1f_safe(_live1f_event, diagnostic, 'result_count', closed, len(markets))
                    _live1f_safe(_live1f_event, diagnostic, 'phase', closed, 'mapping')
                    market = build_token_map(markets).get(token_id)
                    if market:
                        _live1f_safe(_live1f_event, diagnostic, 'resolved', closed)
                        return market
                finally:
                    _live1f_safe(_live1f_event, diagnostic, 'local_end', closed)
            finally:
                _live1f_safe(_live1f_event, diagnostic, 'stage_end', closed)
        _live1f_safe(_live1f_event, diagnostic, 'phase', False, 'not_found')
        raise RuntimeError('Aggressor token metadata unavailable; batch will be retried')
    except Exception as exc:
        _live1f_safe(_live1f_event, diagnostic, 'failure', False, exc)
        raise
    finally:
        _live1f_safe(_live1f_event, diagnostic, 'lookup_end', False)
        _live1f_safe(_live1f_observe, diagnostic, token_id)


def load_market_map():
    markets=get_active_markets()
    token_map=build_token_map(markets)
    print(f"[COLLECTOR] Active markets: {len(markets)} | token mappings: {len(token_map)}")
    return token_map


def normalize_trade(log, trade, market, header):
    metrics = calculate_trade_metrics(trade)
    if metrics['side_label'] == 'UNKNOWN' or not metrics['token_amount'] or not (0 < metrics['fill_price'] < 1):
        raise RuntimeError('Invalid decoded trade metrics; batch will be retried')
    exchange = log['_exchange_address']
    trade_usd = metrics['trade_usd']
    volume24hr = safe_float(market.get('volume24hr'))
    ratio = trade_usd / volume24hr if volume24hr and volume24hr > 0 else None
    saved={
        "collector_version": COLLECTOR_VERSION,
        "flow_role": "TAKER_AGGRESSOR",
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "block": int(log["blockNumber"],16),
        "transaction_hash": log["transactionHash"],
        "log_index": int(log["logIndex"],16),
        "exchange_address": exchange,
        "market_id": market["market_id"],
        "condition_id": market["condition_id"],
        "question": market["question"],
        "outcome": market["outcome"],
        "start_date": market.get("start_date"),
        "end_date": market.get("end_date"),
        "active": market.get("active"),
        "closed": market.get("closed"),
        "accepting_orders": market.get("accepting_orders"),
        "price": market.get("price"),
        "volume24hr": volume24hr,
        "volume": market.get("volume"),
        "token_id": trade["token_id"],
        "order_hash": trade["order_hash"],
        "maker": trade["maker"],
        "taker": trade["taker"],
        "side": trade["side"],
        "side_label": metrics["side_label"],
        "maker_amount_raw": trade["maker_amount"],
        "taker_amount_raw": trade["taker_amount"],
        "maker_amount": metrics["maker_amount"],
        "taker_amount": metrics["taker_amount"],
        "token_amount": metrics["token_amount"],
        "fill_price": metrics["fill_price"],
        "trade_usd": trade_usd,
        "trade_to_volume_ratio": ratio,
        "fee": trade["fee"],
    }
    saved['block_hash'] = log['blockHash']
    saved['block_timestamp'] = header['block_timestamp']
    return saved


def initialize_store(store, latest_block):
    checkpoint = store.checkpoint()
    if checkpoint is None:
        safe_tip = latest_block - CONFIRMATION_BLOCKS
        if safe_tip < 1:
            raise RuntimeError('Not enough confirmed blocks to initialize collector')
        try:
            start = int(START_BLOCK) if START_BLOCK else max(1, safe_tip - INITIAL_LOOKBACK_BLOCKS + 1)
        except ValueError:
            raise FatalConfigError('COLLECTOR_START_BLOCK must be an integer') from None
        if start < 1 or start > safe_tip:
            raise FatalConfigError('COLLECTOR_START_BLOCK must be within confirmed chain history')
        anchor = get_block_header(start - 1)
        store.initialize(start - 1, anchor['block_hash'])
        checkpoint = store.checkpoint()
        print(f"[COLLECTOR] First-run range begins at block {start}")
    current = get_block_header(checkpoint['block_number'])
    if current['block_hash'].lower() != checkpoint['block_hash']:
        raise StorageError('Checkpoint hash changed; manual chain-reorganization recovery required')
    return checkpoint


def scan_once(store, token_map):
    # Repair a failed/missed JSON publication BEFORE accepting another batch.
    checkpoint = store.checkpoint()
    safe_tip = get_latest_block() - CONFIRMATION_BLOCKS
    if safe_tip <= checkpoint['block_number']:
        return 0
    from_block = checkpoint['block_number'] + 1
    to_block = min(from_block + 4, safe_tip)
    with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=5) as pool:
        headers = list(pool.map(get_block_header, range(from_block, to_block + 1)))
    by_number = {header['block_number']: header for header in headers}
    # get_order_filled_logs returns only after BOTH exchanges succeed.
    logs = get_order_filled_logs(from_block, to_block)
    rows = []
    for log in logs:
        n = int(log['blockNumber'], 16)
        header = by_number.get(n)
        if header is None or log.get('removed') or log['blockHash'].lower() != header['block_hash'].lower():
            raise StorageError('Log does not match the requested canonical block range')
        trade = decode_order_filled(log)
        exchange = log['_exchange_address']
        if trade['taker'].lower() != exchange.lower():
            continue
        market = token_map.get(trade['token_id'])
        if market is None:
            market = lookup_market(trade['token_id'])
            if isinstance(token_map, GammaTokenMap):
                market = token_map.remember_fallback(trade['token_id'], market)
            else:
                # Preserve the dict API for callers while never caching failures.
                identity = _gamma_identity(trade['token_id'], market)
                market = _fresh_gamma_entry(identity, market)
                token_map[trade['token_id']] = market
        rows.append(normalize_trade(log, trade, market, header))
    # Recheck the final header after RPC/metadata calls to detect a changed range.
    if get_block_header(to_block)['block_hash'].lower() != headers[-1]['block_hash'].lower():
        raise StorageError('Block range changed while scanning; refusing to commit')
    inserted = store.commit_batch(headers, rows)
    publish_snapshot(store, LIVE_TRADES_FILE)
    age_min = max(0.0, (time.time() - headers[-1]['block_timestamp']) / 60.0)
    print(f'[COLLECTOR] Committed blocks {from_block}-{to_block}: {inserted} new trades - DATA AGE: {age_min:.1f} min')
    return inserted


def main():
    print('[COLLECTOR] SQLite checkpoint + block timestamps + JSON compatibility view')
    try:
        with CollectorLock(DATABASE_FILE.with_suffix('.lock')):
            if int(rpc_call('eth_chainId', []), 16) != 137:
                raise FatalConfigError('Configured RPC is not Polygon chain 137')
            with CollectorStore(DATABASE_FILE) as store:
                checkpoint = initialize_store(store, get_latest_block())
                publish_snapshot(store, LIVE_TRADES_FILE)
                print(f"[COLLECTOR] Resume after block {checkpoint['block_number']}")
                token_map = GammaTokenMap(load_market_map())
                last_market_refresh = time.time()
                retry_index = 0

                while True:
                    before_checkpoint = _checkpoint_block(store)
                    try:
                        if time.time() - last_market_refresh >= MARKET_REFRESH_INTERVAL:
                            token_map = merge_gamma_refresh(token_map, load_market_map())
                            last_market_refresh = time.time()

                        scan_once(store, token_map)
                        after_checkpoint = _checkpoint_block(store)

                        if (before_checkpoint is not None and after_checkpoint is not None
                                and after_checkpoint > before_checkpoint):
                            retry_index = 0

                        time.sleep(CHECK_INTERVAL)

                    except StorageError:
                        raise
                    except FatalConfigError:
                        raise
                    except RetryableCollectorError as exc:
                        delay = BATCH_BACKOFF_SECONDS[min(retry_index, len(BATCH_BACKOFF_SECONDS) - 1)]
                        retry_index += 1
                        print(f'[COLLECTOR] RETRYABLE_BATCH ({type(exc).__name__}); retrying in {delay}s')
                        time.sleep(delay)
                    except sqlite3.OperationalError as exc:
                        if not _sqlite_busy(exc):
                            raise
                        delay = BATCH_BACKOFF_SECONDS[min(retry_index, len(BATCH_BACKOFF_SECONDS) - 1)]
                        retry_index += 1
                        print(f'[COLLECTOR] RETRYABLE_BATCH (SQLite busy/locked); retrying in {delay}s')
                        time.sleep(delay)
                    except requests.RequestException as exc:
                        status = _http_status(exc)
                        if status in (401, 403):
                            raise FatalConfigError(
                                f'HTTP {status} authentication/authorization failure'
                            ) from None
                        retryable = (
                            isinstance(exc, (requests.Timeout, requests.ConnectionError))
                            or status == 429
                            or (isinstance(status, int) and 500 <= status <= 599)
                        )
                        if not retryable:
                            raise
                        delay = BATCH_BACKOFF_SECONDS[min(retry_index, len(BATCH_BACKOFF_SECONDS) - 1)]
                        retry_index += 1
                        print(f'[COLLECTOR] RETRYABLE_BATCH ({type(exc).__name__}); retrying in {delay}s')
                        time.sleep(delay)
                    except Exception as exc:
                        after_checkpoint = _checkpoint_block(store)
                        if (before_checkpoint is not None and after_checkpoint is not None
                                and after_checkpoint > before_checkpoint):
                            retry_index = 0
                            print(f'[COLLECTOR] LOCAL_DEGRADE ({type(exc).__name__}); core batch committed')
                            time.sleep(CHECK_INTERVAL)
                            continue
                        raise

    except KeyboardInterrupt:
        print('[COLLECTOR] Stopped')
    except FatalConfigError as exc:
        print(f'[COLLECTOR] Fatal configuration: {exc}')
        raise SystemExit(1) from None
    except StorageError as exc:
        print(f'[COLLECTOR] Storage/chain history conflict: {exc}')
        raise SystemExit(1) from None
    except RuntimeError as exc:
        print(f'[COLLECTOR] RuntimeError: {exc}')
        raise SystemExit(1) from None
    except Exception as exc:
        print(
            f'[COLLECTOR] Stopped safely ({type(exc).__name__}); '
            'check connectivity, configuration or stored chain history'
        )
        raise SystemExit(1) from None



_LIVE1C_CURRENT=None
_LIVE1C_RPC_LOCAL = threading.local()
_LIVE1C_RPC_LOCK = threading.Lock()

def _live1c_rpc_post(*args, **kwargs):
    # Count at the existing transport boundary; never swap requests.post.
    scope = getattr(_LIVE1C_RPC_LOCAL, "scope", None)
    if scope is not None:
        scope[0] += 1
    return requests.post(*args, **kwargs)

def _live1c_flag(r,f):
    if f and f not in r["flags"]: r["flags"].append(f)

def _live1c_begin(store,outer_retries=0):
    global _LIVE1C_CURRENT
    cp=store.checkpoint() or {}
    n=cp.get("block_number")
    r={"_started":time.perf_counter(),"status":None,"from_block":None,"to_block":None,"blocks":0,
       "safe_tip":None,"checkpoint_before":n,"checkpoint_after":n,"lag_before":0,"lag_after":0,
       "batch_ms":0.0,"blocks_per_second":0.0,"get_logs_ms":0.0,"timestamp_ms":0.0,
       "decode_ms":0.0,"enrichment_ms":0.0,"sqlite_ms":0.0,"compatibility_ms":0.0,
       "compatibility_rows":0,"rpc_calls":0,"rpc_retries":0,"outer_retries":int(outer_retries),
       "backoff_ms":0,"raw_log_count":0,"trade_count":0,"unique_tx_count":0,
       "gamma_lookup_count":0,"gamma_lookup_ms":0.0,"gamma_refresh_ms":0.0,"rpc_ms":0.0,"flags":[]}
    _LIVE1C_CURRENT=r
    return r

def _emit_live1c_record(r):
    print("[LIVE-1C] "+json.dumps({k:v for k,v in r.items() if not k.startswith("_")},
          sort_keys=True,separators=(",",":")))

def _live1c_finish(r,status,store,backoff_ms=0,flag=None):
    global _LIVE1C_CURRENT
    try:
        r["status"] = status
        cp=store.checkpoint() or {}
        r["checkpoint_after"]=cp.get("block_number",r["checkpoint_before"])
        if r["safe_tip"] is None and r.get("_latest") is not None:
            r["safe_tip"]=int(r["_latest"])-CONFIRMATION_BLOCKS
        if r["safe_tip"] is not None and r["checkpoint_before"] is not None:
            r["lag_before"]=max(0,int(r["safe_tip"])-int(r["checkpoint_before"]))
            r["lag_after"]=max(0,int(r["safe_tip"])-int(r["checkpoint_after"]))
        r["backoff_ms"]=max(0,int(backoff_ms));_live1c_flag(r,flag)
        r["batch_ms"]=max(0.0,(time.perf_counter()-r["_started"])*1000.0)
        if r["blocks"] and r["batch_ms"]>0:r["blocks_per_second"]=r["blocks"]/(r["batch_ms"]/1000.0)
        _live1f_safe(_live1f_attach, r)
        _emit_live1c_record(r)
    except Exception:
        pass
    finally:
        if _LIVE1C_CURRENT is r:_LIVE1C_CURRENT=None
    return r

_live1c_rpc_impl=rpc_call
def rpc_call(method,params):
    r=_LIVE1C_CURRENT
    if r is None:return _live1c_rpc_impl(method,params)
    previous = getattr(_LIVE1C_RPC_LOCAL, "scope", None)
    n=[0];t=time.perf_counter()
    _LIVE1C_RPC_LOCAL.scope = n
    try:return _live1c_rpc_impl(method,params)
    finally:
        _LIVE1C_RPC_LOCAL.scope = previous
        try:
            retries = max(0, n[0] - 1)
            elapsed = max(0.0, (time.perf_counter() - t) * 1000.0)
            # Serialize metrics updates only; RPCs and retries remain parallel.
            with _LIVE1C_RPC_LOCK:
                entry = r.setdefault("rpc_methods", {}).setdefault(
                    str(method), {"calls": 0, "retries": 0})
                entry["calls"] += n[0]
                entry["retries"] += retries
                r["rpc_calls"] += n[0]
                r["rpc_retries"] += retries
                r["rpc_ms"] += elapsed
                if retries:_live1c_flag(r,"RPC_RETRY")
        except Exception:
            pass

_live1c_scan_impl=scan_once
def scan_once(store,token_map):
    r=_LIVE1C_CURRENT
    if r is None:return _live1c_scan_impl(store,token_map)
    g=globals();saved={};names=("get_latest_block","get_block_header","get_order_filled_logs","lookup_market","publish_snapshot","decode_order_filled","normalize_trade")
    for n in names:
        if n in g:saved[n]=g[n]
    commit=store.commit_batch
    def latest():
        v=saved["get_latest_block"]();r["_latest"]=v;r["safe_tip"]=int(v)-CONFIRMATION_BLOCKS
        return v
    def hdr(n):
        t=time.perf_counter()
        try:return saved["get_block_header"](n)
        finally:r["timestamp_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def logs(a,b):
        t=time.perf_counter()
        try:
            v=saved["get_order_filled_logs"](a,b);r["raw_log_count"]=len(v);r["from_block"]=int(a);r["to_block"]=int(b);r["blocks"]=max(0,int(b)-int(a)+1);return v
        finally:r["get_logs_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def lookup(token):
        t=time.perf_counter();r["gamma_lookup_count"]+=1;_live1c_flag(r,"TOKEN_LOOKUP")
        try:return saved["lookup_market"](token)
        finally:r["gamma_lookup_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def publish(st,dst):
        t=time.perf_counter()
        try:
            v=saved["publish_snapshot"](st,dst);r["compatibility_rows"]=v;return v
        finally:r["compatibility_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def dec(*a,**k):
        t=time.perf_counter()
        try:return saved["decode_order_filled"](*a,**k)
        finally:r["decode_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def norm(*a,**k):
        t=time.perf_counter()
        try:return saved["normalize_trade"](*a,**k)
        finally:r["enrichment_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    def commit_obs(blocks,trades):
        t=time.perf_counter()
        try:
            v=commit(blocks,trades);r["trade_count"]=len(trades);r["unique_tx_count"]=len({x.get("transaction_hash") or x.get("tx_hash") for x in trades if isinstance(x,dict) and (x.get("transaction_hash") or x.get("tx_hash"))});return v
        finally:r["sqlite_ms"]+=max(0.0,(time.perf_counter()-t)*1000.0)
    g["get_latest_block"]=latest;g["get_block_header"]=hdr;g["get_order_filled_logs"]=logs;g["lookup_market"]=lookup;g["publish_snapshot"]=publish
    if "decode_order_filled" in saved:g["decode_order_filled"]=dec
    g["normalize_trade"]=norm
    store.commit_batch=commit_obs
    try:return _live1c_scan_impl(store,token_map)
    finally:
        store.commit_batch=commit
        for n,v in saved.items():g[n]=v

_live1c_main_impl=main
def main():
    g=globals();scan0=g["scan_once"];refresh0=g["load_market_map"];sleep0=time.sleep
    st={"outer":0,"pending":None,"refresh":None,"startup":True}
    def refresh(*a,**k):
        t=time.perf_counter()
        try:return refresh0(*a,**k)
        finally:
            ms=max(0.0,(time.perf_counter()-t)*1000.0)
            if st["startup"]:st["startup"]=False
            else:st["refresh"]=ms
    def scan(sto,tok):
        r=_live1c_begin(sto,st["outer"])
        if st["refresh"] is not None:r["gamma_refresh_ms"]=st["refresh"];_live1c_flag(r,"GAMMA_REFRESH");st["refresh"]=None
        try:v=scan0(sto,tok)
        except KeyboardInterrupt:
            global _LIVE1C_CURRENT
            _LIVE1C_CURRENT=None
            raise
        except Exception as e:
            if isinstance(e,StorageError) or "Fatal" in type(e).__name__:_live1c_finish(r,"FATAL_FAILURE",sto)
            else:st["pending"]=(r,sto)
            raise
        else:
            _live1c_finish(r,"SUCCESS",sto);st["outer"]=0;return v
    def sleep(sec):
        if st["pending"] is not None:
            r,sto=st["pending"];_live1c_finish(r,"RETRYABLE_FAILURE",sto,backoff_ms=float(sec)*1000.0);st["pending"]=None;st["outer"]+=1
        return sleep0(sec)
    g["scan_once"]=scan;g["load_market_map"]=refresh;time.sleep=sleep
    try:return _live1c_main_impl()
    finally:g["scan_once"]=scan0;g["load_market_map"]=refresh0;time.sleep=sleep0



# ============================================================
# LIVE-1D GAMMA MISS + RPC METHOD ATTRIBUTION
# Instrumentation only. This layer observes existing LIVE-1C
# calls and must not change collector decisions or I/O shape.
# ============================================================

_LIVE1D_MISS_HISTORY = {}
_LIVE1D_REFRESH_GENERATION = 0

def _live1d_market_classification(market_value):
    if not isinstance(market_value, dict):
        return "LOOKUP_FAILED"
    active = market_value.get("active")
    closed = market_value.get("closed")
    accepting = market_value.get(
        "accepting_orders",
        market_value.get("acceptingOrders"),
    )
    if active is True and closed is not True and accepting is not False:
        return "ACTIVE_OPEN"
    return "CLOSED_INACTIVE_HISTORICAL"

def _live1d_note_refresh():
    global _LIVE1D_REFRESH_GENERATION
    _LIVE1D_REFRESH_GENERATION += 1
    return _LIVE1D_REFRESH_GENERATION

def _live1d_note_gamma_miss(record, token_id, market_value, lookup_succeeded=True):
    token = str(token_id)
    history = _LIVE1D_MISS_HISTORY.get(token)
    miss_number = int(history.get("miss_number", 0)) + 1 if history else 1
    generation = int(_LIVE1D_REFRESH_GENERATION)
    classification = (
        _live1d_market_classification(market_value)
        if lookup_succeeded
        else "LOOKUP_FAILED"
    )
    previous_generation = history.get("refresh_generation") if history else None
    previous_classification = (
        history.get("market_classification") if history else None
    )
    repeated = history is not None
    after_refresh = generation > 0
    fallback_lost = bool(
        repeated
        and previous_generation is not None
        and generation > int(previous_generation)
    )
    item = {
        "token_id": token,
        "timestamp": float(time.time()),
        "repeated_miss": repeated,
        "miss_number": miss_number,
        "repeated_miss_count": max(0, miss_number - 1),
        "refresh_generation": generation,
        "after_refresh": after_refresh,
        "fallback_lost_after_refresh": fallback_lost,
        "previous_refresh_generation": previous_generation,
        "previous_market_classification": previous_classification,
        "market_classification": classification,
        "lookup_succeeded": bool(lookup_succeeded),
    }
    record.setdefault("gamma_misses", []).append(item)
    _LIVE1D_MISS_HISTORY[token] = {
        "miss_number": miss_number,
        "refresh_generation": generation,
        "market_classification": classification,
    }
    return item

_live1d_begin_impl = _live1c_begin
def _live1c_begin(store, outer_retries=0):
    record = _live1d_begin_impl(store, outer_retries=outer_retries)
    record.setdefault("gamma_misses", [])
    record.setdefault("rpc_methods", {})
    record.setdefault(
        "live1d_refresh_generation",
        int(_LIVE1D_REFRESH_GENERATION),
    )
    return record

_live1d_scan_impl = scan_once
def scan_once(store, token_map):
    record = _LIVE1C_CURRENT
    if record is None:
        return _live1d_scan_impl(store, token_map)
    original_lookup = globals()["lookup_market"]

    def observed_lookup(token_id):
        try:
            value = original_lookup(token_id)
        except Exception:
            try:
                _live1d_note_gamma_miss(
                    record,
                    token_id,
                    None,
                    lookup_succeeded=False,
                )
            except Exception:
                pass
            raise
        try:
            _live1d_note_gamma_miss(
                record,
                token_id,
                value,
                lookup_succeeded=True,
            )
        except Exception:
            pass
        return value

    globals()["lookup_market"] = observed_lookup
    try:
        return _live1d_scan_impl(store, token_map)
    finally:
        globals()["lookup_market"] = original_lookup

_live1d_main_impl = main
def main():
    original_refresh = globals()["load_market_map"]
    state = {"startup": True}

    def observed_refresh(*args, **kwargs):
        value = original_refresh(*args, **kwargs)
        if state["startup"]:
            state["startup"] = False
        else:
            try:
                _live1d_note_refresh()
            except Exception:
                pass
        return value

    globals()["load_market_map"] = observed_refresh
    try:
        return _live1d_main_impl()
    finally:
        globals()["load_market_map"] = original_refresh


if __name__ == '__main__':
    main()
  
