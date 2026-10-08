"""Read finalized Polymarket CTF settlement state from Polygon.

Settlement authority is the legacy Gnosis Conditional Tokens Framework (CTF)
that backs current Polymarket CTF Exchange V2 adapters. This module never uses
Gamma/CLOB market status as a resolution signal and never submits transactions.
"""

import base64
import os
import re

import requests
from eth_utils import keccak


CHAIN_ID = 137
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
STANDARD_ADAPTER = "0xADa100874d00e3331D00F2007a9c336a65009718"
NEG_RISK_ADAPTER = "0xAdA200001000ef00D07553cEE7006808F895c6F1"
DEFAULT_TIMEOUT_SECONDS = 5


class SettlementReadError(RuntimeError):
    pass


def _hex_bytes(value, size, label):
    if not isinstance(value, str):
        raise SettlementReadError(f"{label}_INVALID")
    text = value.strip()
    if not text.startswith("0x"):
        raise SettlementReadError(f"{label}_INVALID")
    raw = text[2:]
    if len(raw) != size * 2 or not re.fullmatch(r"[0-9a-fA-F]+", raw):
        raise SettlementReadError(f"{label}_INVALID")
    return bytes.fromhex(raw)


def _normalize_address(value, label="ADDRESS"):
    return "0x" + _hex_bytes(value, 20, label).hex()


def _normalize_hash(value, label="HASH"):
    return "0x" + _hex_bytes(value, 32, label).hex()


def _condition_bytes(condition_id):
    return _hex_bytes(condition_id, 32, "CONDITION_ID")


def _token_integer(token_id):
    if isinstance(token_id, bool):
        raise SettlementReadError("TOKEN_ID_INVALID")
    try:
        value = int(str(token_id).strip(), 10)
    except (TypeError, ValueError):
        raise SettlementReadError("TOKEN_ID_INVALID") from None
    if value < 0 or value >= 2**256:
        raise SettlementReadError("TOKEN_ID_INVALID")
    return value


def _selector(signature):
    return keccak(text=signature)[:4]


def _uint_word(value):
    if isinstance(value, bool):
        raise SettlementReadError("UINT_INVALID")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise SettlementReadError("UINT_INVALID") from None
    if value < 0 or value >= 2**256:
        raise SettlementReadError("UINT_INVALID")
    return value.to_bytes(32, "big")


def _address_word(address):
    return b"\x00" * 12 + _hex_bytes(address, 20, "ADDRESS")


def _call_data(signature, *words):
    payload = _selector(signature)
    for word in words:
        if not isinstance(word, bytes) or len(word) != 32:
            raise SettlementReadError("ABI_WORD_INVALID")
        payload += word
    return "0x" + payload.hex()


