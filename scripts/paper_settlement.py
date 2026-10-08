"""Read-only Paper settlement identity and finality resolver.

This module has no Paper-state authority. It validates a Paper position against
the currently supported Polymarket CTF families and reads CTF payout state at a
finalized Polygon block. It never signs, redeems, or submits transactions.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

import requests
from eth_abi import decode, encode
from eth_utils import keccak


POLYGON_CHAIN_ID = 137
CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
STANDARD_USDCE = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
NEGRISK_WRAPPED_COLLATERAL = "0x3a3bd7bb9528e159577f7c2e685cc81a765002e2"
SETTLEMENT_PROTOCOL_LEGACY_CTF = "LEGACY_CTF"

FAMILY_STANDARD = "CTF_STANDARD"
FAMILY_NEGRISK = "CTF_NEGRISK"
SUPPORTED_FAMILIES = {
    FAMILY_STANDARD: STANDARD_USDCE,
    FAMILY_NEGRISK: NEGRISK_WRAPPED_COLLATERAL,
}

UNRESOLVED = "UNRESOLVED"
RESOLVED_NOT_FINAL = "RESOLVED_NOT_FINAL"
FINAL_SETTLED = "FINAL_SETTLED"
SETTLEMENT_CHECK_ERROR = "SETTLEMENT_CHECK_ERROR"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"

DEFAULT_TIMEOUT_SECONDS = 8
_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SettlementSourceError(RuntimeError):
    pass


class SettlementIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class FinalizedBlock:
    number: int
    block_hash: str
    source: str


def _condition_bytes(condition_id):
    value = str(condition_id or "").strip()
    if _BYTES32_RE.fullmatch(value) is None:
        raise SettlementIdentityError("CONDITION_ID_INVALID")
    return bytes.fromhex(value[2:])


def _address(value, label="ADDRESS_INVALID"):
    value = str(value or "").strip().lower()
    if _ADDRESS_RE.fullmatch(value) is None:
        raise SettlementIdentityError(label)
    return value


def _token_int(token_id):
    value = str(token_id or "").strip()
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        raise SettlementIdentityError("TOKEN_ID_INVALID") from None
    if parsed < 0:
        raise SettlementIdentityError("TOKEN_ID_INVALID")
    return parsed


def _outcome_index_from_label(outcome):
    if not isinstance(outcome, str):
        raise SettlementIdentityError("OUTCOME_LABEL_INVALID")
    label = outcome.strip().casefold()
    if label == "yes":
        return 0
    if label == "no":
        return 1
    raise SettlementIdentityError("OUTCOME_LABEL_INVALID")


def validate_outcome_identity(outcome, outcome_index):
    if (
        not isinstance(outcome_index, int)
        or isinstance(outcome_index, bool)
        or outcome_index not in (0, 1)
    ):
        raise SettlementIdentityError("OUTCOME_INDEX_INVALID")
    if _outcome_index_from_label(outcome) != outcome_index:
        raise SettlementIdentityError("OUTCOME_IDENTITY_MISMATCH")
    return True


def _selector(signature):
    return keccak(text=signature)[:4]


def _rpc_call(rpc_url, method, params, timeout=DEFAULT_TIMEOUT_SECONDS):
    rpc_url = str(rpc_url or "").strip()
    if not rpc_url:
        raise SettlementSourceError("RPC_MISSING")

    try:
        response = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SettlementSourceError(f"RPC_{type(exc).__name__}") from exc
    except ValueError as exc:
        raise SettlementSourceError("RPC_INVALID_JSON") from exc

    if not isinstance(payload, dict):
        raise SettlementSourceError("RPC_INVALID_PAYLOAD")
    if payload.get("error") is not None:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise SettlementSourceError(f"RPC_ERROR_{code}")
    if "result" not in payload:
        raise SettlementSourceError("RPC_RESULT_MISSING")
    return payload["result"]



def _assert_polygon_chain(rpc_url, timeout=DEFAULT_TIMEOUT_SECONDS):
    result = _rpc_call(
        rpc_url,
        "eth_chainId",
        [],
        timeout=timeout,
    )
    try:
        chain_id = int(str(result), 16)
    except (TypeError, ValueError):
        raise SettlementSourceError("RPC_CHAIN_ID_INVALID") from None
    if chain_id != POLYGON_CHAIN_ID:
        raise SettlementSourceError(
            f"RPC_CHAIN_ID_MISMATCH:{chain_id}"
        )
    return chain_id

def _normalize_block(block):
    if not isinstance(block, dict):
        raise SettlementSourceError("FINALIZED_BLOCK_INVALID")
    try:
        number = int(str(block.get("number")), 16)
    except (TypeError, ValueError):
        raise SettlementSourceError("FINALIZED_BLOCK_NUMBER_INVALID") from None
    block_hash = str(block.get("hash") or "").strip().lower()
    if _BYTES32_RE.fullmatch(block_hash) is None:
        raise SettlementSourceError("FINALIZED_BLOCK_HASH_INVALID")
    return number, block_hash


def _normalize_milestone_hash(value):
    raw = str(value or "").strip()
    if _BYTES32_RE.fullmatch(raw):
        return raw.lower()
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return "0x" + raw.lower()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SettlementSourceError("HEIMDALL_HASH_INVALID") from exc
    if len(decoded) != 32:
        raise SettlementSourceError("HEIMDALL_HASH_INVALID")
    return "0x" + decoded.hex()


def _latest_milestone(heimdall_url, timeout=DEFAULT_TIMEOUT_SECONDS):
    heimdall_url = str(heimdall_url or "").strip()
    if not heimdall_url:
        raise SettlementSourceError("HEIMDALL_MISSING")

    try:
        response = requests.get(
            heimdall_url.rstrip("/") + "/milestones/latest",
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SettlementSourceError(f"HEIMDALL_{type(exc).__name__}") from exc
    except ValueError as exc:
        raise SettlementSourceError("HEIMDALL_INVALID_JSON") from exc

    if not isinstance(payload, dict):
        raise SettlementSourceError("HEIMDALL_INVALID_PAYLOAD")

    milestone = payload.get("milestone")
    if milestone is None and isinstance(payload.get("result"), dict):
        milestone = payload["result"].get("milestone")
    if not isinstance(milestone, dict):
        raise SettlementSourceError("HEIMDALL_MILESTONE_MISSING")

    try:
        end_block = int(str(milestone.get("end_block") or milestone.get("endBlock")))
    except (TypeError, ValueError):
        raise SettlementSourceError("HEIMDALL_END_BLOCK_INVALID") from None
    if end_block < 0:
        raise SettlementSourceError("HEIMDALL_END_BLOCK_INVALID")

    chain_id = str(
        milestone.get("bor_chain_id")
        or milestone.get("borChainId")
        or ""
    ).strip()
    if not chain_id:
        raise SettlementSourceError("HEIMDALL_CHAIN_ID_MISSING")
    if chain_id != str(POLYGON_CHAIN_ID):
        raise SettlementSourceError("HEIMDALL_CHAIN_ID_MISMATCH")

    return end_block, _normalize_milestone_hash(milestone.get("hash"))


def get_finalized_block(
    *,
    rpc_url=None,
    heimdall_url=None,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL", "")).strip()
    heimdall_url = str(
        heimdall_url or os.getenv("POLYMARKET_HEIMDALL_URL", "")
    ).strip()

    _assert_polygon_chain(rpc_url, timeout=timeout)

    try:
        block = _rpc_call(
            rpc_url,
            "eth_getBlockByNumber",
            ["finalized", False],
            timeout=timeout,
        )
        number, block_hash = _normalize_block(block)
        return FinalizedBlock(number, block_hash, "RPC_FINALIZED")
    except SettlementSourceError as finalized_error:
        if not heimdall_url:
            raise SettlementSourceError(
                f"FINALITY_UNAVAILABLE:{finalized_error}"
            ) from finalized_error

    end_block, milestone_hash = _latest_milestone(
        heimdall_url,
        timeout=timeout,
    )
    rpc_block = _rpc_call(
        rpc_url,
        "eth_getBlockByNumber",
        [hex(end_block), False],
        timeout=timeout,
    )
    rpc_number, rpc_hash = _normalize_block(rpc_block)
    if rpc_number != end_block or rpc_hash != milestone_hash:
        raise SettlementSourceError("FINALITY_HASH_MISMATCH")

    return FinalizedBlock(end_block, rpc_hash, "HEIMDALL_MILESTONE")


def _eth_call(
    signature,
    arg_types,
    args,
    return_types,
    *,
    rpc_url,
    block_tag,
    contract=CTF_CONTRACT,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    call_data = _selector(signature) + encode(arg_types, args)
    result = _rpc_call(
        rpc_url,
        "eth_call",
        [
            {
                "to": _address(contract, "CTF_ADDRESS_INVALID"),
                "data": "0x" + call_data.hex(),
            },
            block_tag,
        ],
        timeout=timeout,
    )
    if not isinstance(result, str) or not result.startswith("0x"):
        raise SettlementSourceError("ETH_CALL_RESULT_INVALID")
    try:
        decoded = decode(return_types, bytes.fromhex(result[2:]))
    except Exception as exc:
        raise SettlementSourceError("ETH_CALL_DECODE_ERROR") from exc
    return decoded


def _ctf_uint(
    signature,
    arg_types,
    args,
    *,
    rpc_url,
    block_tag,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    value = _eth_call(
        signature,
        arg_types,
        args,
        ["uint256"],
        rpc_url=rpc_url,
        block_tag=block_tag,
        timeout=timeout,
    )[0]
    return int(value)


def _collection_id(
    condition_bytes,
    index_set,
    *,
    rpc_url,
    block_tag,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    return _eth_call(
        "getCollectionId(bytes32,bytes32,uint256)",
        ["bytes32", "bytes32", "uint256"],
        [b"\x00" * 32, condition_bytes, int(index_set)],
        ["bytes32"],
        rpc_url=rpc_url,
        block_tag=block_tag,
        timeout=timeout,
    )[0]


def _position_id(
    collateral,
    collection_id,
    *,
    rpc_url,
    block_tag,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    return _ctf_uint(
        "getPositionId(address,bytes32)",
        ["address", "bytes32"],
        [_address(collateral), collection_id],
        rpc_url=rpc_url,
        block_tag=block_tag,
        timeout=timeout,
    )


def resolve_position_identity(
    condition_id,
    token_id,
    *,
    rpc_url=None,
    block_tag="latest",
    timeout=DEFAULT_TIMEOUT_SECONDS,
    validate_chain=True,
):
    rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL", "")).strip()
    if validate_chain:
        _assert_polygon_chain(rpc_url, timeout=timeout)
    condition = _condition_bytes(condition_id)
    token = _token_int(token_id)

    slots = _ctf_uint(
        "getOutcomeSlotCount(bytes32)",
        ["bytes32"],
        [condition],
        rpc_url=rpc_url,
        block_tag=block_tag,
        timeout=timeout,
    )
    if slots != 2:
        raise SettlementIdentityError("OUTCOME_SLOT_COUNT_UNSUPPORTED")

    collections = {
        0: _collection_id(
            condition,
            1,
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
        ),
        1: _collection_id(
            condition,
            2,
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
        ),
    }

    matches = []
    for family, collateral in SUPPORTED_FAMILIES.items():
        for outcome_index in (0, 1):
            candidate = _position_id(
                collateral,
                collections[outcome_index],
                rpc_url=rpc_url,
                block_tag=block_tag,
                timeout=timeout,
            )
            if candidate == token:
                matches.append(
                    {
                        "settlement_protocol": SETTLEMENT_PROTOCOL_LEGACY_CTF,
                        "settlement_family": family,
                        "ctf_contract": CTF_CONTRACT,
                        "position_collateral": collateral,
                        "outcome_index": outcome_index,
                    }
                )

    if len(matches) != 1:
        raise SettlementIdentityError(
            "TOKEN_IDENTITY_NOT_UNIQUE" if matches else "TOKEN_IDENTITY_NO_MATCH"
        )

    return matches[0]


def validate_outcome_identity(outcome, outcome_index):
    """Require the stored binary label to agree with the derived CTF slot."""
    if not isinstance(outcome_index, int) or isinstance(outcome_index, bool):
        raise SettlementIdentityError("OUTCOME_INDEX_INVALID")
    if outcome_index not in (0, 1):
        raise SettlementIdentityError("OUTCOME_INDEX_INVALID")

    label = str(outcome or "").strip().lower()
    expected = {"yes": 0, "no": 1}.get(label)
    if expected is None:
        raise SettlementIdentityError("OUTCOME_LABEL_INVALID")
    if expected != outcome_index:
        raise SettlementIdentityError("OUTCOME_LABEL_INDEX_MISMATCH")
    return outcome_index


def _frozen_identity(position):
    if not isinstance(position, dict):
        raise SettlementIdentityError("POSITION_INVALID")
    protocol = str(position.get("settlement_protocol") or "").strip()
    if protocol != SETTLEMENT_PROTOCOL_LEGACY_CTF:
        raise SettlementIdentityError("SETTLEMENT_PROTOCOL_INVALID")
    family = str(position.get("settlement_family") or "").strip()
    if family not in SUPPORTED_FAMILIES:
        raise SettlementIdentityError("SETTLEMENT_FAMILY_INVALID")

    ctf_contract = _address(
        position.get("ctf_contract"),
        "CTF_ADDRESS_INVALID",
    )
    if ctf_contract != CTF_CONTRACT:
        raise SettlementIdentityError("CTF_ADDRESS_MISMATCH")

    collateral = _address(
        position.get("position_collateral"),
        "POSITION_COLLATERAL_INVALID",
    )
    if collateral != SUPPORTED_FAMILIES[family]:
        raise SettlementIdentityError("POSITION_COLLATERAL_MISMATCH")

    outcome_index = position.get("outcome_index")
    if (
        not isinstance(outcome_index, int)
        or isinstance(outcome_index, bool)
        or outcome_index not in (0, 1)
    ):
        raise SettlementIdentityError("OUTCOME_INDEX_INVALID")
    validate_outcome_identity(position.get("outcome"), outcome_index)

    return {
        "settlement_protocol": protocol,
        "settlement_family": family,
        "ctf_contract": ctf_contract,
        "position_collateral": collateral,
        "outcome_index": outcome_index,
    }


def check_settlement(
    position,
    *,
    rpc_url=None,
    heimdall_url=None,
    timeout=DEFAULT_TIMEOUT_SECONDS,
):
    rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL", "")).strip()

    try:
        frozen = _frozen_identity(position)
        finalized = get_finalized_block(
            rpc_url=rpc_url,
            heimdall_url=heimdall_url,
            timeout=timeout,
        )
        block_tag = hex(finalized.number)

        condition = _condition_bytes(position.get("condition_id"))
        denominator = _ctf_uint(
            "payoutDenominator(bytes32)",
            ["bytes32"],
            [condition],
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
        )

        base = {
            "settlement_read_block": finalized.number,
            "settlement_read_block_hash": finalized.block_hash,
            "settlement_authority": CTF_CONTRACT,
            "settlement_finality_source": finalized.source,
        }

        if denominator == 0:
            result = {**base, "status": UNRESOLVED, "reason_code": None}
            try:
                latest = _ctf_uint(
                    "payoutDenominator(bytes32)",
                    ["bytes32"],
                    [condition],
                    rpc_url=rpc_url,
                    block_tag="latest",
                    timeout=timeout,
                )
                if latest > 0:
                    result["status"] = RESOLVED_NOT_FINAL
            except SettlementSourceError:
                result["reason_code"] = "LATEST_DIAGNOSTIC_UNAVAILABLE"
            return result

        observed = resolve_position_identity(
            position.get("condition_id"),
            position.get("token_id"),
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
            validate_chain=False,
        )
        if observed != frozen:
            return {
                "status": IDENTITY_MISMATCH,
                "reason_code": "FROZEN_IDENTITY_MISMATCH",
            }

        numerator0 = _ctf_uint(
            "payoutNumerators(bytes32,uint256)",
            ["bytes32", "uint256"],
            [condition, 0],
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
        )
        numerator1 = _ctf_uint(
            "payoutNumerators(bytes32,uint256)",
            ["bytes32", "uint256"],
            [condition, 1],
            rpc_url=rpc_url,
            block_tag=block_tag,
            timeout=timeout,
        )
        if denominator < 0 or numerator0 < 0 or numerator1 < 0:
            raise SettlementSourceError("PAYOUT_STRUCTURE_INVALID")
        if numerator0 + numerator1 != denominator:
            raise SettlementSourceError("PAYOUT_STRUCTURE_INVALID")
        if (
            frozen["settlement_family"] == FAMILY_NEGRISK
            and (numerator0, numerator1)
            not in ((denominator, 0), (0, denominator))
        ):
            raise SettlementSourceError("NEGRISK_PAYOUT_STRUCTURE_INVALID")

        numerator = (numerator0, numerator1)[frozen["outcome_index"]]
        return {
            **base,
            "status": FINAL_SETTLED,
            "reason_code": None,
            "payout_numerator": numerator,
            "payout_denominator": denominator,
        }

    except SettlementIdentityError as exc:
        return {
            "status": IDENTITY_MISMATCH,
            "reason_code": str(exc) or "IDENTITY_MISMATCH",
        }
    except SettlementSourceError as exc:
        return {
            "status": SETTLEMENT_CHECK_ERROR,
            "reason_code": str(exc) or "SETTLEMENT_CHECK_ERROR",
        }
    except Exception as exc:
        return {
            "status": SETTLEMENT_CHECK_ERROR,
            "reason_code": type(exc).__name__,
        }
