import pytest

from scripts import settlement_reader


CONDITION = "0x" + "11" * 32
BLOCK_HASH = "0x" + "22" * 32


def test_binary_outcome_binding_is_explicit():
    assert settlement_reader.outcome_index_for_label("Yes") == 0
    assert settlement_reader.outcome_index_for_label("NO") == 1
    with pytest.raises(settlement_reader.SettlementReadError, match="OUTCOME_BINDING_INVALID"):
        settlement_reader.outcome_index_for_label("Maybe")


def test_finalized_fractional_payout_uses_exact_finalized_block():
    calls = []

    def fake_rpc(url, method, params, timeout):
        calls.append((method, params))
        if method == "eth_chainId":
            return hex(settlement_reader.POLYGON_CHAIN_ID)
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            return {"number": "0x64", "hash": BLOCK_HASH}
        if method == "eth_getCode":
            assert params == [settlement_reader.POLYMARKET_CTF_ADDRESS, "0x64"]
            return "0x6000"
        assert method == "eth_call"
        assert params[1] == "0x64"
        data = params[0]["data"]
        if data.startswith("0x" + settlement_reader._selector("payoutDenominator(bytes32)")):
            return hex(2)
        if data.startswith("0x" + settlement_reader._selector("payoutNumerators(bytes32,uint256)")):
            index = int(data[-64:], 16)
            return hex(1 if index in (0, 1) else 0)
        raise AssertionError(method)

    result = settlement_reader.read_finalized_settlement(
        CONDITION,
        1,
        rpc_url="https://rpc.invalid",
        rpc_call=fake_rpc,
    )

    assert result["status"] == "SETTLED"
    assert result["finalized_block_number"] == 100
    assert result["finalized_block_hash"] == BLOCK_HASH
    assert result["payout_vector"] == [1, 1]
    assert result["payout_numerator"] == 1
    assert result["payout_denominator"] == 2
    assert result["payout_per_token"] == 0.5
    assert [method for method, _ in calls].count("eth_call") == 3


def test_unresolved_denominator_zero_does_not_invent_zero_payout():
    calls = []

    def fake_rpc(url, method, params, timeout):
        calls.append(method)
        if method == "eth_chainId":
            return hex(settlement_reader.POLYGON_CHAIN_ID)
        if method == "eth_getBlockByNumber":
            return {"number": "0x65", "hash": BLOCK_HASH}
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            return "0x0"
        raise AssertionError(method)

    result = settlement_reader.read_finalized_settlement(
        CONDITION,
        0,
        rpc_url="https://rpc.invalid",
        rpc_call=fake_rpc,
    )

    assert result["status"] == "UNRESOLVED"
    assert result["payout_denominator"] == 0
    assert result["payout_numerator"] is None
    assert result["payout_per_token"] is None
    assert calls.count("eth_call") == 1


def test_invalid_binary_payout_vector_fails_closed():
    def fake_rpc(url, method, params, timeout):
        if method == "eth_chainId":
            return hex(settlement_reader.POLYGON_CHAIN_ID)
        if method == "eth_getBlockByNumber":
            return {"number": "0x66", "hash": BLOCK_HASH}
        if method == "eth_getCode":
            return "0x6000"
        data = params[0]["data"]
        if data.startswith("0x" + settlement_reader._selector("payoutDenominator(bytes32)")):
            return hex(3)
        index = int(data[-64:], 16)
        return hex(1 if index == 0 else 1)

    with pytest.raises(settlement_reader.SettlementReadError, match="PAYOUT_VECTOR_INVALID"):
        settlement_reader.read_finalized_settlement(
            CONDITION,
            0,
            rpc_url="https://rpc.invalid",
            rpc_call=fake_rpc,
        )


def test_finalized_tag_unavailable_fails_closed():
    def fake_rpc(url, method, params, timeout):
        if method == "eth_chainId":
            return hex(settlement_reader.POLYGON_CHAIN_ID)
        assert method == "eth_getBlockByNumber"
        return None

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="FINALIZED_BLOCK_UNAVAILABLE",
    ):
        settlement_reader.read_finalized_settlement(
            CONDITION,
            0,
            rpc_url="https://rpc.invalid",
            rpc_call=fake_rpc,
        )


def test_wrong_ctf_contract_is_rejected():
    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="CTF_CONTRACT_UNSUPPORTED",
    ):
        settlement_reader.read_finalized_settlement(
            CONDITION,
            0,
            rpc_url="https://rpc.invalid",
            ctf_contract="0x" + "33" * 20,
            rpc_call=lambda *args: None,
        )


def test_wrong_chain_fails_before_ctf_call():
    calls = []

    def fake_rpc(url, method, params, timeout):
        calls.append(method)
        if method == "eth_chainId":
            return "0x1"
        raise AssertionError("must stop before finalized block or eth_call")

    with pytest.raises(
        settlement_reader.SettlementReadError,
        match="CHAIN_ID_MISMATCH",
    ):
        settlement_reader.read_finalized_settlement(
            CONDITION,
            0,
            rpc_url="https://rpc.invalid",
            rpc_call=fake_rpc,
        )

    assert calls == ["eth_chainId"]
