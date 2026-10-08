"""Finalized on-chain settlement resolver for Paper positions.

Authority is Polygon state at a finalized block and the Gnosis Conditional
Tokens Framework (CTF). Gamma/CLOB lifecycle flags are deliberately not used
as resolution authority.

The resolver also proves token_id -> CTF outcome index by deriving the two
valid position ids from each currently supported CTF Exchange V2 family and
requiring exactly one match.
"""

import base64
import os

import requests
from eth_abi import decode, encode
from eth_utils import keccak


CHAIN_ID = 137
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
STANDARD_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
DEFAULT_TIMEOUT_SECONDS = 8
HEIMDALL_REST_URL_ENV = "POLYGON_HEIMDALL_REST_URL"


class SettlementSourceError(RuntimeError):
    """Settlement authority could not be read or validated safely."""


def _selector(signature):
    return keccak(text=signature)[:4]


def _calldata(signature, types=(), values=()):
    return "0x" + (_selector(signature) + encode(list(types), list(values))).hex()


def _bytes32(value, label):
    raw = str(value or "").strip()
    if len(raw) != 66 or not raw.startswith("0x"):
        raise SettlementSourceError(f"{label}_INVALID")
    try:
        data = bytes.fromhex(raw[2:])
    except ValueError as exc:
        raise SettlementSourceError(f"{label}_INVALID") from exc
    if len(data) != 32:
        raise SettlementSourceError(f"{label}_INVALID")
    return data


def _token_int(value):
    raw = str(value or "").strip()
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise SettlementSourceError("TOKEN_ID_INVALID")
    return int(raw)


