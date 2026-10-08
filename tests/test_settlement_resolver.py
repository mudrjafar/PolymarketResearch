import base64

import pytest

from scripts import settlement_resolver


COND = "0x" + "aa" * 32
BLOCK = {"number": 123, "tag": "0x7b", "hash": "0x" + "bb" * 32}


class FakeResolver(settlement_resolver.PolygonFinalizedCtfResolver):
    def __init__(self, *, denominator=0, numerators=(0, 0), ambiguous=False):
        self.denominator = denominator
        self.numerators = tuple(numerators)
        self.ambiguous = ambiguous

    def finalized_block(self):
        return dict(BLOCK)

    def _outcome_slot_count(self, condition, block_tag):
        assert block_tag == BLOCK["tag"]
        return 2

    def _ctf_collateral(self, exchange, block_tag):
        assert block_tag == BLOCK["tag"]
        if exchange.lower() == settlement_resolver.STANDARD_EXCHANGE_V2.lower():
            return "0x" + "11" * 20
        return "0x" + "22" * 20

    def _position_id(self, collateral, condition, index_set, block_tag):
        standard = "0x" + "11" * 20
        neg = "0x" + "22" * 20
        values = {
            (standard, 1): 101,
            (standard, 2): 102,
            (neg, 1): 201,
            (neg, 2): 202,
        }
        value = values[(collateral, index_set)]
        if self.ambiguous and index_set == 1:
            return 101
        return value

    def _decode_call(self, to, signature, arg_types, args, out_types, block_tag):
        assert block_tag == BLOCK["tag"]
        if signature == "payoutDenominator(bytes32)":
            return (self.denominator,)
        if signature == "payoutNumerators(bytes32,uint256)":
            return (self.numerators[int(args[1])],)
        raise AssertionError(signature)


def test_bind_position_proves_standard_outcome_index_from_token_id():
    result = FakeResolver().bind_position("101", COND)

    assert result["source"] == "POLYGON_FINALIZED_CTF"
    assert result["market_family"] == "STANDARD_CTF_V2"
    assert result["outcome_index"] == 0
    assert result["index_set"] == 1
    assert result["verified_block_number"] == 123


def test_bind_position_proves_neg_risk_outcome_index_from_token_id():
    result = FakeResolver().bind_position("202", COND)

    assert result["market_family"] == "NEG_RISK_CTF_V2"
    assert result["outcome_index"] == 1
    assert result["index_set"] == 2


def test_unknown_token_fails_closed():
    with pytest.raises(
        settlement_resolver.SettlementSourceError,
        match="TOKEN_OUTCOME_BINDING_NOT_FOUND",
    ):
        FakeResolver().bind_position("999", COND)


def test_zero_denominator_is_unresolved_not_zero_payout():
    resolver = FakeResolver(denominator=0)
    binding = resolver.bind_position("101", COND)
    result = resolver.check_position(
        {
            "token_id": "101",
            "condition_id": COND,
            "settlement_binding": binding,
        }
    )

    assert result["status"] == "UNRESOLVED"
    assert result["payout_denominator"] == 0
    assert result["payout_per_token"] is None


def test_fractional_finalized_payout_is_preserved():
    resolver = FakeResolver(denominator=2, numerators=(1, 1))
    binding = resolver.bind_position("101", COND)
    result = resolver.check_position(
        {
            "token_id": "101",
            "condition_id": COND,
            "settlement_binding": binding,
        }
    )

    assert result["status"] == "RESOLVED"
    assert result["payout_numerators"] == [1, 1]
    assert result["payout_numerator"] == 1
    assert result["payout_denominator"] == 2
    assert result["payout_per_token"] == 0.5


def test_invalid_payout_vector_fails_closed():
    resolver = FakeResolver(denominator=3, numerators=(1, 1))
    binding = resolver.bind_position("101", COND)

    with pytest.raises(
        settlement_resolver.SettlementSourceError,
        match="PAYOUT_VECTOR_DENOMINATOR_MISMATCH",
    ):
        resolver.check_position(
            {
                "token_id": "101",
                "condition_id": COND,
                "settlement_binding": binding,
            }
        )



class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _HeimdallSession:
    def __init__(self, milestone):
        self.milestone = milestone
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return _Response({"milestone": self.milestone})


class _FallbackResolver(settlement_resolver.PolygonFinalizedCtfResolver):
    def __init__(self, milestone, bor_hash):
        self.bor_hash = bor_hash
        session = _HeimdallSession(milestone)
        super().__init__(
            rpc_url="https://rpc.invalid",
            heimdall_rest_url="https://heimdall.invalid/",
            session=session,
        )

    def _rpc(self, method, params):
        assert method == "eth_getBlockByNumber"
        if params[0] == "finalized":
            raise settlement_resolver.SettlementSourceError("RPC_ERROR")
        assert params == ["0xc8", False]
        return {"number": "0xc8", "hash": self.bor_hash}


def test_heimdall_v2_fallback_cross_checks_finalized_end_block_hash():
    raw_hash = bytes.fromhex("44" * 32)
    milestone = {
        "start_block": "190",
        "end_block": "200",
        "hash": base64.b64encode(raw_hash).decode("ascii"),
        "bor_chain_id": "137",
        "milestone_id": "M-1",
    }
    resolver = _FallbackResolver(milestone, "0x" + "44" * 32)

    block = resolver.finalized_block()

    assert block["number"] == 200
    assert block["hash"] == "0x" + "44" * 32
    assert block["finality_source"] == "HEIMDALL_V2_MILESTONE"
    assert resolver.session.urls == ["https://heimdall.invalid/milestones/latest"]


def test_heimdall_v2_fallback_rejects_bor_hash_mismatch():
    raw_hash = bytes.fromhex("44" * 32)
    milestone = {
        "end_block": "200",
        "hash": base64.b64encode(raw_hash).decode("ascii"),
        "bor_chain_id": "137",
    }
    resolver = _FallbackResolver(milestone, "0x" + "55" * 32)

    with pytest.raises(
        settlement_resolver.SettlementSourceError,
        match="HEIMDALL_BOR_BLOCK_HASH_MISMATCH",
    ):
        resolver.finalized_block()


def test_heimdall_v2_fallback_rejects_wrong_chain():
    raw_hash = bytes.fromhex("44" * 32)
    milestone = {
        "end_block": "200",
        "hash": base64.b64encode(raw_hash).decode("ascii"),
        "bor_chain_id": "80002",
    }
    resolver = _FallbackResolver(milestone, "0x" + "44" * 32)

    with pytest.raises(
        settlement_resolver.SettlementSourceError,
        match="HEIMDALL_CHAIN_ID_MISMATCH",
    ):
        resolver.finalized_block()
