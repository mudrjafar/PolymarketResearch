import base64

import pytest

from scripts import paper_settlement


CONDITION = "0x" + "11" * 32
TOKEN = "12345"


def test_identity_matches_exactly_one_supported_family(monkeypatch):
    monkeypatch.setattr(paper_settlement, "_assert_polygon_chain", lambda *a, **k: 137)
    monkeypatch.setattr(paper_settlement, "_ctf_uint", lambda *a, **k: 2)

    def fake_collection(condition, index_set, **kwargs):
        return b"A" * 32 if index_set == 1 else b"B" * 32

    def fake_position(collateral, collection_id, **kwargs):
        if (
            collateral == paper_settlement.STANDARD_USDCE
            and collection_id == b"A" * 32
        ):
            return int(TOKEN)
        return 999

    monkeypatch.setattr(paper_settlement, "_collection_id", fake_collection)
    monkeypatch.setattr(paper_settlement, "_position_id", fake_position)

    identity = paper_settlement.resolve_position_identity(
        CONDITION,
        TOKEN,
        rpc_url="http://rpc",
    )

    assert identity == {
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
        "settlement_family": paper_settlement.FAMILY_STANDARD,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.STANDARD_USDCE,
        "outcome_index": 0,
    }


def test_protocol_v2_condition_is_rejected_explicitly():
    v2_condition = "0x01" + "00" * 30
    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="UNSUPPORTED_PROTOCOL_V2",
    ):
        paper_settlement.resolve_position_identity(
            v2_condition,
            "1",
            rpc_url="http://rpc",
            validate_chain=False,
        )


def test_identity_zero_or_ambiguous_match_fails_closed(monkeypatch):
    monkeypatch.setattr(paper_settlement, "_assert_polygon_chain", lambda *a, **k: 137)
    monkeypatch.setattr(paper_settlement, "_ctf_uint", lambda *a, **k: 2)
    monkeypatch.setattr(
        paper_settlement,
        "_collection_id",
        lambda condition, index_set, **kwargs: bytes([index_set]) * 32,
    )
    monkeypatch.setattr(paper_settlement, "_position_id", lambda *a, **k: 999)

    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="TOKEN_IDENTITY_NO_MATCH",
    ):
        paper_settlement.resolve_position_identity(
            CONDITION,
            TOKEN,
            rpc_url="http://rpc",
        )

    monkeypatch.setattr(
        paper_settlement,
        "_position_id",
        lambda *a, **k: int(TOKEN),
    )
    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="TOKEN_IDENTITY_NOT_UNIQUE",
    ):
        paper_settlement.resolve_position_identity(
            CONDITION,
            TOKEN,
            rpc_url="http://rpc",
        )


def test_finalized_rpc_path_is_primary(monkeypatch):
    block_hash = "0x" + "aa" * 32
    calls = []

    def fake_rpc(url, method, params, timeout=8):
        calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        return {"number": "0x64", "hash": block_hash}

    monkeypatch.setattr(paper_settlement, "_rpc_call", fake_rpc)
    monkeypatch.setattr(
        paper_settlement,
        "_latest_milestone",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Heimdall must not be called")
        ),
    )

    result = paper_settlement.get_finalized_block(
        rpc_url="http://rpc",
        heimdall_url="http://heimdall",
    )

    assert result.number == 100
    assert result.block_hash == block_hash
    assert result.source == "RPC_FINALIZED"
    assert calls == [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", ["finalized", False]),
    ]


