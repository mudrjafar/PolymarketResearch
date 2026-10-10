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
import sys
from typing import Any

import requests
from eth_abi import decode


POLYGON_CHAIN_ID = 137
EXCHANGE_V3_ADDRESS = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"
GAMMA_API = "https://gamma-api.polymarket.com"
EVENT_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,"
    "uint256,bytes32,bytes32)"
)
# Same topic0 as the Collector V2 decoder; importing the probe needs no hashing
# backend, and the test suite pins this value to the documented event signature.
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
POSITION_LAYOUT = {
    "module_id": "bits 248-255",
    "base_hash": "bits 120-247",
    "arity": "bits 104-119",
    "reserved": "bits 40-103",
    "resolution_chain": "bits 24-39",
    "condition_index": "bits 8-23",
    "outcome_index": "bits 0-7",
}
MODULE_NAMES = {1: "BINARY", 2: "NEG_RISK", 3: "COMBINATORIAL"}


class ProbeError(RuntimeError):
    """A safe, endpoint-redacted read failure."""

    def __init__(self, message: str, *, http_status: int | None = None, rpc_code: int | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.rpc_code = rpc_code


def parse_position_id(value: int) -> dict[str, Any]:
    """Decode the documented PositionId bit fields without guessing identity."""
    if not isinstance(value, int) or value < 0 or value >= 1 << 256:
        raise ValueError("position id is outside uint256")
    module_id = (value >> 248) & 0xFF
    result = {
        "position_id": str(value),
        "module_id": module_id,
        "module": MODULE_NAMES.get(module_id, "UNKNOWN_MODULE"),
        "base_hash": f"0x{((value >> 120) & ((1 << 128) - 1)):032x}",
        "arity": (value >> 104) & 0xFFFF,
        "reserved": f"0x{((value >> 40) & ((1 << 64) - 1)):016x}",
        "resolution_chain": (value >> 24) & 0xFFFF,
        "condition_index": (value >> 8) & 0xFFFF,
        "outcome_index": value & 0xFF,
    }
    if module_id == 1:
        valid = result["arity"] == 0 and result["condition_index"] == 0 and result["outcome_index"] in (0, 1)
    elif module_id == 2:
        valid = result["arity"] > 0 and result["condition_index"] <= result["arity"] and result["outcome_index"] in (0, 1)
    elif module_id == 3:
        # Combinatorial identity semantics are deliberately not inferred here.
        valid = True
    else:
        valid = False
    if not valid or module_id not in MODULE_NAMES:
        result["classification"] = "FAIL_CLOSED"
    elif module_id == 3:
        result["classification"] = "RESEARCH_ONLY"
    else:
        result["classification"] = "RECOGNIZED"
    return result


def decode_order_filled(log: dict[str, Any]) -> dict[str, Any]:
    """Decode the indexed addresses and seven ABI data words of OrderFilled."""
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 4:
        raise ValueError("unexpected indexed topic count")
    if str(topics[0]).lower() != ORDER_FILLED_TOPIC.lower():
        raise ValueError("unexpected event signature")
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("missing event data")
    try:
        side, position_id, maker_amount, taker_amount, fee, builder, metadata = decode(
            ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
            bytes.fromhex(data[2:]),
        )
    except Exception as exc:
        raise ValueError("malformed event data") from exc
    identity = parse_position_id(position_id)
    return {
        "transaction_hash": str(log.get("transactionHash", "")),
        "block_number": int(log.get("blockNumber", "0x0"), 16),
        "side": int(side),
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
            raise ProbeError(f"RPC read failed for {method} ({detail})", http_status=status) from exc
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


def _scan_logs(rpc: JsonRpcReader, address: str, start: int, end: int, chunk_size: int) -> list[Any]:
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
            if (exc.http_status == 413 or exc.rpc_code == -32005) and first < last:
                midpoint = (first + last) // 2
                return fetch_range(first, midpoint) + fetch_range(midpoint + 1, last)
            raise
        if not isinstance(batch, list):
            raise ProbeError("RPC returned an invalid eth_getLogs result")
        return batch

    first = start
    while first <= end:
        last = min(end, first + chunk_size - 1)
        logs.extend(fetch_range(first, last))
        first = last + 1
    return logs


def _gamma_exact_match(position_id: str, session: Any = requests) -> dict[str, Any]:
    """Check for an exact Gamma CLOB token match; a miss is not proof of absence."""
    try:
        response = session.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": position_id, "closed": "false", "limit": 100},
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
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except (TypeError, ValueError):
                token_ids = []
        if isinstance(token_ids, list) and position_id in {str(item) for item in token_ids}:
            return {
                "status": "EXACT_MATCH",
                "condition_id": market.get("conditionId"),
                "question": market.get("question"),
            }
    return {"status": "NO_EXACT_MATCH_NOT_PROOF_OF_ABSENCE"}


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
) -> dict[str, Any]:
    if (start_block is None) != (end_block is None):
        raise ValueError("start_block and end_block must be supplied together")
    if lookback_blocks < 1 or chunk_size < 1 or gamma_sample < 0:
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

    logs = _scan_logs(rpc, exchange_address, start_block, end_block, chunk_size)
    decoded: list[dict[str, Any]] = []
    malformed_logs = 0
    for log in logs:
        try:
            if not isinstance(log, dict):
                raise ValueError("invalid log payload")
            decoded.append(decode_order_filled(log))
        except (AttributeError, TypeError, ValueError):
            malformed_logs += 1

    by_module: Counter[str] = Counter()
    unique_ids: dict[str, dict[str, Any]] = {}
    malformed_positions = 0
    for fill in decoded:
        status = (fill["module"] if fill["classification"] == "RECOGNIZED" else fill["classification"])
        by_module[status] += 1
        if fill["classification"] == "FAIL_CLOSED":
            malformed_positions += 1
        unique_ids.setdefault(fill["position_id"], fill)

    gamma_results = []
    for position_id in list(unique_ids)[:gamma_sample]:
        gamma_results.append({
            "position_id": position_id,
            **_gamma_exact_match(position_id, session=session),
        })
    gamma_counts = Counter(item["status"] for item in gamma_results)
    legacy_exchanges = {
        "0xe111180000d2663c0091e4f400237545b87b996b",
        "0xe2222d279d744050d28e00520010520000310f59",
    }
    return {
        "probe": "POLYMARKET_V2_EXCHANGE_V3_COVERAGE",
        "read_only": True,
        "runtime_authority_changed": False,
        "chain_id": chain_id,
        "exchange_address": exchange_address,
        "event_signature": EVENT_SIGNATURE,
        "event_topic0": ORDER_FILLED_TOPIC,
        "position_id_layout": POSITION_LAYOUT,
        "module_map": MODULE_NAMES,
        "range": {"start_block": start_block, "end_block": end_block, "latest_head": latest,
                  "finalized_block": finalized_end},
        "collector_comparison": {
            "current_collector_monitors_this_exchange": exchange_address.lower() in legacy_exchanges,
            "collector_legacy_exchange_addresses": [
                "0xE111180000d2663C0091e4f400237545B87B996B",
                "0xe2222d279d744050d28e00520010520000310F59",
            ],
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
            "interpretation": "No exact Gamma match is inconclusive; V2 asset identity mapping requires separate validation.",
        },
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Log counts are OrderFilled events, not deduplicated economic trades.",
            "Raw amounts are not labeled USD because collateral identity and decimals are not resolved here.",
            "Position IDs are classified by their documented module field; Combinatorial identity is research-only.",
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
        )
    except (ProbeError, ValueError) as exc:
        print(f"V2 coverage probe failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
