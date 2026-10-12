"""Read-only coverage probe for Polymarket's V2 Exchange V3 deployment.

This utility reads finalized Polygon logs and, optionally, asks the public Gamma
API whether sampled position IDs have an exact CLOB token identity match. It
does not write runtime data, update Collector state, or feed any decision worker.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
import re
import sys
from typing import Any

import requests
from eth_abi import decode, encode

try:  # Support both `python scripts/...py` and package-based tests.
    from .v3_identity import MODULE_NAMES as RUNTIME_MODULE_NAMES, V3IdentityError, decode_position_id
except ImportError:  # pragma: no cover - used by direct script execution
    from v3_identity import MODULE_NAMES as RUNTIME_MODULE_NAMES, V3IdentityError, decode_position_id


POLYGON_CHAIN_ID = 137
EXCHANGE_V3_ADDRESS = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"
COMBINATORIAL_MODULE_ADDRESS = "0x30000034706C7d8e12009DAB006Be20000c031A8"
GAMMA_API = "https://gamma-api.polymarket.com"
EVENT_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,"
    "uint256,bytes32,bytes32)"
)
# Same topic0 as the Collector V2 decoder; importing the probe needs no hashing
# backend, and the test suite pins this value to the documented event signature.
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
GET_COMBINATORIAL_LEGS_SELECTOR = "0x9989b1ed"  # getLegs(bytes31)
POSITION_LAYOUT = {
    "module_id": "bits 248-255",
    "base_hash": "bits 120-247",
    "arity": "bits 104-119",
    "reserved": "bits 40-103",
    "resolution_chain": "bits 24-39",
    "condition_index": "bits 8-23",
    "outcome_index": "bits 0-7",
}
MODULE_NAMES = RUNTIME_MODULE_NAMES


class ProbeError(RuntimeError):
    """A safe, endpoint-redacted read failure."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        rpc_code: int | None = None,
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.rpc_code = rpc_code
        self.timed_out = timed_out