def test_heimdall_fallback_requires_rpc_hash_match(monkeypatch):
    milestone_hash = "0x" + "bb" * 32

    def fake_rpc(url, method, params, timeout=8):
        if method == "eth_chainId":
            return "0x89"
        if params[0] == "finalized":
            raise paper_settlement.SettlementSourceError("UNSUPPORTED")
        return {"number": "0xc8", "hash": milestone_hash}

    monkeypatch.setattr(paper_settlement, "_rpc_call", fake_rpc)
    monkeypatch.setattr(
        paper_settlement,
        "_latest_milestone",
        lambda *a, **k: (200, milestone_hash),
    )

    result = paper_settlement.get_finalized_block(
        rpc_url="http://rpc",
        heimdall_url="http://heimdall",
    )
    assert result.number == 200
    assert result.source == "HEIMDALL_MILESTONE"

    def mismatch_rpc(url, method, params, timeout=8):
        if method == "eth_chainId":
            return "0x89"
        if params[0] == "finalized":
            raise paper_settlement.SettlementSourceError("UNSUPPORTED")
        return {"number": "0xc8", "hash": "0x" + "cc" * 32}

    monkeypatch.setattr(paper_settlement, "_rpc_call", mismatch_rpc)
    with pytest.raises(
        paper_settlement.SettlementSourceError,
        match="FINALITY_HASH_MISMATCH",
    ):
        paper_settlement.get_finalized_block(
            rpc_url="http://rpc",
            heimdall_url="http://heimdall",
        )


def test_heimdall_hash_accepts_protobuf_base64():
    raw = bytes.fromhex("dd" * 32)
    encoded = base64.b64encode(raw).decode()
    assert paper_settlement._normalize_milestone_hash(encoded) == "0x" + "dd" * 32


def position():
    return {
        "condition_id": CONDITION,
        "token_id": TOKEN,
        "outcome": "Yes",
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
        "settlement_family": paper_settlement.FAMILY_STANDARD,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.STANDARD_USDCE,
        "outcome_index": 0,
    }


def _identity():
    return {
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
        "settlement_family": paper_settlement.FAMILY_STANDARD,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.STANDARD_USDCE,
        "outcome_index": 0,
    }


def test_unresolved_and_resolved_not_final(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "get_finalized_block",
        lambda **kwargs: paper_settlement.FinalizedBlock(
            100,
            "0x" + "aa" * 32,
            "RPC_FINALIZED",
        ),
    )
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda *a, **k: _identity(),
    )

    values = iter([0, 0])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(position(), rpc_url="http://rpc")
    assert result["status"] == paper_settlement.UNRESOLVED

    values = iter([0, 1])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(position(), rpc_url="http://rpc")
    assert result["status"] == paper_settlement.RESOLVED_NOT_FINAL


def test_final_payout_and_malformed_structure(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "get_finalized_block",
        lambda **kwargs: paper_settlement.FinalizedBlock(
            100,
            "0x" + "aa" * 32,
            "RPC_FINALIZED",
        ),
    )
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda *a, **k: _identity(),
    )

    values = iter([2, 1, 1])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(position(), rpc_url="http://rpc")
    assert result["status"] == paper_settlement.FINAL_SETTLED
    assert result["payout_numerator"] == 1
    assert result["payout_denominator"] == 2

    values = iter([2, 2, 1])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(position(), rpc_url="http://rpc")
    assert result["status"] == paper_settlement.SETTLEMENT_CHECK_ERROR
    assert result["reason_code"] == "PAYOUT_STRUCTURE_INVALID"


def test_frozen_identity_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "get_finalized_block",
        lambda **kwargs: paper_settlement.FinalizedBlock(
            100,
            "0x" + "aa" * 32,
            "RPC_FINALIZED",
        ),
    )
    wrong = _identity()
    wrong["outcome_index"] = 1
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda *a, **k: wrong,
    )
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: 1,
    )

    result = paper_settlement.check_settlement(position(), rpc_url="http://rpc")
    assert result["status"] == paper_settlement.IDENTITY_MISMATCH


def test_wrong_frozen_protocol_fails_closed():
    candidate = position()
    candidate["settlement_protocol"] = "PROTOCOL_V2"
    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="SETTLEMENT_PROTOCOL_INVALID",
    ):
        paper_settlement._frozen_identity(candidate)


def test_wrong_rpc_chain_fails_closed(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "_rpc_call",
        lambda url, method, params, timeout=8: "0x1",
    )

    with pytest.raises(
        paper_settlement.SettlementSourceError,
        match="RPC_CHAIN_ID_MISMATCH:1",
    ):
        paper_settlement.get_finalized_block(
            rpc_url="http://rpc",
            heimdall_url="http://heimdall",
        )