class PolygonFinalizedCtfResolver:
    def __init__(
        self,
        rpc_url=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        session=None,
        heimdall_rest_url=None,
    ):
        self.rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL", "")).strip()
        if not self.rpc_url:
            raise SettlementSourceError("POLYMARKET_RPC_URL_MISSING")
        self.heimdall_rest_url = str(
            heimdall_rest_url
            if heimdall_rest_url is not None
            else os.getenv(HEIMDALL_REST_URL_ENV, "")
        ).strip().rstrip("/")
        self.timeout = timeout
        self.session = session or requests

    def _rpc(self, method, params):
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=self.timeout,
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
            raise SettlementSourceError("RPC_ERROR")
        if "result" not in payload:
            raise SettlementSourceError("RPC_RESULT_MISSING")
        return payload["result"]

    @staticmethod
    def _parse_rpc_block(result, source):
        if not isinstance(result, dict):
            raise SettlementSourceError(f"{source}_BLOCK_UNAVAILABLE")
        number = result.get("number")
        block_hash = str(result.get("hash") or "").lower()
        if not isinstance(number, str) or not number.startswith("0x"):
            raise SettlementSourceError(f"{source}_BLOCK_NUMBER_INVALID")
        try:
            number_int = int(number, 16)
        except ValueError as exc:
            raise SettlementSourceError(f"{source}_BLOCK_NUMBER_INVALID") from exc
        if number_int < 0 or len(block_hash) != 66 or not block_hash.startswith("0x"):
            raise SettlementSourceError(f"{source}_BLOCK_INVALID")
        return {
            "number": number_int,
            "tag": hex(number_int),
            "hash": block_hash,
            "finality_source": source,
        }

    @staticmethod
    def _milestone_hash(value):
        raw = str(value or "").strip()
        if raw.startswith("0x"):
            try:
                data = bytes.fromhex(raw[2:])
            except ValueError as exc:
                raise SettlementSourceError("HEIMDALL_MILESTONE_HASH_INVALID") from exc
        else:
            try:
                data = base64.b64decode(raw, validate=True)
            except Exception as exc:
                raise SettlementSourceError("HEIMDALL_MILESTONE_HASH_INVALID") from exc
        if len(data) != 32:
            raise SettlementSourceError("HEIMDALL_MILESTONE_HASH_INVALID")
        return "0x" + data.hex()

    def _heimdall_finalized_block(self):
        if not self.heimdall_rest_url:
            raise SettlementSourceError("HEIMDALL_REST_URL_MISSING")

        try:
            response = self.session.get(
                f"{self.heimdall_rest_url}/milestones/latest",
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise SettlementSourceError(f"HEIMDALL_{type(exc).__name__}") from exc
        except ValueError as exc:
            raise SettlementSourceError("HEIMDALL_INVALID_JSON") from exc

        milestone = payload.get("milestone") if isinstance(payload, dict) else None
        if not isinstance(milestone, dict):
            raise SettlementSourceError("HEIMDALL_MILESTONE_INVALID")

        if str(milestone.get("bor_chain_id") or "").strip() != str(CHAIN_ID):
            raise SettlementSourceError("HEIMDALL_CHAIN_ID_MISMATCH")

        end_raw = milestone.get("end_block")
        try:
            end_block = int(end_raw)
        except (TypeError, ValueError) as exc:
            raise SettlementSourceError("HEIMDALL_END_BLOCK_INVALID") from exc
        if end_block < 0:
            raise SettlementSourceError("HEIMDALL_END_BLOCK_INVALID")

        milestone_hash = self._milestone_hash(milestone.get("hash"))
        bor_result = self._rpc("eth_getBlockByNumber", [hex(end_block), False])
        bor_block = self._parse_rpc_block(bor_result, "HEIMDALL_MILESTONE")
        if bor_block["number"] != end_block:
            raise SettlementSourceError("HEIMDALL_BOR_BLOCK_NUMBER_MISMATCH")
        if bor_block["hash"] != milestone_hash:
            raise SettlementSourceError("HEIMDALL_BOR_BLOCK_HASH_MISMATCH")

        bor_block["finality_source"] = "HEIMDALL_V2_MILESTONE"
        return bor_block

    def finalized_block(self):
        rpc_error = None
        try:
            result = self._rpc("eth_getBlockByNumber", ["finalized", False])
            return self._parse_rpc_block(result, "RPC_FINALIZED")
        except SettlementSourceError as exc:
            rpc_error = exc

        if self.heimdall_rest_url:
            return self._heimdall_finalized_block()
        raise rpc_error

    def _eth_call(self, to, data, block_tag):
        result = self._rpc(
            "eth_call",
            [{"to": to, "data": data}, block_tag],
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise SettlementSourceError("ETH_CALL_INVALID")
        try:
            return bytes.fromhex(result[2:])
        except ValueError as exc:
            raise SettlementSourceError("ETH_CALL_INVALID") from exc

    def _decode_call(self, to, signature, arg_types, args, out_types, block_tag):
        raw = self._eth_call(
            to,
            _calldata(signature, arg_types, args),
            block_tag,
        )
        try:
            values = decode(list(out_types), raw)
        except Exception as exc:
            raise SettlementSourceError("ETH_CALL_DECODE_ERROR") from exc
        if len(values) != len(out_types):
            raise SettlementSourceError("ETH_CALL_DECODE_ERROR")
        return values

    def _ctf_collateral(self, exchange, block_tag):
        (address_value,) = self._decode_call(
            exchange,
            "getCtfCollateral()",
            (),
            (),
            ("address",),
            block_tag,
        )
        return str(address_value).lower()

    def _outcome_slot_count(self, condition, block_tag):
        (count,) = self._decode_call(
            CTF_CONTRACT,
            "getOutcomeSlotCount(bytes32)",
            ("bytes32",),
            (condition,),
            ("uint256",),
            block_tag,
        )
        return int(count)

    def _position_id(self, collateral, condition, index_set, block_tag):
        (collection_id,) = self._decode_call(
            CTF_CONTRACT,
            "getCollectionId(bytes32,bytes32,uint256)",
            ("bytes32", "bytes32", "uint256"),
            (bytes(32), condition, int(index_set)),
            ("bytes32",),
            block_tag,
        )
        (position_id,) = self._decode_call(
            CTF_CONTRACT,
            "getPositionId(address,bytes32)",
            ("address", "bytes32"),
            (collateral, collection_id),
            ("uint256",),
            block_tag,
        )
        return int(position_id)

    def _binding_at(self, token_id, condition_id, block):
        token = _token_int(token_id)
        condition = _bytes32(condition_id, "CONDITION_ID")
        slot_count = self._outcome_slot_count(condition, block["tag"])
        if slot_count != 2:
            raise SettlementSourceError("OUTCOME_SLOT_COUNT_UNSUPPORTED")

        families = (
            ("STANDARD_CTF_V2", STANDARD_EXCHANGE_V2),
            ("NEG_RISK_CTF_V2", NEG_RISK_EXCHANGE_V2),
        )
        matches = []
        for family, exchange in families:
            collateral = self._ctf_collateral(exchange, block["tag"])
            for outcome_index in (0, 1):
                index_set = 1 << outcome_index
                candidate = self._position_id(
                    collateral,
                    condition,
                    index_set,
                    block["tag"],
                )
                if candidate == token:
                    matches.append(
                        {
                            "market_family": family,
                            "exchange_contract": exchange.lower(),
                            "ctf_collateral": collateral,
                            "outcome_index": outcome_index,
                            "index_set": index_set,
                        }
                    )

        if len(matches) != 1:
            raise SettlementSourceError(
                "TOKEN_OUTCOME_BINDING_NOT_UNIQUE"
                if matches
                else "TOKEN_OUTCOME_BINDING_NOT_FOUND"
            )
        return matches[0]

    def bind_position(self, token_id, condition_id):
        block = self.finalized_block()
        binding = self._binding_at(token_id, condition_id, block)
        return {
            "source": "POLYGON_FINALIZED_CTF",
            "chain_id": CHAIN_ID,
            "ctf_contract": CTF_CONTRACT.lower(),
            "condition_id": str(condition_id).strip().lower(),
            "token_id": str(token_id).strip(),
            **binding,
            "verified_block_number": block["number"],
            "verified_block_hash": block["hash"],
            "finality_source": block.get("finality_source"),
        }

    def check_position(self, position):
        if not isinstance(position, dict):
            raise SettlementSourceError("POSITION_INVALID")

        token_id = str(position.get("token_id") or "").strip()
        condition_id = str(position.get("condition_id") or "").strip()
        stored = position.get("settlement_binding")
        if not isinstance(stored, dict):
            raise SettlementSourceError("SETTLEMENT_BINDING_MISSING")

        block = self.finalized_block()
        current = self._binding_at(token_id, condition_id, block)

        for field in (
            "market_family",
            "exchange_contract",
            "ctf_collateral",
            "outcome_index",
            "index_set",
        ):
            stored_value = stored.get(field)
            current_value = current.get(field)
            if isinstance(current_value, str):
                if str(stored_value or "").lower() != current_value.lower():
                    raise SettlementSourceError("SETTLEMENT_BINDING_DRIFT")
            elif stored_value != current_value:
                raise SettlementSourceError("SETTLEMENT_BINDING_DRIFT")

        condition = _bytes32(condition_id, "CONDITION_ID")
        (denominator,) = self._decode_call(
            CTF_CONTRACT,
            "payoutDenominator(bytes32)",
            ("bytes32",),
            (condition,),
            ("uint256",),
            block["tag"],
        )
        denominator = int(denominator)

        base = {
            "source": "POLYGON_FINALIZED_CTF",
            "chain_id": CHAIN_ID,
            "ctf_contract": CTF_CONTRACT.lower(),
            "finalized_block_number": block["number"],
            "finalized_block_hash": block["hash"],
            "finality_source": block.get("finality_source"),
            **current,
        }

        if denominator == 0:
            return {
                **base,
                "status": "UNRESOLVED",
                "payout_denominator": 0,
                "payout_numerators": None,
                "payout_numerator": None,
                "payout_per_token": None,
            }

        numerators = []
        for index in (0, 1):
            (numerator,) = self._decode_call(
                CTF_CONTRACT,
                "payoutNumerators(bytes32,uint256)",
                ("bytes32", "uint256"),
                (condition, index),
                ("uint256",),
                block["tag"],
            )
            numerators.append(int(numerator))

        if denominator <= 0 or any(x < 0 for x in numerators):
            raise SettlementSourceError("PAYOUT_VECTOR_INVALID")
        if sum(numerators) != denominator:
            raise SettlementSourceError("PAYOUT_VECTOR_DENOMINATOR_MISMATCH")

        outcome_index = int(current["outcome_index"])
        numerator = numerators[outcome_index]
        return {
            **base,
            "status": "RESOLVED",
            "payout_denominator": denominator,
            "payout_numerators": numerators,
            "payout_numerator": numerator,
            "payout_per_token": numerator / denominator,
        }


def bind_position(token_id, condition_id):
    return PolygonFinalizedCtfResolver().bind_position(token_id, condition_id)


def check_position(position):
    return PolygonFinalizedCtfResolver().check_position(position)
