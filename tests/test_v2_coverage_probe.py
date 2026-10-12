import json

import pytest
from eth_abi import encode

from scripts import v2_coverage_probe as probe


def make_position(module_id, *, arity=0, condition_index=0, outcome_index=0):
    return (
        (module_id << 248)
        | (123 << 120)
        | (arity << 104)
        | (7 << 40)
        | (0 << 24)
        | (condition_index << 8)
        | outcome_index
    )


def make_log(position_id, module_id=1, *, data=None, log_index=None, maker_event=False, side=0):
    data = data or encode(
        ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
        [side, position_id, 100, 200, 1, bytes(32), bytes(32)],
    )
    exchange = probe.EXCHANGE_V3_ADDRESS.lower()
    taker = ("0x" + "bb" * 20) if maker_event else exchange
    return {
        "address": exchange,
        "topics": [
            probe.ORDER_FILLED_TOPIC,
            "0x" + "00" * 32,
            "0x" + "00" * 32,
            "0x" + "00" * 12 + taker[2:],
        ],
        "data": "0x" + data.hex(),
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": hex((position_id >> 248) if log_index is None else log_index),
        "blockNumber": "0xe6",
        "blockHash": "0x" + "cd" * 32,
        "removed": False,
    }


def test_event_topic_matches_official_v2_signature():
    assert probe.EVENT_SIGNATURE == (
        "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,"
        "uint256,bytes32,bytes32)"
    )
    assert probe.ORDER_FILLED_TOPIC == "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


@pytest.mark.parametrize(
    ("module_id", "expected"),
    [(1, "BINARY"), (2, "NEG_RISK"), (3, "COMBINATORIAL"), (9, "UNKNOWN_MODULE")],
)
def test_position_module_classification(module_id, expected):
    identity = probe.parse_position_id(make_position(module_id, arity=2 if module_id == 2 else 0))
    assert identity["module"] == expected
    expected_classification = {1: "SUPPORTED", 2: "SUPPORTED", 3: "RESEARCH_ONLY"}.get(module_id, "FAIL_CLOSED")
    assert identity["classification"] == expected_classification


def test_gamma_samples_prioritize_runtime_supported_positions():
    unique_ids = {
        "combo-a": {"classification": "RESEARCH_ONLY"},
        "combo-b": {"classification": "RESEARCH_ONLY"},
        "binary": {"classification": "SUPPORTED"},
        "negrisk": {"classification": "SUPPORTED"},
        "invalid": {"classification": "FAIL_CLOSED"},
    }
    assert probe._prioritize_gamma_position_ids(unique_ids) == [
        "binary", "negrisk", "combo-a", "combo-b", "invalid",
    ]


def test_invalid_module_shape_fails_closed():
    identity = probe.parse_position_id(make_position(1, outcome_index=2))
    assert identity["classification"] == "FAIL_CLOSED"


def test_decoder_extracts_position_id_and_raw_amounts():
    position_id = make_position(2, arity=3, condition_index=1, outcome_index=1)
    fill = probe.decode_order_filled(make_log(position_id))
    assert fill["module"] == "NEG_RISK"
    assert fill["condition_index"] == 1
    assert fill["outcome_index"] == 1
    assert fill["maker_amount_raw"] == "100"
    assert fill["classification"] == "SUPPORTED"


def test_runtime_decoder_rejects_non_polygon_chain_and_invalid_negrisk_arity():
    bad_chain = make_position(1) | (1 << 24)
    bad_arity = make_position(2, arity=1)
    assert probe.parse_position_id(bad_chain)["classification"] == "FAIL_CLOSED"
    assert probe.parse_position_id(bad_arity)["classification"] == "FAIL_CLOSED"


def test_combinatorial_position_ids_are_research_only():
    identity = probe.parse_position_id(make_position(3))
    assert identity["classification"] == "RESEARCH_ONLY"


