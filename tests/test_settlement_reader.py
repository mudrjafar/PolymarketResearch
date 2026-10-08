import base64

import pytest

from scripts import settlement_reader


CONDITION = "0x" + "ab" * 32
FINAL_BLOCK = 100
FINAL_HASH = "0x" + "11" * 32
STANDARD_COLLATERAL = "0x" + "33" * 20
NEG_COLLATERAL = "0x" + "44" * 20


def uint_output(value):
    return "0x" + int(value).to_bytes(32, "big").hex()


def bytes32_output(value):
    raw = bytes.fromhex(value.removeprefix("0x"))
    assert len(raw) == 32
    return "0x" + raw.hex()


def address_output(value):
    raw = bytes.fromhex(value.removeprefix("0x"))
    assert len(raw) == 20
    return "0x" + (b"\x00" * 12 + raw).hex()


def finalized_block():
    return {"number": hex(FINAL_BLOCK), "hash": FINAL_HASH}


def unresolved_rpc(calls):
    denominator_selector = (
        "0x"
        + settlement_reader._selector("payoutDenominator(bytes32)").hex()
    )

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            return finalized_block()
        if method == "eth_call":
            assert params[1] == hex(FINAL_BLOCK)
            assert params[0]["to"].lower() == settlement_reader.CTF_ADDRESS.lower()
            assert params[0]["data"].startswith(denominator_selector)
            return uint_output(0)
        raise AssertionError(method)

    return rpc


def resolved_rpc(target_token, denominator, numerators, calls):
    collections = [
        "0x" + "01" * 32,
        "0x" + "02" * 32,
        "0x" + "03" * 32,
        "0x" + "04" * 32,
    ]
    position_ids = [111, 112, 211, 212]

    eth_results = [
        uint_output(denominator),
        address_output(settlement_reader.CTF_ADDRESS),
        address_output(settlement_reader.CTF_ADDRESS),
        address_output(STANDARD_COLLATERAL),
        address_output(NEG_COLLATERAL),
        uint_output(2),
        bytes32_output(collections[0]),
        uint_output(position_ids[0]),
        bytes32_output(collections[1]),
        uint_output(position_ids[1]),
        bytes32_output(collections[2]),
        uint_output(position_ids[2]),
        bytes32_output(collections[3]),
        uint_output(position_ids[3]),
        uint_output(numerators[0]),
        uint_output(numerators[1]),
    ]

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            return finalized_block()
        if method == "eth_call":
            assert params[1] == hex(FINAL_BLOCK)
            if not eth_results:
                raise AssertionError("unexpected extra eth_call")
            return eth_results.pop(0)
        raise AssertionError(method)

    rpc.remaining = eth_results
    rpc.position_ids = position_ids
    assert target_token in position_ids or target_token == 999
    return rpc


def test_unresolved_reads_denominator_at_exact_finalized_block_only():
    calls = []
    reader = settlement_reader.SettlementReader(
        rpc_transport=unresolved_rpc(calls),
    )

    result = reader.read(CONDITION, "111")

    assert result["status"] == "UNRESOLVED"
    assert result["payout_denominator"] == 0
    assert result["finalized_block_number"] == FINAL_BLOCK
    assert result["finalized_block_hash"] == FINAL_HASH
    assert result["finality_source"] == "RPC_FINALIZED"
    assert result["market_family"] is None
    eth_calls = [row for row in calls if row[0] == "eth_call"]
    assert len(eth_calls) == 1
    assert eth_calls[0][1][1] == hex(FINAL_BLOCK)


