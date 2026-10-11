import json

import pytest
from eth_abi import encode

from scripts import v2_coverage_1b_audit as audit
from scripts import v2_coverage_probe as probe


def make_position():
    return (1 << 248) | (123 << 120) | (7 << 40)


def make_log():
    exchange = probe.EXCHANGE_V3_ADDRESS.lower()
    return {
        "address": exchange,
        "topics": [
            probe.ORDER_FILLED_TOPIC,
            "0x" + "11" * 32,
            "0x" + "00" * 12 + "aa" * 20,
            "0x" + "00" * 12 + exchange[2:],
        ],
        "data": "0x" + encode(
            ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
            [0, make_position(), 100, 200, 1, bytes.fromhex("22" * 32), bytes.fromhex("33" * 32)],
        ).hex(),
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": "0x2",
        "blockNumber": "0xe6",
        "blockHash": "0x" + "cd" * 32,
        "removed": False,
    }


def sample_event():
    return probe.decode_order_filled(make_log())


class FakeRpc:
    def __init__(self, *, receipt=None, block_hash=None, chain_id="0x89"):
        self.receipt = receipt if receipt is not None else {
            "transactionHash": "0x" + "ab" * 32,
            "blockNumber": "0xe6",
            "blockHash": "0x" + "cd" * 32,
            "status": "0x1",
            "logs": [make_log()],
        }
        self.block_hash = block_hash if block_hash is not None else "0x" + "cd" * 32
        self.chain_id = chain_id

    def call(self, method, params):
        if method == "eth_chainId":
            return self.chain_id
        if method == "eth_getTransactionReceipt":
            return self.receipt
        if method == "eth_getBlockByNumber":
            return {"number": "0xe6", "hash": self.block_hash}
        raise AssertionError(method)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeGamma:
    def __init__(self, markets, clob_book=None, combo_pages=None):
        self.markets = markets
        self.clob_book = clob_book
        self.combo_pages = combo_pages or [{"markets": [], "next_cursor": None}]
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        if url.endswith("/book"):
            if isinstance(self.clob_book, Exception):
                raise self.clob_book
            return FakeResponse(self.clob_book)
        if url.endswith("/combo-markets"):
            page = len([call for call in self.calls if call[0].endswith("/combo-markets")]) - 1
            return FakeResponse(self.combo_pages[page])
        return FakeResponse(self.markets)


@pytest.mark.parametrize(("module_id", "module", "outcome_index", "outcome"), [
    (1, "CTF", 0, "YES"),
    (2, "NEG_RISK", 1, "NO"),
    (3, "COMBO", 0, "YES"),
])
def test_v2_position_id_decoder(module_id, module, outcome_index, outcome):
    value = (module_id << 248) | (123 << 120) | outcome_index
    result = audit.decode_v2_position_id(str(value))
    assert result["status"] == "V2_POSITION_ID_DECODED"
    assert result["module_id"] == module_id
    assert result["module"] == module
    assert result["outcome_index"] == outcome_index
    assert result["outcome"] == outcome


def test_v2_position_id_decoder_rejects_invalid_layout():
    assert audit.decode_v2_position_id("-1")["status"] == "INVALID_POSITION_ID_ENCODING"
    assert audit.decode_v2_position_id("0")["status"] == "UNRECOGNIZED_V2_POSITION_ID_LAYOUT"


def test_combo_catalog_requires_exact_position_id_and_reports_market_metadata():
    position_id = str((3 << 248) | (123 << 120))
    session = FakeGamma([], combo_pages=[{
        "markets": [{
            "condition_id": "0xcondition",
            "slug": "combo-slug",
            "title": "Combo title",
            "position_ids": [position_id, "different-id"],
            "outcomes": ["YES", "NO"],
        }],
        "next_cursor": None,
    }])
    result = audit.audit_combo_catalog([position_id], session=session)
    assert result["status"] == "COMBO_CATALOG_COMPLETE"
    assert result["catalog_complete"] is True
    assert result["results"] == [{
        "position_id": position_id,
        "status": "EXACT_COMBO_POSITION_ID_MATCH",
        "condition_id": "0xcondition",
        "slug": "combo-slug",
        "title": "Combo title",
        "position_id_index": 0,
        "outcome": "YES",
    }]
    assert session.calls[0][0] == f"{audit.COMBO_API}/v1/rfq/combo-markets"
    assert session.calls[0][1] == {"limit": 100}