def test_decoder_rejects_wrong_topic_and_malformed_data():
    log = make_log(make_position(1))
    log["topics"][0] = "0x" + "11" * 32
    with pytest.raises(ValueError, match="signature"):
        probe.decode_order_filled(log)
    with pytest.raises(ValueError, match="malformed"):
        probe.decode_order_filled(make_log(make_position(1), data=b"short"))


def test_decoder_rejects_extra_or_non_hex_event_data():
    valid = make_log(make_position(1))
    with pytest.raises(ValueError, match="encoding or length"):
        probe.decode_order_filled(dict(valid, data=valid["data"] + "00" * 32))
    with pytest.raises(ValueError, match="encoding or length"):
        probe.decode_order_filled(dict(valid, data=valid["data"][:20] + " " + valid["data"][21:]))


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, logs):
        self.logs = logs
        self.calls = []

    def post(self, _url, json, timeout):
        self.calls.append(json)
        if json["method"] == "eth_chainId":
            result = "0x89"
        elif json["method"] == "eth_blockNumber":
            result = "0x100"
        elif json["method"] == "eth_getBlockByNumber":
            assert json["params"] == ["finalized", False]
            result = {"number": "0xf0"}
        elif json["method"] == "eth_getLogs":
            result = self.logs
        else:
            raise AssertionError("unexpected RPC method")
        return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": result})

    def get(self, *_args, **_kwargs):
        return FakeResponse([])


def test_run_probe_is_read_only_and_counts_unknown_positions_fail_closed():
    good = make_position(2, arity=2, condition_index=1)
    unknown = make_position(9)
    session = FakeSession([make_log(good), make_log(unknown)])
    result = probe.run_probe("http://rpc.invalid", lookback_blocks=20, chunk_size=20,
                             gamma_sample=0, session=session)
    assert result["read_only"] is True
    assert result["runtime_authority_changed"] is False
    assert result["activity"]["order_filled_logs"] == 2
    assert result["activity"]["unique_position_ids"] == 2
    assert result["activity"]["fills_by_module_or_fail_closed"] == {"FAIL_CLOSED": 1, "NEG_RISK": 1}
    assert all(call["method"] in {"eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs"} for call in session.calls)
    assert all(call["method"] != "eth_sendRawTransaction" for call in session.calls)


def test_gamma_only_accepts_exact_position_id_match():
    position_id = str(make_position(1))
    condition = f"0x{(int(position_id) >> 8):062x}"
    class GammaSession:
        def get(self, _url, params, timeout):
            assert timeout == 30
            assert params == {"position_ids": [position_id], "closed": False, "limit": 100}
            return FakeResponse([{
                "positionIds": json.dumps([position_id, str(make_position(1, outcome_index=1))]),
                "outcomes": json.dumps(["YES", "NO"]),
                "conditionId": condition,
                "question": "Q",
            }])

    match = probe._gamma_exact_match(position_id, session=GammaSession())
    assert match == {
        "status": "EXACT_MATCH",
        "condition_id": condition,
        "question": "Q",
        "outcome": "YES",
        "module": "BINARY",
        "closed_filter": False,
        "collector_runtime_eligible": True,
    }


def test_gamma_ignores_clob_token_id_match_without_position_id_match():
    position_id = str(make_position(1))
    class GammaSession:
        def get(self, _url, params, timeout):
            assert params["position_ids"] == [position_id]
            assert params["closed"] is False
            return FakeResponse([{"clobTokenIds": json.dumps([position_id]),
                                  "positionIds": json.dumps(["456"])}])

    result = probe._gamma_exact_match(position_id, session=GammaSession())
    assert result == {"status": "POSITION_ID_QUERY_RETURNED_NO_EXACT_MATCH", "closed_filter": False}


