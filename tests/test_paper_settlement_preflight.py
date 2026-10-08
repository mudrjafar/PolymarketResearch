from types import SimpleNamespace

from scripts import paper_settlement
from scripts import paper_settlement_preflight as preflight


UNRESOLVED_SAMPLE = {
    "condition_id": "0x" + "11" * 32,
    "token_id": "101",
    "outcome": "Yes",
}
RESOLVED_SAMPLE = {
    "condition_id": "0x" + "22" * 32,
    "token_id": "202",
    "outcome": "No",
}


def configure_base(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "_assert_polygon_chain",
        lambda rpc_url: 137,
    )
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
        "_ctf_uint",
        lambda *a, **k: 0,
    )


def test_rpc_only_preflight_is_not_full_acceptance(monkeypatch):
    configure_base(monkeypatch)

    result, code = preflight.run_preflight(rpc_url="http://rpc")

    assert code == 0
    assert result["status"] == "RPC_PREFLIGHT_OK"
    assert result["chain_id"] == 137
    assert result["historical_ctf_read_ok"] is True
    assert result["reason_code"] == "KNOWN_EXAMPLES_REQUIRED_FOR_FULL_ACCEPTANCE"


def test_full_acceptance_requires_both_examples_and_expected_states(monkeypatch):
    configure_base(monkeypatch)
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda condition_id, token_id, **kwargs: {
            "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
            "settlement_family": paper_settlement.FAMILY_STANDARD,
            "ctf_contract": paper_settlement.CTF_CONTRACT,
            "position_collateral": paper_settlement.STANDARD_USDCE,
            "outcome_index": 0 if token_id == "101" else 1,
        },
    )

    def fake_check(position, **kwargs):
        if position["token_id"] == "101":
            status = paper_settlement.UNRESOLVED
        else:
            status = paper_settlement.FINAL_SETTLED
        return {
            "status": status,
            "reason_code": None,
            "settlement_read_block": 100,
            "settlement_read_block_hash": "0x" + "aa" * 32,
            "settlement_finality_source": "RPC_FINALIZED",
        }

    monkeypatch.setattr(paper_settlement, "check_settlement", fake_check)

    result, code = preflight.run_preflight(
        rpc_url="http://rpc",
        unresolved_example=UNRESOLVED_SAMPLE,
        resolved_example=RESOLVED_SAMPLE,
    )

    assert code == 0
    assert result["status"] == "SETTLEMENT_ACCEPTANCE_OK"
    assert result["examples"]["unresolved"]["ok"] is True
    assert result["examples"]["resolved"]["ok"] is True


def test_example_status_mismatch_blocks_acceptance(monkeypatch):
    configure_base(monkeypatch)
    monkeypatch.setattr(
        paper_settlement,
        "resolve_position_identity",
        lambda condition_id, token_id, **kwargs: {
            "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
            "settlement_family": paper_settlement.FAMILY_STANDARD,
            "ctf_contract": paper_settlement.CTF_CONTRACT,
            "position_collateral": paper_settlement.STANDARD_USDCE,
            "outcome_index": 0 if token_id == "101" else 1,
        },
    )
    monkeypatch.setattr(
        paper_settlement,
        "check_settlement",
        lambda position, **kwargs: {
            "status": paper_settlement.UNRESOLVED,
            "reason_code": None,
        },
    )

    result, code = preflight.run_preflight(
        rpc_url="http://rpc",
        unresolved_example=UNRESOLVED_SAMPLE,
        resolved_example=RESOLVED_SAMPLE,
    )

    assert code == 2
    assert result["status"] == "SETTLEMENT_ACCEPTANCE_BLOCKED"
    assert result["reason_code"] == "EXAMPLE_STATUS_MISMATCH"


def test_source_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        paper_settlement,
        "_assert_polygon_chain",
        lambda rpc_url: (_ for _ in ()).throw(
            paper_settlement.SettlementSourceError("RPC_CHAIN_ID_MISMATCH:1")
        ),
    )

    result, code = preflight.run_preflight(rpc_url="http://rpc")

    assert code == 2
    assert result["status"] == "SETTLEMENT_ACCEPTANCE_BLOCKED"
    assert result["historical_ctf_read_ok"] is False
    assert result["reason_code"] == "RPC_CHAIN_ID_MISMATCH:1"


def test_incomplete_cli_example_is_rejected_without_rpc_call(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "run_preflight",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("RPC preflight must not run")
        ),
    )

    code = preflight.main([
        "--unresolved-condition",
        UNRESOLVED_SAMPLE["condition_id"],
    ])

    assert code == 2