def test_combo_catalog_paginates_and_keeps_misses_inconclusive_at_page_cap():
    position_id = str((3 << 248) | (123 << 120))
    session = FakeGamma([], combo_pages=[
        {"markets": [], "next_cursor": "page-2"},
        {"markets": [], "next_cursor": "page-3"},
    ])
    result = audit.audit_combo_catalog([position_id], session=session, max_pages=2)
    assert result["status"] == "COMBO_CATALOG_PAGE_LIMIT_REACHED"
    assert result["catalog_complete"] is False
    assert result["results"][0]["status"] == "NO_EXACT_ACTIVE_COMBO_MATCH_INCONCLUSIVE"
    assert session.calls[1][1] == {"limit": 100, "cursor": "page-2"}


def test_receipt_event_and_canonical_block_match():
    result = audit.audit_receipt_event(FakeRpc(), sample_event())
    assert result["status"] == "RECEIPT_AND_CANONICAL_BLOCK_MATCH"
    assert result["evidence_id"] == "0x" + "ab" * 32 + ":2"


def test_receipt_event_mismatch_fails_closed():
    receipt = {
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": "0xe6",
        "blockHash": "0x" + "cd" * 32,
        "status": "0x1",
        "logs": [dict(make_log(), data="0x" + "00" * 224)],
    }
    result = audit.audit_receipt_event(FakeRpc(receipt=receipt), sample_event())
    assert result["status"] == "RECEIPT_EVENT_MISMATCH"


def test_receipt_hash_mismatch_is_reported():
    result = audit.audit_receipt_event(FakeRpc(block_hash="0x" + "ef" * 32), sample_event())
    assert result["status"] == "CANONICAL_BLOCK_HASH_MISMATCH"


@pytest.mark.parametrize("status", ["0x0", "0x2"])
def test_failed_transaction_receipt_is_not_accepted(status):
    receipt = {
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": "0xe6",
        "blockHash": "0x" + "cd" * 32,
        "status": status,
        "logs": [make_log()],
    }
    assert audit.audit_receipt_event(FakeRpc(receipt=receipt), sample_event())["status"] == "TRANSACTION_NOT_SUCCESSFUL"


def test_receipt_without_status_is_not_accepted_as_provenance():
    receipt = {
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": "0xe6",
        "blockHash": "0x" + "cd" * 32,
        "logs": [make_log()],
    }
    assert audit.audit_receipt_event(FakeRpc(receipt=receipt), sample_event())["status"] == "RECEIPT_STATUS_UNVERIFIED"


def test_gamma_exact_position_match_preserves_ctf_ids_separately():
    position_id = str(make_position())
    gamma = FakeGamma([{
        "conditionId": "condition-1",
        "slug": "market-1",
        "question": "Question?",
        "positionIds": json.dumps([position_id]),
        "clobTokenIds": json.dumps(["999"]),
        "outcomes": json.dumps(["Yes"]),
    }])
    result = audit.audit_gamma_position(position_id, session=gamma)
    assert result["status"] == "EXACT_POSITION_ID_MATCH"
    assert result["outcome_at_index"] == "Yes"
    assert result["clob_token_ids"] == ["999"]
    assert result["clob_token_ids_treated_as_position_ids"] is False
    assert result["clob_protocol_identity"] == "positionID"
    assert gamma.calls[0][1] == {"position_ids": position_id, "limit": 100}


