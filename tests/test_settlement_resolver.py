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