def test_gamma_lookup_tries_closed_markets_after_active_lookup_misses():
    position_id = str(make_position(1))
    condition = f"0x{(int(position_id) >> 8):062x}"

    class GammaSession:
        def __init__(self):
            self.closed_filters = []

        def get(self, _url, params, timeout):
            assert timeout == 30
            self.closed_filters.append(params["closed"])
            if params["closed"] is False:
                return FakeResponse([])
            return FakeResponse([{
                "positionIds": [position_id, str(make_position(1, outcome_index=1))],
                "outcomes": ["YES", "NO"],
                "conditionId": condition,
                "question": "Resolved Q",
            }])

    session = GammaSession()
    result = probe._gamma_exact_match(position_id, session=session)
    assert session.closed_filters == [False, True]
    assert result["status"] == "EXACT_MATCH"
    assert result["closed_filter"] is True
    assert result["collector_runtime_eligible"] is True


def test_combinatorial_leg_reader_uses_read_only_get_legs_call():
    leg_position_id = make_position(2, arity=2, condition_index=1)

    class LegsRpc:
        def __init__(self):
            self.params = None

        def call(self, method, params):
            assert method == "eth_call"
            self.params = params
            return "0x" + encode(["uint256[]"], [[leg_position_id]]).hex()

    rpc = LegsRpc()
    combo_position_id = make_position(3, outcome_index=0)
    legs = probe._combinatorial_legs(rpc, str(combo_position_id))
    assert legs == [str(leg_position_id)]
    assert rpc.params[0]["to"] == probe.COMBINATORIAL_MODULE_ADDRESS
    assert rpc.params[0]["data"].startswith(probe.GET_COMBINATORIAL_LEGS_SELECTOR)
    assert rpc.params[1] == "latest"


def test_wrong_chain_fails_before_log_scan():
    class WrongChainSession(FakeSession):
        def post(self, _url, json, timeout):
            self.calls.append(json)
            return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": "0x1"})

    with pytest.raises(probe.ProbeError, match="Wrong chain"):
        probe.run_probe("http://rpc.invalid", session=WrongChainSession([]))


def test_env_file_reader_extracts_only_rpc_value_without_echoing_it(tmp_path, capsys, monkeypatch):
    secret = "https://rpc.example.invalid/?token=do-not-print"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=also-secret\n"
        f"POLYMARKET_RPC_URL=\"{secret}\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    assert probe._rpc_url_from_config(str(env_file)) == secret
    assert secret not in capsys.readouterr().out


