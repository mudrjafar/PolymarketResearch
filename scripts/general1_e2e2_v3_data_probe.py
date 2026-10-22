"""Read-only V3 Data API identity smoke probe for the known blocked E2E-2 batch.

No file writes, no wallet/position IDs/URLs emitted, no Collector or Supervisor
startup. Uses existing local Polygon RPC credentials only in process memory.
Do not confuse this metadata check with an executable Book/Paper gate.
"""
import os
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

FIRST_BLOCK = 95883353
LAST_BLOCK = 95883357


def _rpc_from_existing_env():
    value = os.getenv("POLYMARKET_RPC_URL", "").strip()
    if value:
        return value
    root = Path(r"C:\PolymarketResearch")
    for path in (
        root / "PolymarketResearch_machine" / ".env",
        root / "V3_test" / "PolymarketResearch_machine" / ".env",
    ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("POLYMARKET_RPC_URL="):
                return line.split("=", 1)[1].strip()
    return ""


def _supported_ids(logs, collector):
    result = []
    for log in logs:
        if log["_exchange_address"].lower() != collector.EXCHANGE_V3.lower():
            continue
        trade = collector.decode_order_filled(log)
        if trade["taker"].lower() != collector.EXCHANGE_V3.lower():
            continue
        if collector.decode_position_id(trade["asset_id"])["classification"] == "SUPPORTED":
            if trade["asset_id"] not in result:
                result.append(trade["asset_id"])
    return result


def _validate_candidates(ids, payload, collector):
    """Return only non-sensitive per-position result codes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ["RESPONSE_SHAPE_INVALID"] * len(ids)
    rows = payload["data"]
    result = []
    for token in ids:
        candidates = [r for r in rows if isinstance(r, dict) and str(r.get("token_id")) == token]
        if len(candidates) != 1:
            result.append("NOT_FOUND" if not candidates else "AMBIGUOUS")
            continue
        try:
            market = collector._v3_data_token_market(token, candidates[0])
            collector._gamma_identity(token, market)
        except (ValueError, RuntimeError) as exc:
            result.append("IDENTITY_REJECTED:" + type(exc).__name__)
        else:
            result.append("IDENTITY_PASS")
    return result


def main():
    rpc = _rpc_from_existing_env()
    if not rpc.startswith("https://"):
        print("PREFLIGHT: RPC_NOT_CONFIGURED")
        return 2
    os.environ["POLYMARKET_RPC_URL"] = rpc
    from scripts import live_active_trades as collector

    try:
        logs = collector.get_order_filled_logs(FIRST_BLOCK, LAST_BLOCK)
        ids = _supported_ids(logs, collector)
        if not ids:
            print("RESULT: NO_SUPPORTED_POSITIONS")
            return 3
        response = requests.get(
            "https://data-api.polymarket.com/v2/tokens",
            params={"token_id": ",".join(ids)},
            timeout=30,
        )
        print("BLOCK_RANGE:", FIRST_BLOCK, LAST_BLOCK)
        print("SUPPORTED_UNIQUE_POSITIONS:", len(ids))
        print("DATA_API_HTTP:", response.status_code)
        response.raise_for_status()
        results = _validate_candidates(ids, response.json(), collector)
        for i, result in enumerate(results, start=1):
            print("POSITION", i, result)
        print("IDENTITY_PASS_COUNT:", results.count("IDENTITY_PASS"))
        return 0 if results.count("IDENTITY_PASS") == len(ids) else 4
    except requests.RequestException as exc:
        print("NETWORK_FAILURE:", type(exc).__name__)
        return 5
    except Exception as exc:
        # Never print an exception message which might contain a URL/API key.
        print("PROBE_FAILURE:", type(exc).__name__)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