def test_outcome_label_must_match_derived_index():
    assert paper_settlement.validate_outcome_identity("Yes", 0) == 0
    assert paper_settlement.validate_outcome_identity("No", 1) == 1

    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="OUTCOME_LABEL_INDEX_MISMATCH",
    ):
        paper_settlement.validate_outcome_identity("Yes", 1)

    with pytest.raises(
        paper_settlement.SettlementIdentityError,
        match="OUTCOME_LABEL_INDEX_MISMATCH",
    ):
        paper_settlement.validate_outcome_identity("No", 0)


def test_frozen_outcome_index_requires_real_integer():
    for bad in (True, False, 0.0, 1.0, "0", "1", None):
        candidate = position()
        candidate["outcome_index"] = bad
        with pytest.raises(
            paper_settlement.SettlementIdentityError,
            match="OUTCOME_INDEX_INVALID",
        ):
            paper_settlement._frozen_identity(candidate)


def test_heimdall_requires_polygon_bor_chain_id(monkeypatch):
    milestone_hash = base64.b64encode(bytes.fromhex("ee" * 32)).decode()

    class Response:
        def __init__(self, chain_id_marker):
            self.chain_id_marker = chain_id_marker

        def raise_for_status(self):
            return None

        def json(self):
            milestone = {
                "end_block": "200",
                "hash": milestone_hash,
            }
            if self.chain_id_marker is not None:
                milestone["bor_chain_id"] = self.chain_id_marker
            return {"milestone": milestone}

    monkeypatch.setattr(
        paper_settlement.requests,
        "get",
        lambda *a, **k: Response(None),
    )
    with pytest.raises(
        paper_settlement.SettlementSourceError,
        match="HEIMDALL_CHAIN_ID_MISSING",
    ):
        paper_settlement._latest_milestone("http://heimdall")

    monkeypatch.setattr(
        paper_settlement.requests,
        "get",
        lambda *a, **k: Response("1"),
    )
    with pytest.raises(
        paper_settlement.SettlementSourceError,
        match="HEIMDALL_CHAIN_ID_MISMATCH",
    ):
        paper_settlement._latest_milestone("http://heimdall")

    monkeypatch.setattr(
        paper_settlement.requests,
        "get",
        lambda *a, **k: Response("137"),
    )
    end_block, block_hash = paper_settlement._latest_milestone("http://heimdall")
    assert end_block == 200
    assert block_hash == "0x" + "ee" * 32


def test_negrisk_fractional_payout_fails_closed_but_standard_remains_valid(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "get_finalized_block",
        lambda **kwargs: paper_settlement.FinalizedBlock(
            100,
            "0x" + "aa" * 32,
            "RPC_FINALIZED",
        ),
    )

    standard = position()
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda *a, **k: _identity(),
    )
    values = iter([2, 1, 1])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(standard, rpc_url="http://rpc")
    assert result["status"] == paper_settlement.FINAL_SETTLED
    assert result["payout_numerator"] == 1
    assert result["payout_denominator"] == 2

    negrisk = position()
    negrisk["settlement_family"] = paper_settlement.FAMILY_NEGRISK
    negrisk["position_collateral"] = paper_settlement.NEGRISK_WRAPPED_COLLATERAL
    negrisk_identity = {
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
        "settlement_family": paper_settlement.FAMILY_NEGRISK,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.NEGRISK_WRAPPED_COLLATERAL,
        "outcome_index": 0,
    }
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda *a, **k: negrisk_identity,
    )
    values = iter([2, 1, 1])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(negrisk, rpc_url="http://rpc")
    assert result["status"] == paper_settlement.SETTLEMENT_CHECK_ERROR
    assert result["reason_code"] == "NEGRISK_PAYOUT_STRUCTURE_INVALID"

    values = iter([1, 1, 0])
    monkeypatch.setattr(
        paper_settlement,
        "_ctf_uint",
        lambda *a, **k: next(values),
    )
    result = paper_settlement.check_settlement(negrisk, rpc_url="http://rpc")
    assert result["status"] == paper_settlement.FINAL_SETTLED
    assert result["payout_numerator"] == 1
    assert result["payout_denominator"] == 1


