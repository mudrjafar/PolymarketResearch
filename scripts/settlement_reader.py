"""Read-only CTF settlement state from a finalized Polygon block.

Authority:
- Polygon RPC finalized tag for finality.
- Polymarket ConditionalTokens contract for payout state.

No CLOB/Gamma status is used as resolution authority. No transaction is signed
or submitted.
"""

import os

import requests
from eth_utils import keccak


POLYGON_CHAIN_ID = 137
POLYMARKET_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_TIMEOUT_SECONDS = 5


class SettlementReadError(RuntimeError):
    pass


def _condition_bytes32(condition_id):
    value = str(condition_id or "").strip().lower()
    if not value.startswith("0x") or len(value) != 66:
        raise SettlementReadError("CONDITION_ID_INVALID")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise SettlementReadError("CONDITION_ID_INVALID") from exc
    if len(raw) != 32:
        raise SettlementReadError("CONDITION_ID_INVALID")
    return raw


def outcome_index_for_label(outcome):
    """Polymarket binary CTF partition: YES indexSet=1, NO indexSet=2."""
    label = str(outcome or "").strip().lower()
    if label == "yes":
        return 0
    if label == "no":
        return 1
    raise SettlementReadError("OUTCOME_BINDING_INVALID")


def _selector(signature):
    return keccak(text=signature)[:4].hex()


def _rpc(url, method, params, timeout=DEFAULT_TIMEOUT_SECONDS):
    try:
        response = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SettlementReadError(f"RPC_{type(exc).__name__}") from exc

    if response.status_code != 200:
        raise SettlementReadError(f"RPC_HTTP_{response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SettlementReadError("RPC_INVALID_JSON") from exc

    if not isinstance(payload, dict):
        raise SettlementReadError("RPC_INVALID_PAYLOAD")
    if payload.get("error") is not None:
        raise SettlementReadError("RPC_ERROR")
    if "result" not in payload:
        raise SettlementReadError("RPC_RESULT_MISSING")
    return payload["result"]


def _parse_hex_uint(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SettlementReadError(label)
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise SettlementReadError(label) from exc
    if result < 0:
        raise SettlementReadError(label)
    return result


def _eth_call_uint(
    rpc_url,
    contract,
    signature,
    condition_id,
    block_number,
    *,
    outcome_index=None,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    rpc_call=_rpc,
):
    condition = _condition_bytes32(condition_id)
    data = "0x" + _selector(signature) + condition.hex()
    if outcome_index is not None:
        if isinstance(outcome_index, bool) or not isinstance(outcome_index, int):
            raise SettlementReadError("OUTCOME_INDEX_INVALID")
        if outcome_index < 0:
            raise SettlementReadError("OUTCOME_INDEX_INVALID")
        data += outcome_index.to_bytes(32, byteorder="big").hex()

    result = rpc_call(
        rpc_url,
        "eth_call",
        [{"to": contract, "data": data}, hex(block_number)],
        timeout,
    )
    return _parse_hex_uint(result, "ETH_CALL_RESULT_INVALID")


def read_finalized_settlement(
    condition_id,
    outcome_index,
    *,
    rpc_url=None,
    ctf_contract=POLYMARKET_CTF_ADDRESS,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    rpc_call=_rpc,
):
    """Return finalized CTF payout state for one binary outcome.

    Fail closed if the RPC cannot provide a finalized block or if the payout
    vector is internally inconsistent.
    """
    rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL") or "").strip()
    if not rpc_url:
        raise SettlementReadError("RPC_URL_MISSING")

    if outcome_index not in (0, 1):
        raise SettlementReadError("OUTCOME_INDEX_INVALID")

    contract = str(ctf_contract or "").strip()
    if contract.lower() != POLYMARKET_CTF_ADDRESS.lower():
        raise SettlementReadError("CTF_CONTRACT_UNSUPPORTED")

    block = rpc_call(
        rpc_url,
        "eth_getBlockByNumber",
        ["finalized", False],
        timeout,
    )
    if not isinstance(block, dict):
        raise SettlementReadError("FINALIZED_BLOCK_UNAVAILABLE")

    block_number = _parse_hex_uint(block.get("number"), "FINALIZED_BLOCK_INVALID")
    block_hash = str(block.get("hash") or "").strip().lower()
    if len(block_hash) != 66 or not block_hash.startswith("0x"):
        raise SettlementReadError("FINALIZED_BLOCK_INVALID")

    denominator = _eth_call_uint(
        rpc_url,
        contract,
        "payoutDenominator(bytes32)",
        condition_id,
        block_number,
        timeout=timeout,
        rpc_call=rpc_call,
    )

    base = {
        "finality_source": "POLYGON_RPC_FINALIZED_TAG",
        "finalized_block_number": block_number,
        "finalized_block_hash": block_hash,
        "ctf_contract": contract,
        "condition_id": str(condition_id).lower(),
        "outcome_index": outcome_index,
        "payout_denominator": denominator,
    }

    if denominator == 0:
        return {
            **base,
            "status": "UNRESOLVED",
            "payout_numerator": None,
            "payout_per_token": None,
        }

    numerator_yes = _eth_call_uint(
        rpc_url,
        contract,
        "payoutNumerators(bytes32,uint256)",
        condition_id,
        block_number,
        outcome_index=0,
        timeout=timeout,
        rpc_call=rpc_call,
    )
    numerator_no = _eth_call_uint(
        rpc_url,
        contract,
        "payoutNumerators(bytes32,uint256)",
        condition_id,
        block_number,
        outcome_index=1,
        timeout=timeout,
        rpc_call=rpc_call,
    )

    if numerator_yes + numerator_no != denominator:
        raise SettlementReadError("PAYOUT_VECTOR_INVALID")

    numerator = numerator_yes if outcome_index == 0 else numerator_no
    return {
        **base,
        "status": "SETTLED",
        "payout_numerator": numerator,
        "payout_per_token": numerator / denominator,
        "payout_vector": [numerator_yes, numerator_no],
    }