def test_environment_rpc_value_takes_precedence_over_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("POLYMARKET_RPC_URL=https://file.invalid/key\n", encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_RPC_URL", "https://environment.invalid/key")
    assert probe._rpc_url_from_config(str(env_file)) == "https://environment.invalid/key"


def test_log_scan_splits_http_413_ranges_until_provider_accepts_them():
    class RangeLimitedRpc:
        def __init__(self):
            self.ranges = []

        def call(self, method, params):
            assert method == "eth_getLogs"
            query = params[0]
            first = int(query["fromBlock"], 16)
            last = int(query["toBlock"], 16)
            self.ranges.append((first, last))
            if last - first + 1 > 2:
                raise probe.ProbeError("large", http_status=413)
            return [{"blockNumber": hex(block)} for block in range(first, last + 1)]

    rpc = RangeLimitedRpc()
    logs = probe._scan_logs(rpc, "0x" + "11" * 20, 10, 17, 8)
    assert [int(log["blockNumber"], 16) for log in logs] == list(range(10, 18))
    assert rpc.ranges == [(10, 17), (10, 13), (10, 11), (12, 13), (14, 17), (14, 15), (16, 17)]


# V2-COVERAGE-1A: canonical reconciliation, provider boundaries, fail-closed proof.

def test_reconciliation_evidence_preserves_chain_identity_and_aggressor_role():
    position = make_position(1, outcome_index=0)
    taker_log = make_log(position, log_index=17, side=0)
    maker_log = make_log(position, log_index=18, maker_event=True, side=1)
    taker = probe.decode_order_filled(taker_log)
    maker = probe.decode_order_filled(maker_log)
    assert taker["evidence_id"] == "0x" + "ab" * 32 + ":17"
    assert taker["log_index"] == 17
    assert taker["block_number"] == 230
    assert taker["block_hash"] == "0x" + "cd" * 32
    assert taker["exchange_address"] == probe.EXCHANGE_V3_ADDRESS.lower()
    assert taker["taker_aggressor"] is True
    assert taker["flow_role"] == "TAKER_AGGRESSOR"
    assert taker["side_label"] == "BUY"
    assert maker["taker_aggressor"] is False
    assert maker["flow_role"] == "MAKER_OR_OTHER"
    assert maker["side_label"] == "SELL"
    assert taker["position_id"] == maker["position_id"]


@pytest.mark.parametrize("missing", [
    "transactionHash", "logIndex", "blockHash", "address", "blockNumber",
])
def test_missing_canonical_provenance_is_malformed(missing):
    log = make_log(make_position(1))
    log.pop(missing)
    with pytest.raises(ValueError):
        probe.decode_order_filled(log)


def test_removed_log_and_unsupported_side_are_malformed():
    log = make_log(make_position(1))
    log["removed"] = True
    with pytest.raises(ValueError, match="removed"):
        probe.decode_order_filled(log)
    log = make_log(make_position(1), side=2)
    with pytest.raises(ValueError, match="side"):
        probe.decode_order_filled(log)


def test_reconciliation_duplicate_dedup_separates_raw_and_unique_counts():
    same = make_log(make_position(1), log_index=3)
    session = FakeSession([same, dict(same)])
    result = probe.run_probe("http://rpc.invalid", lookback_blocks=20, chunk_size=20,
                             gamma_sample=0, session=session)
    assert result["activity"]["order_filled_logs"] == 2
    assert result["activity"]["decoded_fills"] == 2
    assert result["reconciliation"]["duplicate_events"] == 1
    assert result["reconciliation"]["conflicting_duplicate_events"] == 0
    assert result["reconciliation"]["unique_event_count"] == 1
    assert result["reconciliation"]["taker_aggressor_events"] == 1
    assert result["reconciliation"]["status"] == "EVIDENCE_KEYS_VALID_ONLY"
    assert result["reconciliation"]["completeness_claim_allowed"] is False


def test_conflicting_duplicate_identity_fails_closed():
    a = make_log(make_position(1, outcome_index=0), log_index=5)
    b = make_log(make_position(1, outcome_index=1), log_index=5)
    result = probe.run_probe("http://rpc.invalid", lookback_blocks=20, chunk_size=20,
                             gamma_sample=0, session=FakeSession([a, b]))
    assert result["reconciliation"]["conflicting_duplicate_events"] == 1
    assert result["reconciliation"]["status"] == "FAIL_CLOSED"
    assert result["reconciliation"]["completeness_claim_allowed"] is False


def test_malformed_event_rejects_completeness_claim_without_dropping_total():
    valid = make_log(make_position(1))
    malformed = dict(valid, logIndex=None)
    report = probe.run_probe("http://rpc.invalid", lookback_blocks=20, chunk_size=20,
                             gamma_sample=0, session=FakeSession([valid, malformed]))
    assert report["activity"]["order_filled_logs"] == 2
    assert report["activity"]["decoded_fills"] == 1
    assert report["reconciliation"]["malformed_event_count"] == 1
    assert report["reconciliation"]["status"] == "FAIL_CLOSED"
    assert report["reconciliation"]["rpc_result_completeness"] == "UNVERIFIED_NO_INDEPENDENT_LOG_COUNT"


def test_taker_counts_do_not_conflate_maker_fills():
    p = make_position(2, arity=2, condition_index=1)
    one = make_log(p, log_index=10)
    two = make_log(p, log_index=11, maker_event=True)
    report = probe.run_probe("http://rpc.invalid", lookback_blocks=20, chunk_size=20,
                             gamma_sample=0, session=FakeSession([one, two]))
    assert report["reconciliation"]["unique_event_count"] == 2
    assert report["reconciliation"]["taker_aggressor_events"] == 1
    assert report["reconciliation"]["maker_or_other_events"] == 1


def test_provider_log_limit_bisects_silently_capped_ranges():
    class Capped:
        def __init__(self):
            self.ranges = []

        def call(self, method, params):
            assert method == "eth_getLogs"
            params = params[0]
            first, last = int(params["fromBlock"], 16), int(params["toBlock"], 16)
            self.ranges.append((first, last))
            # Deliberately simulate a provider that returns only 2 logs
            # when more than 2 are requested, WITHOUT returning an RPC error.
            return [{"blockNumber": hex(block)} for block in range(first, min(last + 1, first + 2))]

    rpc = Capped()
    rows = probe._scan_logs(rpc, "0x" + "11" * 20, 10, 13, 4, max_logs_per_response=2)
    assert [int(item["blockNumber"], 16) for item in rows] == [10, 11, 12, 13]
    assert (10, 13) in rpc.ranges


def test_rpc_reader_marks_timeout_and_log_scan_splits_timed_out_ranges():
    class TimeoutSession:
        def post(self, *_args, **_kwargs):
            raise probe.requests.exceptions.ReadTimeout("temporary timeout")

    rpc_reader = probe.JsonRpcReader("http://rpc.invalid", session=TimeoutSession())
    with pytest.raises(probe.ProbeError) as exc_info:
        rpc_reader.call("eth_getLogs", [])
    assert exc_info.value.timed_out is True

    class TimeoutLimitedRpc:
        def __init__(self):
            self.ranges = []

        def call(self, method, params):
            assert method == "eth_getLogs"
            query = params[0]
            first, last = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            self.ranges.append((first, last))
            if last - first + 1 > 2:
                raise probe.ProbeError("timeout", timed_out=True)
            return [{"blockNumber": hex(block)} for block in range(first, last + 1)]

    rpc = TimeoutLimitedRpc()
    rows = probe._scan_logs(rpc, "0x" + "11" * 20, 10, 17, 8)
    assert [int(item["blockNumber"], 16) for item in rows] == list(range(10, 18))
    assert rpc.ranges == [(10, 17), (10, 13), (10, 11), (12, 13), (14, 17), (14, 15), (16, 17)]


def test_provider_single_block_at_limit_fails_closed():
    class Capped:
        def call(self, method, params):
            assert method == "eth_getLogs"
            return [{"blockNumber": "0xa"}, {"blockNumber": "0xa"}]

    with pytest.raises(probe.ProbeError, match="Single-block"):
        probe._scan_logs(Capped(), "0x" + "11" * 20, 10, 10, 1,
                         max_logs_per_response=2)


def test_provider_wrong_range_and_address_fail_closed():
    class Bogus:
        def __init__(self, row):
            self.row = row

        def call(self, method, params):
            return [self.row]

    for row in ({"blockNumber": "0x9"}, {
        "blockNumber": "0xa", "address": "0x" + "bb" * 20,
    }):
        with pytest.raises(probe.ProbeError):
            probe._scan_logs(Bogus(row), "0x" + "aa" * 20, 10, 10, 1)


def test_combinatorial_legs_query_at_selected_finalized_evidence_block():
    class Legs:
        def __init__(self):
            self.call_params = None

        def call(self, method, params):
            self.call_params = params
            return "0x" + encode(["uint256[]"], [[make_position(1)]]).hex()

    rpc = Legs()
    legs = probe._combinatorial_legs(rpc, str(make_position(3)), "0x64")
    assert legs == [str(make_position(1))]
    assert rpc.call_params[1] == "0x64"


def test_rpc_error_minus_32005_bisects_ranges():
    class RpcLimit:
        def __init__(self):
            self.ranges = []

        def call(self, method, params):
            first = int(params[0]["fromBlock"], 16)
            last = int(params[0]["toBlock"], 16)
            self.ranges.append((first, last))
            if first != last:
                raise probe.ProbeError("too many", rpc_code=-32005)
            return [{"blockNumber": hex(first)}]

    rpc = RpcLimit()
    rows = probe._scan_logs(rpc, "0x" + "11" * 20, 10, 12, 3)
    assert len(rows) == 3
    assert rpc.ranges[0] == (10, 12)
