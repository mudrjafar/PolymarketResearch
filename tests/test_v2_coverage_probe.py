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


def make_log(position_id, module_id=1, *, data=None):
    data = data or encode(
        ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
        [0, position_id, 100, 200, 1, bytes(32), bytes(32)],
    )
    return {
        "topics": [probe.ORDER_FILLED_TOPIC, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + "00" * 32],
        "data": "0x" + data.hex(),
        "transactionHash": "0xabc",
        "blockNumber": "0x64",
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
    expected_classification = {1: "RECOGNIZED", 2: "RECOGNIZED", 3: "RESEARCH_ONLY"}.get(module_id, "FAIL_CLOSED")
    assert identity["classification"] == expected_classification


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
    assert fill["classification"] == "RECOGNIZED"


def test_decoder_rejects_wrong_topic_and_malformed_data():
    log = make_log(make_position(1))
    log["topics"][0] = "0x" + "11" * 32
    with pytest.raises(ValueError, match="signature"):
        probe.decode_order_filled(log)
    with pytest.raises(ValueError, match="malformed"):
        probe.decode_order_filled(make_log(make_position(1), data=b"short"))


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


def test_gamma_only_accepts_exact_token_match(monkeypatch):
    class GammaSession:
        def get(self, _url, params, timeout):
            assert timeout == 15
            return FakeResponse([{"clobTokenIds": json.dumps([params["clob_token_ids"]]),
                                 "conditionId": "0xcondition", "question": "Q"}])

    match = probe._gamma_exact_match("123", session=GammaSession())
    assert match == {"status": "EXACT_MATCH", "condition_id": "0xcondition", "question": "Q"}


def test_wrong_chain_fails_before_log_scan():
    class WrongChainSession(FakeSession):
        def post(self, _url, json, timeout):
            self.calls.append(json)
            return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": "0x1"})

    with pytest.raises(probe.ProbeError, match="Wrong chain"):
        probe.run_probe("http://rpc.invalid", session=WrongChainSession([]))