def parse_position_id(value: int) -> dict[str, Any]:
    """Compatibility wrapper around the Runtime's single PositionId decoder."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("position id is invalid")
    try:
        return decode_position_id(str(value))
    except V3IdentityError as exc:
        raise ValueError(str(exc)) from exc


def _canonical_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError(f"invalid {label}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    return value.lower()


def _hex_log_integer(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        raise ValueError(f"invalid {label}")
    try:
        parsed = int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if parsed < 0:
        raise ValueError(f"invalid {label}")
    return parsed


def _indexed_address(topic: Any, label: str) -> str:
    word = _canonical_hash(topic, label)
    if int(word[2:26], 16) != 0:
        raise ValueError(f"invalid {label}")
    return "0x" + word[-40:]


def decode_order_filled(log: dict[str, Any]) -> dict[str, Any]:
    """Decode an OrderFilled event with canonical, Collector-compatible evidence identity.

    This only establishes event identity and provenance; it does not prove that
    an RPC provider returned every log in a block range.
    """
    if not isinstance(log, dict) or log.get("removed") is True:
        raise ValueError("invalid or removed log")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 4:
        raise ValueError("unexpected indexed topic count")
    if str(topics[0]).lower() != ORDER_FILLED_TOPIC.lower():
        raise ValueError("unexpected event signature")
    _canonical_hash(topics[1], "order hash")
    maker = _indexed_address(topics[2], "maker address topic")
    taker = _indexed_address(topics[3], "taker address topic")
    tx_hash = _canonical_hash(log.get("transactionHash"), "transaction hash")
    block_hash = _canonical_hash(log.get("blockHash"), "block hash")
    log_index = _hex_log_integer(log.get("logIndex"), "log index")
    block_number = _hex_log_integer(log.get("blockNumber"), "block number")
    address = log.get("address")
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError("invalid exchange address")
    try:
        int(address[2:], 16)
    except ValueError as exc:
        raise ValueError("invalid exchange address") from exc
    address = address.lower()
    data = log.get("data")
    if (
        not isinstance(data, str)
        or not data.startswith("0x")
        or re.fullmatch(r"[0-9a-fA-F]{448}", data[2:]) is None
    ):
        raise ValueError("malformed event data encoding or length")
    try:
        side, position_id, maker_amount, taker_amount, fee, builder, metadata = decode(
            ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
            bytes.fromhex(data[2:]),
        )
    except Exception as exc:
        raise ValueError("malformed event data") from exc
    if side not in (0, 1):
        raise ValueError("invalid BUY/SELL side")
    identity = parse_position_id(position_id)
    return {
        "evidence_id": f"{tx_hash}:{log_index}",
        "transaction_hash": tx_hash,
        "log_index": log_index,
        "block_number": block_number,
        "block_hash": block_hash,
        "exchange_address": address,
        "order_hash": str(topics[1]).lower(),
        "maker": maker,
        "taker": taker,
        "taker_aggressor": taker == address,
        "flow_role": "TAKER_AGGRESSOR" if taker == address else "MAKER_OR_OTHER",
        "side": int(side),
        "side_label": "BUY" if side == 0 else "SELL",
        "maker_amount_raw": str(maker_amount),
        "taker_amount_raw": str(taker_amount),
        "fee_raw": str(fee),
        "builder": "0x" + bytes(builder).hex(),
        "metadata": "0x" + bytes(metadata).hex(),
        **identity,
    }


class JsonRpcReader:
    def __init__(self, url: str, session: Any = requests):
        self._url = url
        self._session = session
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        response = None
        try:
            response = self._session.post(
                self._url,
                json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            failed_response = getattr(exc, "response", None)
            if failed_response is None:
                failed_response = response
            status = getattr(failed_response, "status_code", None)
            detail = f"HTTP {status}" if status is not None else type(exc).__name__
            raise ProbeError(
                f"RPC read failed for {method} ({detail})",
                http_status=status,
                timed_out=isinstance(exc, requests.exceptions.Timeout),
            ) from exc
        if not isinstance(body, dict) or body.get("id") != self._request_id:
            raise ProbeError(f"RPC response invalid for {method}")
        if body.get("error"):
            code = body["error"].get("code") if isinstance(body["error"], dict) else None
            raise ProbeError(f"RPC returned an error for {method} (code={code})", rpc_code=code)
        if "result" not in body:
            raise ProbeError(f"RPC response missing result for {method}")
        return body["result"]


def _hex_block(value: Any, label: str) -> int:
    try:
        block = int(value, 16)
    except (TypeError, ValueError) as exc:
        raise ProbeError(f"RPC returned an invalid {label}") from exc
    if block < 0:
        raise ProbeError(f"RPC returned an invalid {label}")
    return block


def _scan_logs(
    rpc: JsonRpcReader,
    address: str,
    start: int,
    end: int,
    chunk_size: int,
    max_logs_per_response: int = 1_000,
) -> list[Any]:
    """Scan non-overlapping ranges, bisection on provider errors and known ceilings.

    A provider can silently truncate below any assumed limit. Successful calls
    remain UNVERIFIED until corroborated independently (receipts or second RPC).
    """
    if chunk_size < 1 or max_logs_per_response < 1:
        raise ValueError("chunk and response ceilings must be positive")
    logs: list[Any] = []

    def fetch_range(first: int, last: int) -> list[Any]:
        try:
            batch = rpc.call("eth_getLogs", [{
                "address": address,
                "fromBlock": hex(first),
                "toBlock": hex(last),
                "topics": [ORDER_FILLED_TOPIC],
            }])
        except ProbeError as exc:
            if (
                exc.http_status == 413 or exc.rpc_code == -32005 or exc.timed_out
            ) and first < last:
                middle = (first + last) // 2
                return fetch_range(first, middle) + fetch_range(middle + 1, last)
            raise
        if not isinstance(batch, list):
            raise ProbeError("RPC returned an invalid eth_getLogs result")
        if len(batch) >= max_logs_per_response:
            if first == last:
                raise ProbeError("Single-block log count reached provider ceiling; completeness unknown")
            middle = (first + last) // 2
            return fetch_range(first, middle) + fetch_range(middle + 1, last)
        for item in batch:
            if not isinstance(item, dict):
                raise ProbeError("RPC returned a non-object log")
            try:
                block = _hex_log_integer(item.get("blockNumber"), "log block number")
            except ValueError as exc:
                raise ProbeError("RPC returned a log with invalid block number") from exc
            if not first <= block <= last:
                raise ProbeError("RPC returned a log outside the requested block range")
            reported_address = item.get("address")
            if reported_address is not None and str(reported_address).lower() != address.lower():
                raise ProbeError("RPC returned a log from an unexpected exchange")
        return batch

    first = start
    while first <= end:
        last = min(end, first + chunk_size - 1)
        logs.extend(fetch_range(first, last))
        first = last + 1
    return logs


def _gamma_exact_match(position_id: str, session: Any = requests) -> dict[str, Any]:
    """Check Gamma's position-ID identity field; a miss is not proof of absence."""
    try:
        response = session.get(
            f"{GAMMA_API}/markets",
            params={"position_ids": position_id, "limit": 100},
            timeout=15,
        )
        response.raise_for_status()
        markets = response.json()
    except Exception:
        return {"status": "LOOKUP_ERROR"}
    if not isinstance(markets, list):
        return {"status": "INVALID_RESPONSE"}
    for market in markets:
        if not isinstance(market, dict):
            continue
        position_ids = market.get("positionIds", [])
        if isinstance(position_ids, str):
            try:
                position_ids = json.loads(position_ids)
            except (TypeError, ValueError):
                position_ids = []
        if isinstance(position_ids, list) and position_id in {str(item) for item in position_ids}:
            return {
                "status": "EXACT_MATCH",
                "condition_id": market.get("conditionId"),
                "question": market.get("question"),
            }
    return {"status": "NO_POSITION_ID_MATCH_NOT_PROOF_OF_ABSENCE"}