def test_standard_yes_binding_and_payout():
    calls = []
    rpc = resolved_rpc(111, 1, [1, 0], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    result = reader.read(CONDITION, "111")

    assert rpc.remaining == []
    assert result["status"] == "SETTLED"
    assert result["market_family"] == "STANDARD"
    assert result["outcome_index"] == 0
    assert result["outcome"] == "YES"
    assert result["payout_numerators"] == [1, 0]
    assert result["payout_numerator"] == 1
    assert result["payout_per_token"] == 1.0
    assert result["collateral"].lower() == STANDARD_COLLATERAL.lower()


def test_standard_fractional_payout_is_supported():
    calls = []
    rpc = resolved_rpc(111, 2, [1, 1], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    result = reader.read(CONDITION, "111")

    assert rpc.remaining == []
    assert result["status"] == "SETTLED"
    assert result["market_family"] == "STANDARD"
    assert result["outcome_index"] == 0
    assert result["payout_numerators"] == [1, 1]
    assert result["payout_per_token"] == 0.5


def test_neg_risk_no_binding_uses_binary_payout():
    calls = []
    rpc = resolved_rpc(212, 1, [0, 1], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    result = reader.read(CONDITION, "212")

    assert rpc.remaining == []
    assert result["status"] == "SETTLED"
    assert result["market_family"] == "NEG_RISK"
    assert result["outcome_index"] == 1
    assert result["outcome"] == "NO"
    assert result["payout_numerators"] == [0, 1]
    assert result["payout_numerator"] == 1
    assert result["payout_per_token"] == 1.0
    assert result["collateral"].lower() == NEG_COLLATERAL.lower()


def test_fractional_neg_risk_payout_fails_closed():
    calls = []
    rpc = resolved_rpc(212, 2, [1, 1], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="NEG_RISK_PAYOUT_VECTOR_INVALID",
    ):
        reader.read(CONDITION, "212")


def test_unknown_token_id_fails_closed():
    calls = []
    rpc = resolved_rpc(999, 1, [1, 0], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="TOKEN_BINDING_NOT_FOUND",
    ):
        reader.read(CONDITION, "999")


def test_invalid_binary_payout_vector_fails_closed():
    calls = []
    rpc = resolved_rpc(111, 1, [1, 1], calls)
    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="PAYOUT_VECTOR_INVALID",
    ):
        reader.read(CONDITION, "111")


def test_wrong_chain_id_fails_before_settlement_reads():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        assert method == "eth_chainId"
        return "0x1"

    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="CHAIN_ID_MISMATCH",
    ):
        reader.read(CONDITION, "111")

    assert [method for method, _ in calls] == ["eth_chainId"]


def test_heimdall_v2_fallback_cross_checks_bor_tail_hash():
    calls = []
    hash_bytes = bytes.fromhex(FINAL_HASH[2:])

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            raise settlement_reader.SettlementReadError("RPC_ERROR_-32602")
        if method == "eth_getBlockByNumber":
            assert params == [hex(FINAL_BLOCK), False]
            return finalized_block()
        if method == "eth_call":
            assert params[1] == hex(FINAL_BLOCK)
            return uint_output(0)
        raise AssertionError(method)

    def heimdall():
        return {
            "milestone": {
                "end_block": str(FINAL_BLOCK),
                "hash": base64.b64encode(hash_bytes).decode("ascii"),
                "bor_chain_id": "137",
                "milestone_id": "M-100",
            }
        }

    reader = settlement_reader.SettlementReader(
        rpc_transport=rpc,
        heimdall_transport=heimdall,
    )

    result = reader.read(CONDITION, "111")

    assert result["status"] == "UNRESOLVED"
    assert result["finality_source"] == "HEIMDALL_MILESTONE"
    assert result["milestone_id"] == "M-100"
    assert result["finalized_block_hash"] == FINAL_HASH


def test_heimdall_hash_mismatch_fails_closed():
    def rpc(method, params):
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            raise settlement_reader.SettlementReadError("RPC_ERROR_-32602")
        if method == "eth_getBlockByNumber":
            return finalized_block()
        raise AssertionError(method)

    def heimdall():
        return {
            "milestone": {
                "end_block": FINAL_BLOCK,
                "hash": "0x" + "99" * 32,
                "bor_chain_id": "137",
            }
        }

    reader = settlement_reader.SettlementReader(
        rpc_transport=rpc,
        heimdall_transport=heimdall,
    )

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="MILESTONE_BLOCK_HASH_MISMATCH",
    ):
        reader.read(CONDITION, "111")


def test_finalized_tag_failure_without_heimdall_is_unknown_not_latest():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            raise settlement_reader.SettlementReadError("RPC_ERROR_-32602")
        raise AssertionError(method)

    reader = settlement_reader.SettlementReader(rpc_transport=rpc)

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="FINALITY_UNAVAILABLE",
    ):
        reader.read(CONDITION, "111")

    assert not any(
        method == "eth_getBlockByNumber"
        and params
        and params[0] == "latest"
        for method, params in calls
    )