def _decode_output(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SettlementReadError("ETH_CALL_RESULT_INVALID")
    raw = value[2:]
    if len(raw) < 64 or len(raw) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", raw):
        raise SettlementReadError("ETH_CALL_RESULT_INVALID")
    return bytes.fromhex(raw)


def _decode_uint(value):
    raw = _decode_output(value)
    return int.from_bytes(raw[:32], "big")


def _decode_bytes32(value):
    raw = _decode_output(value)
    return "0x" + raw[:32].hex()


def _decode_address(value):
    raw = _decode_output(value)
    word = raw[:32]
    if any(word[:12]):
        raise SettlementReadError("ADDRESS_RESULT_INVALID")
    return "0x" + word[12:].hex()


def _quantity(value, label):
    if isinstance(value, bool):
        raise SettlementReadError(f"{label}_INVALID")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            result = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        except ValueError:
            raise SettlementReadError(f"{label}_INVALID") from None
    else:
        raise SettlementReadError(f"{label}_INVALID")
    if result < 0:
        raise SettlementReadError(f"{label}_INVALID")
    return result


def _milestone_hash(value):
    if not isinstance(value, str) or not value.strip():
        raise SettlementReadError("MILESTONE_HASH_INVALID")
    text = value.strip()
    if text.startswith("0x"):
        return _normalize_hash(text, "MILESTONE_HASH")
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return "0x" + text.lower()
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception:
        raise SettlementReadError("MILESTONE_HASH_INVALID") from None
    if len(raw) != 32:
        raise SettlementReadError("MILESTONE_HASH_INVALID")
    return "0x" + raw.hex()


class SettlementReader:
    def __init__(
        self,
        rpc_url=None,
        heimdall_url=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        rpc_transport=None,
        heimdall_transport=None,
    ):
        self.rpc_url = str(
            rpc_url if rpc_url is not None else os.getenv("POLYMARKET_RPC_URL", "")
        ).strip()
        self.heimdall_url = str(
            heimdall_url
            if heimdall_url is not None
            else os.getenv("POLYGON_HEIMDALL_URL", "")
        ).strip().rstrip("/")
        self.timeout = float(timeout)
        self.rpc_transport = rpc_transport
        self.heimdall_transport = heimdall_transport
        self._rpc_id = 0
        self._chain_verified = False

    def _rpc(self, method, params):
        if self.rpc_transport is not None:
            return self.rpc_transport(method, params)
        if not self.rpc_url:
            raise SettlementReadError("RPC_URL_MISSING")

        self._rpc_id += 1
        try:
            response = requests.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": self._rpc_id,
                    "method": method,
                    "params": params,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SettlementReadError(
                f"RPC_REQUEST_{type(exc).__name__.upper()}"
            ) from exc

        if response.status_code != 200:
            raise SettlementReadError(f"RPC_HTTP_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SettlementReadError("RPC_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise SettlementReadError("RPC_INVALID_PAYLOAD")
        if payload.get("error") is not None:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            suffix = str(code) if code is not None else "UNKNOWN"
            raise SettlementReadError(f"RPC_ERROR_{suffix}")
        if "result" not in payload or payload.get("result") is None:
            raise SettlementReadError("RPC_RESULT_MISSING")
        return payload["result"]

    def _latest_milestone(self):
        if self.heimdall_transport is not None:
            payload = self.heimdall_transport()
        else:
            if not self.heimdall_url:
                raise SettlementReadError("FINALIZED_TAG_UNAVAILABLE")
            try:
                response = requests.get(
                    f"{self.heimdall_url}/milestones/latest",
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise SettlementReadError(
                    f"HEIMDALL_REQUEST_{type(exc).__name__.upper()}"
                ) from exc
            if response.status_code != 200:
                raise SettlementReadError(f"HEIMDALL_HTTP_{response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise SettlementReadError("HEIMDALL_INVALID_JSON") from exc

        if not isinstance(payload, dict):
            raise SettlementReadError("HEIMDALL_INVALID_PAYLOAD")
        if isinstance(payload.get("result"), dict):
            payload = payload["result"]
        milestone = payload.get("milestone")
        if not isinstance(milestone, dict):
            raise SettlementReadError("MILESTONE_MISSING")

        end_block = _quantity(milestone.get("end_block"), "MILESTONE_END_BLOCK")
        block_hash = _milestone_hash(milestone.get("hash"))

        chain = milestone.get("bor_chain_id")
        if chain is not None and str(chain).strip().isdigit():
            if int(str(chain).strip()) != CHAIN_ID:
                raise SettlementReadError("MILESTONE_CHAIN_ID_MISMATCH")

        return {
            "end_block": end_block,
            "hash": block_hash,
            "milestone_id": milestone.get("milestone_id"),
        }

    def _ensure_chain(self):
        if self._chain_verified:
            return
        chain_id = _quantity(self._rpc("eth_chainId", []), "CHAIN_ID")
        if chain_id != CHAIN_ID:
            raise SettlementReadError("CHAIN_ID_MISMATCH")
        self._chain_verified = True

    def finalized_block(self):
        self._ensure_chain()

        finalized_error = None
        try:
            block = self._rpc("eth_getBlockByNumber", ["finalized", False])
            return self._validate_block(block, source="RPC_FINALIZED")
        except SettlementReadError as exc:
            finalized_error = exc

        if not (self.heimdall_url or self.heimdall_transport is not None):
            raise SettlementReadError("FINALITY_UNAVAILABLE") from finalized_error

        milestone = self._latest_milestone()
        expected_number = milestone["end_block"]
        block = self._rpc(
            "eth_getBlockByNumber",
            [hex(expected_number), False],
        )
        result = self._validate_block(block, source="HEIMDALL_MILESTONE")
        if result["number"] != expected_number:
            raise SettlementReadError("MILESTONE_BLOCK_NUMBER_MISMATCH")
        if result["hash"].lower() != milestone["hash"].lower():
            raise SettlementReadError("MILESTONE_BLOCK_HASH_MISMATCH")
        result["milestone_id"] = milestone.get("milestone_id")
        return result

    @staticmethod
    def _validate_block(block, source):
        if not isinstance(block, dict):
            raise SettlementReadError("FINALIZED_BLOCK_INVALID")
        number = _quantity(block.get("number"), "FINALIZED_BLOCK_NUMBER")
        block_hash = _normalize_hash(block.get("hash"), "FINALIZED_BLOCK_HASH")
        return {
            "number": number,
            "hash": block_hash,
            "source": source,
        }

    def _eth_call(self, address, data, block_number):
        address = _normalize_address(address)
        return self._rpc(
            "eth_call",
            [{"to": address, "data": data}, hex(int(block_number))],
        )

    def _read_address(self, contract, signature, block_number):
        value = self._eth_call(contract, _call_data(signature), block_number)
        return _decode_address(value)

    def _read_uint(self, contract, signature, block_number, *words):
        value = self._eth_call(
            contract,
            _call_data(signature, *words),
            block_number,
        )
        return _decode_uint(value)

    def _read_bytes32(self, contract, signature, block_number, *words):
        value = self._eth_call(
            contract,
            _call_data(signature, *words),
            block_number,
        )
        return _decode_bytes32(value)

    def _adapter_state(self, block_number):
        standard_ctf = self._read_address(
            STANDARD_ADAPTER, "CONDITIONAL_TOKENS()", block_number
        )
        neg_ctf = self._read_address(
            NEG_RISK_ADAPTER, "CONDITIONAL_TOKENS()", block_number
        )
        expected = _normalize_address(CTF_ADDRESS)
        if standard_ctf.lower() != expected.lower() or neg_ctf.lower() != expected.lower():
            raise SettlementReadError("CTF_CONTRACT_MISMATCH")

        usdce = self._read_address(STANDARD_ADAPTER, "USDCE()", block_number)
        wrapped = self._read_address(
            NEG_RISK_ADAPTER, "WRAPPED_COLLATERAL()", block_number
        )
        if usdce.lower() == wrapped.lower():
            raise SettlementReadError("COLLATERAL_FAMILY_AMBIGUOUS")

        return {
            "ctf": expected,
            "standard_collateral": usdce,
            "neg_risk_collateral": wrapped,
        }

    def _position_id(self, ctf, collateral, condition, index_set, block_number):
        collection_id = self._read_bytes32(
            ctf,
            "getCollectionId(bytes32,bytes32,uint256)",
            block_number,
            bytes(32),
            condition,
            _uint_word(index_set),
        )
        return self._read_uint(
            ctf,
            "getPositionId(address,bytes32)",
            block_number,
            _address_word(collateral),
            _hex_bytes(collection_id, 32, "COLLECTION_ID"),
        )

    def read(self, condition_id, token_id):
        condition = _condition_bytes(condition_id)
        normalized_condition = "0x" + condition.hex()
        target_token = _token_integer(token_id)
        finality = self.finalized_block()
        block_number = finality["number"]
        ctf = _normalize_address(CTF_ADDRESS)

        denominator = self._read_uint(
            ctf,
            "payoutDenominator(bytes32)",
            block_number,
            condition,
        )
        base = {
            "condition_id": normalized_condition,
            "token_id": str(target_token),
            "finalized_block_number": block_number,
            "finalized_block_hash": finality["hash"],
            "finality_source": finality["source"],
            "ctf_contract": ctf,
            "payout_denominator": denominator,
        }
        if finality.get("milestone_id") is not None:
            base["milestone_id"] = finality.get("milestone_id")

        # payoutDenominator == 0 is the CTF's unresolved state. Do not spend
        # extra RPC calls deriving token identity until a payout vector exists.
        if denominator == 0:
            base.update(
                {
                    "status": "UNRESOLVED",
                    "market_family": None,
                    "collateral": None,
                    "outcome_index": None,
                    "outcome": None,
                    "index_set": None,
                    "payout_numerators": None,
                    "payout_numerator": None,
                    "payout_per_token": None,
                }
            )
            return base

        adapters = self._adapter_state(block_number)
        ctf = adapters["ctf"]
        base["ctf_contract"] = ctf

        slot_count = self._read_uint(
            ctf,
            "getOutcomeSlotCount(bytes32)",
            block_number,
            condition,
        )
        if slot_count != 2:
            raise SettlementReadError("OUTCOME_SLOT_COUNT_NOT_BINARY")

        matches = []
        families = (
            ("STANDARD", adapters["standard_collateral"]),
            ("NEG_RISK", adapters["neg_risk_collateral"]),
        )
        for family, collateral in families:
            for outcome_index, index_set, label in (
                (0, 1, "YES"),
                (1, 2, "NO"),
            ):
                position_id = self._position_id(
                    ctf,
                    collateral,
                    condition,
                    index_set,
                    block_number,
                )
                if position_id == target_token:
                    matches.append(
                        {
                            "market_family": family,
                            "collateral": collateral,
                            "outcome_index": outcome_index,
                            "outcome": label,
                            "index_set": index_set,
                        }
                    )

        if len(matches) != 1:
            code = "TOKEN_BINDING_NOT_FOUND" if not matches else "TOKEN_BINDING_AMBIGUOUS"
            raise SettlementReadError(code)
        binding = matches[0]

        numerators = [
            self._read_uint(
                ctf,
                "payoutNumerators(bytes32,uint256)",
                block_number,
                condition,
                _uint_word(index),
            )
            for index in (0, 1)
        ]
        if sum(numerators) != denominator:
            raise SettlementReadError("PAYOUT_VECTOR_INVALID")

        numerator = numerators[binding["outcome_index"]]
        base.update(
            {
                "status": "SETTLED",
                "market_family": binding["market_family"],
                "collateral": binding["collateral"],
                "outcome_index": binding["outcome_index"],
                "outcome": binding["outcome"],
                "index_set": binding["index_set"],
                "payout_numerators": numerators,
                "payout_numerator": numerator,
                "payout_per_token": numerator / denominator,
            }
        )
        return base


_DEFAULT_READER = None


def read_settlement(condition_id, token_id):
    global _DEFAULT_READER
    if _DEFAULT_READER is None:
        _DEFAULT_READER = SettlementReader()
    return _DEFAULT_READER.read(condition_id, token_id)