def test_gamma_clob_token_match_is_not_position_identity_proof():
    position_id = str(make_position())
    result = audit.audit_gamma_position(position_id, session=FakeGamma([{
        "positionIds": ["123"],
        "clobTokenIds": [position_id],
    }]))
    assert result["status"] == "NO_EXACT_POSITION_ID_MATCH_INCONCLUSIVE"
    assert result["clob_token_ids_treated_as_position_ids"] is False


def test_gamma_duplicate_position_matches_are_ambiguous():
    position_id = str(make_position())
    result = audit.audit_gamma_position(position_id, session=FakeGamma([
        {"positionIds": [position_id]},
        {"positionIds": [position_id]},
    ]))
    assert result["status"] == "POSITION_ID_MATCH_NOT_UNIQUE"
    assert result["match_count"] == 2


def test_clob_exact_asset_id_book_response_confirms_public_lookup():
    position_id = str(make_position())
    result = audit.audit_clob_asset(position_id, session=FakeGamma([], {
        "asset_id": position_id,
        "market": "0xcondition",
        "bids": [],
        "asks": [],
    }))
    assert result["status"] == "EXACT_CLOB_ASSET_ID_MATCH"
    assert result["returned_asset_id"] == position_id
    assert result["market"] == "0xcondition"


def test_clob_mismatch_and_lookup_errors_are_not_identity_proof():
    position_id = str(make_position())
    mismatch = audit.audit_clob_asset(position_id, session=FakeGamma([], {"asset_id": "other"}))
    error = audit.audit_clob_asset(position_id, session=FakeGamma([], audit.requests.ConnectionError("offline")))
    assert mismatch["status"] == "CLOB_ASSET_ID_MISMATCH"
    assert error["status"] == "CLOB_REQUEST_ERROR"
    assert error["error_type"] == "ConnectionError"


def test_clob_http_error_exposes_status_without_response_body():
    position_id = str(make_position())
    response = type("Response", (), {"status_code": 404})()
    error = audit.requests.HTTPError(response=response)
    result = audit.audit_clob_asset(position_id, session=FakeGamma([], error))
    assert result["status"] == "CLOB_HTTP_ERROR"
    assert result["http_status"] == 404


def test_run_audit_reports_sample_only_and_never_claims_completeness():
    event = sample_event()
    report = {
        "probe": "POLYMARKET_V2_EXCHANGE_V3_COVERAGE",
        "range": {"start_block": 100, "end_block": 230},
        "reconciliation": {"unique_event_samples": [event]},
        "activity": {"unique_position_id_samples": [event]},
    }
    output = audit.run_audit(report, "unused", rpc=FakeRpc(), session=FakeGamma([{
        "positionIds": [event["position_id"]], "outcomes": ["Yes"],
    }], {"asset_id": event["position_id"], "market": "0xcondition"}))
    assert output["read_only"] is True
    assert output["receipt_provenance"]["result_counts"] == {"RECEIPT_AND_CANONICAL_BLOCK_MATCH": 1}
    assert output["receipt_provenance"]["completeness_claim_allowed"] is False
    assert output["position_identity"]["result_counts"] == {"EXACT_POSITION_ID_MATCH": 1}
    assert output["position_identity"]["clob_public_read"]["result_counts"] == {"EXACT_CLOB_ASSET_ID_MATCH": 1}
    assert output["position_identity"]["decoded_v2_position_ids"][0]["module"] == "CTF"
    assert output["position_identity"]["combo_catalog"]["status"] == "NO_COMBO_POSITION_IDS_SAMPLED"


def test_run_audit_rejects_wrong_chain():
    report = {
        "probe": "POLYMARKET_V2_EXCHANGE_V3_COVERAGE",
        "reconciliation": {"unique_event_samples": []},
        "activity": {"unique_position_id_samples": []},
    }
    with pytest.raises(probe.ProbeError, match="Wrong chain"):
        audit.run_audit(report, "unused", rpc=FakeRpc(chain_id="0x1"))


def test_input_must_be_a_v2_probe_report():
    with pytest.raises(ValueError, match="input must be"):
        audit.run_audit({}, "unused", rpc=FakeRpc())