def _combinatorial_legs(rpc: JsonRpcReader, position_id: str, block_tag: str = "latest") -> list[str] | None:
    """Read a prepared combinatorial condition's canonical underlying positions."""
    try:
        condition_id = (int(position_id) >> 8).to_bytes(31, "big")
        call_data = GET_COMBINATORIAL_LEGS_SELECTOR + encode(["bytes31"], [condition_id]).hex()
        result = rpc.call("eth_call", [
            {"to": COMBINATORIAL_MODULE_ADDRESS, "data": call_data},
            block_tag,
        ])
        if not isinstance(result, str) or not result.startswith("0x"):
            return None
        decoded = decode(["uint256[]"], bytes.fromhex(result[2:]))
        return [str(value) for value in decoded[0]]
    except Exception:
        return None


def _rpc_url_from_config(env_file: str | None) -> str:
    """Read only POLYMARKET_RPC_URL; never return file contents or report errors verbatim."""
    configured = os.getenv("POLYMARKET_RPC_URL", "").strip()
    if configured or not env_file:
        return configured
    try:
        with open(env_file, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                candidate = line.strip()
                if not candidate or candidate.startswith("#"):
                    continue
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                key, separator, value = candidate.partition("=")
                if separator and key.strip() == "POLYMARKET_RPC_URL":
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    return value.strip()
    except (OSError, UnicodeError) as exc:
        raise ProbeError("Could not read RPC configuration file") from exc
    return ""


def run_probe(
    rpc_url: str,
    start_block: int | None = None,
    end_block: int | None = None,
    lookback_blocks: int = 5_000,
    chunk_size: int = 500,
    gamma_sample: int = 25,
    exchange_address: str = EXCHANGE_V3_ADDRESS,
    session: Any = requests,
    max_logs_per_response: int = 1_000,
) -> dict[str, Any]:
    if (start_block is None) != (end_block is None):
        raise ValueError("start_block and end_block must be supplied together")
    if lookback_blocks < 1 or chunk_size < 1 or gamma_sample < 0 or max_logs_per_response < 1:
        raise ValueError("probe limits must be non-negative and block limits positive")
    if not exchange_address.startswith("0x") or len(exchange_address) != 42:
        raise ValueError("exchange address must be a 20-byte hex address")
    rpc = JsonRpcReader(rpc_url, session=session)
    chain_id = _hex_block(rpc.call("eth_chainId", []), "chain id")
    if chain_id != POLYGON_CHAIN_ID:
        raise ProbeError(f"Wrong chain: expected Polygon ({POLYGON_CHAIN_ID}), received {chain_id}")
    latest = _hex_block(rpc.call("eth_blockNumber", []), "block number")
    finalized_block = rpc.call("eth_getBlockByNumber", ["finalized", False])
    if not isinstance(finalized_block, dict) or "number" not in finalized_block:
        raise ProbeError("RPC returned no finalized Polygon block")
    finalized_end = _hex_block(finalized_block["number"], "finalized block")
    if finalized_end > latest:
        raise ProbeError("RPC finalized block is ahead of the chain head")
    if start_block is None:
        end_block = finalized_end
        start_block = max(0, end_block - lookback_blocks + 1)
    if start_block < 0 or end_block is None or end_block < start_block:
        raise ValueError("invalid inclusive block range")
    if end_block > finalized_end:
        raise ValueError("end_block must be at or below the finalized Polygon block")

    logs = _scan_logs(
        rpc, exchange_address, start_block, end_block, chunk_size,
        max_logs_per_response=max_logs_per_response,
    )
    decoded: list[dict[str, Any]] = []
    malformed_logs = 0
    for log in logs:
        try:
            fill = decode_order_filled(log)
            if fill["exchange_address"] != exchange_address.lower():
                raise ValueError("log is from the wrong exchange")
            if not start_block <= fill["block_number"] <= end_block:
                raise ValueError("log is outside the requested block range")
            decoded.append(fill)
        except (AttributeError, TypeError, ValueError):
            malformed_logs += 1

    decoded.sort(key=lambda r: (r["block_number"], r["log_index"], r["transaction_hash"]))
    unique_events: dict[str, dict[str, Any]] = {}
    duplicate_events = 0
    conflicting_duplicate_events = 0
    for fill in decoded:
        key = fill["evidence_id"]
        previous = unique_events.get(key)
        if previous is None:
            unique_events[key] = fill
        elif previous == fill:
            duplicate_events += 1
        else:
            conflicting_duplicate_events += 1

    unique_fills = list(unique_events.values())
    by_module: Counter[str] = Counter()
    unique_ids: dict[str, dict[str, Any]] = {}
    malformed_positions = 0
    for fill in unique_fills:
        status = fill["module"] if fill["classification"] == "SUPPORTED" else fill["classification"]
        by_module[status] += 1
        if fill["classification"] == "FAIL_CLOSED":
            malformed_positions += 1
        unique_ids.setdefault(fill["position_id"], fill)

    gamma_results = []
    combinatorial_results = []
    for position_id in list(unique_ids)[:gamma_sample]:
        gamma_results.append({
            "position_id": position_id,
            **_gamma_exact_match(position_id, session=session),
        })
        identity = unique_ids[position_id]
        if identity["module_id"] == 3:
            # Anchor module state to the exact finalized evidence block, not "latest".
            legs = _combinatorial_legs(rpc, position_id, hex(identity["block_number"]))
            if legs is None:
                combinatorial_results.append({
                    "position_id": position_id,
                    "condition_id": f"0x{(int(position_id) >> 8):062x}",
                    "status": "LEG_LOOKUP_ERROR_OR_UNPREPARED",
                })
                continue
            leg_results = []
            for leg_position_id in legs:
                leg_identity = parse_position_id(int(leg_position_id))
                leg_results.append({
                    "position_id": leg_position_id,
                    "module": leg_identity["module"],
                    "outcome_index": leg_identity["outcome_index"],
                    **_gamma_exact_match(leg_position_id, session=session),
                })
            combinatorial_results.append({
                "position_id": position_id,
                "condition_id": f"0x{(int(position_id) >> 8):062x}",
                "status": "LEGS_RESOLVED",
                "leg_count": len(legs),
                "legs": leg_results,
            })
    gamma_counts = Counter(item["status"] for item in gamma_results)
    legacy_exchanges = {
        "0xe111180000d2663c0091e4f400237545b87b996b",
        "0xe2222d279d744050d28e00520010520000310f59",
    }
    reconciliation_blocked = bool(
        malformed_logs or conflicting_duplicate_events or malformed_positions
    )
    return {
        "probe": "POLYMARKET_V2_EXCHANGE_V3_COVERAGE",
        "read_only": True,
        "runtime_authority_changed": False,
        "chain_id": chain_id,
        "exchange_address": exchange_address.lower(),
        "event_signature": EVENT_SIGNATURE,
        "event_topic0": ORDER_FILLED_TOPIC,
        "position_id_layout": POSITION_LAYOUT,
        "module_map": MODULE_NAMES,
        "range": {
            "start_block": start_block, "end_block": end_block,
            "latest_head": latest, "finalized_block": finalized_end,
        },
        "collector_comparison": {
            "current_collector_monitors_this_exchange": exchange_address.lower() in legacy_exchanges,
            "collector_legacy_exchange_addresses": [
                "0xE111180000d2663C0091e4f400237545B87B996B",
                "0xe2222d279d744050d28e00520010520000310F59",
            ],
        },
        "reconciliation": {
            "status": "FAIL_CLOSED" if reconciliation_blocked else "EVIDENCE_KEYS_VALID_ONLY",
            "rpc_result_completeness": "UNVERIFIED_NO_INDEPENDENT_LOG_COUNT",
            "completeness_claim_allowed": False,
            "max_logs_per_response": max_logs_per_response,
            "evidence_id_format": "lowercase_transaction_hash:decimal_log_index",
            "duplicate_events": duplicate_events,
            "conflicting_duplicate_events": conflicting_duplicate_events,
            "malformed_event_count": malformed_logs,
            "invalid_position_id_events": malformed_positions,
            "unique_event_count": len(unique_fills),
            "taker_aggressor_events": sum(x["taker_aggressor"] for x in unique_fills),
            "maker_or_other_events": sum(not x["taker_aggressor"] for x in unique_fills),
            "unique_event_samples": unique_fills[:10],
        },
        "activity": {
            "order_filled_logs": len(logs),
            "decoded_fills": len(decoded),
            "malformed_logs": malformed_logs,
            "unique_position_ids": len(unique_ids),
            "fills_by_module_or_fail_closed": dict(sorted(by_module.items())),
            "fail_closed_position_ids": malformed_positions,
            "unique_position_id_samples": list(unique_ids.values())[:10],
        },
        "gamma_identity_probe": {
            "sampled_position_ids": len(gamma_results),
            "result_counts": dict(sorted(gamma_counts.items())),
            "results": gamma_results,
            "interpretation": "No exact Gamma position-ID match is inconclusive; V2 asset identity mapping requires separate validation.",
        },
        "combinatorial_identity_probe": {
            "sampled_combinatorial_positions": len(combinatorial_results),
            "resolved_conditions": sum(item["status"] == "LEGS_RESOLVED" for item in combinatorial_results),
            "results": combinatorial_results,
            "interpretation": "Combinatorial positions resolve through historical CombinatorialModule getLegs(bytes31) calls at their evidence block; Gamma is queried for underlying leg position IDs.",
        },
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Even valid eth_getLogs responses do not prove completeness without independent receipts or a second provider.",
            "Raw OrderFilled event counts include maker-side events and are not economic trade counts.",
            "Duplicate event IDs are counted separately; conflicting duplicates and malformed events fail closed.",
            "No raw amounts are labeled USD: collateral identity and decimals are not established by this probe.",
            "Position IDs are classified by module; Combinatorial positions remain research-only.",
            "No output file, Collector database, production snapshot, or worker input is written.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Polygon probe for Polymarket V2 Exchange V3 coverage")
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--end-block", type=int)
    parser.add_argument("--lookback-blocks", type=int, default=5_000)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--gamma-sample", type=int, default=25)
    parser.add_argument("--max-logs-per-response", type=int, default=1_000)
    parser.add_argument("--exchange-address", default=EXCHANGE_V3_ADDRESS)
    parser.add_argument("--env-file", help="optional .env file; only POLYMARKET_RPC_URL is read")
    args = parser.parse_args(argv)
    try:
        rpc_url = _rpc_url_from_config(args.env_file)
    except ProbeError as exc:
        print(f"V2 coverage probe failed: {exc}", file=sys.stderr)
        return 2
    if not rpc_url:
        print("Missing POLYMARKET_RPC_URL configuration; RPC endpoint was not contacted.", file=sys.stderr)
        return 2
    try:
        report = run_probe(
            rpc_url,
            start_block=args.start_block,
            end_block=args.end_block,
            lookback_blocks=args.lookback_blocks,
            chunk_size=args.chunk_size,
            gamma_sample=args.gamma_sample,
            exchange_address=args.exchange_address,
            max_logs_per_response=args.max_logs_per_response,
        )
    except (ProbeError, ValueError) as exc:
        print(f"V2 coverage probe failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
