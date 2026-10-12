"""Read-only receipt provenance and position-identity audit for V2 coverage.

Consumes a saved V2-COVERAGE-1A JSON report. It checks sampled event evidence
against transaction receipts and canonical block headers from the configured
RPC provider, and independently looks up sampled PositionIds in Gamma and the
public active-combo catalog. It does not submit orders, update Collector
state, or claim scan completeness.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
import time
from typing import Any

import requests

CLOB_API = "https://clob.polymarket.com"
COMBO_API = "https://combos-rfq-api.polymarket.com"
COMBO_PAGE_SIZE = 100
GAMMA_TIMEOUT_SECONDS = 20
GAMMA_MAX_RETRIES = 3


def _gamma_get_with_retry(session: Any, url: str, *, params: dict[str, Any]) -> tuple[Any, int]:
    """Retry transient Gamma transport and server errors without hiding hard failures."""
    retries = 0
    while True:
        try:
            response = session.get(url, params=params, timeout=GAMMA_TIMEOUT_SECONDS)
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in (408, 425, 429) and not (isinstance(status, int) and status >= 500):
                    exc.gamma_retries = retries
                    raise
                if retries >= GAMMA_MAX_RETRIES:
                    exc.gamma_retries = retries
                    raise
                retries += 1
                time.sleep(min(2 ** (retries - 1), 8))
                continue
            return response, retries
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if retries >= GAMMA_MAX_RETRIES:
                exc.gamma_retries = retries
                raise
            retries += 1
            time.sleep(min(2 ** (retries - 1), 8))

try:  # Support both `python scripts/...py` and package-based tests.
    from .v2_coverage_probe import (
        GAMMA_API,
        EXCHANGE_V3_ADDRESS,
        JsonRpcReader,
        POLYGON_CHAIN_ID,
        ProbeError,
        _hex_block,
        _rpc_url_from_config,
        _scan_logs,
        decode_order_filled,
    )
    from .v3_identity import V3IdentityError, condition_matches_position, decode_position_id
except ImportError:  # pragma: no cover - used by direct script execution
    from v2_coverage_probe import (
        GAMMA_API,
        EXCHANGE_V3_ADDRESS,
        JsonRpcReader,
        POLYGON_CHAIN_ID,
        ProbeError,
        _hex_block,
        _rpc_url_from_config,
        _scan_logs,
        decode_order_filled,
    )
    from v3_identity import V3IdentityError, condition_matches_position, decode_position_id


def _json_array(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, list) else None
    return None


def _same_event(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    fields = (
        "evidence_id", "transaction_hash", "log_index", "block_number",
        "block_hash", "exchange_address", "order_hash", "maker", "taker",
        "taker_aggressor", "flow_role", "side", "side_label",
        "maker_amount_raw", "taker_amount_raw", "fee_raw", "builder",
        "metadata", "position_id", "module_id", "module", "classification",
    )
    return all(expected.get(field) == observed.get(field) for field in fields)


def decode_v2_position_id(position_id: str) -> dict[str, Any]:
    """Present the Runtime decoder's decision in the audit's legacy schema."""
    try:
        identity = decode_position_id(position_id)
    except (V3IdentityError, TypeError, ValueError):
        return {"position_id": position_id, "status": "INVALID_POSITION_ID_ENCODING"}
    if identity["classification"] == "FAIL_CLOSED":
        return {
            "position_id": identity["position_id"],
            "status": "UNRECOGNIZED_V2_POSITION_ID_LAYOUT",
            **identity,
        }
    module = {"BINARY": "CTF", "NEG_RISK": "NEG_RISK", "COMBINATORIAL": "COMBO"}.get(identity["module"])
    return {
        **identity,
        "status": "V2_POSITION_ID_DECODED",
        "module": module,
        "outcome": "YES" if identity["outcome_index"] == 0 else "NO",
    }


def _combo_market_ids(market: dict[str, Any]) -> list[str]:
    """Read SDK-style outcomes or API position_ids without coercing token IDs."""
    ids = market.get("position_ids", market.get("positionIds"))
    if ids is not None:
        parsed = _json_array(ids)
        if parsed is not None:
            return [str(value) for value in parsed]
    outcomes = market.get("outcomes")
    if isinstance(outcomes, list):
        result = []
        for outcome in outcomes:
            if isinstance(outcome, dict):
                value = outcome.get("position_id", outcome.get("positionId"))
                if value is not None:
                    result.append(str(value))
            else:
                result.append("")
        return result
    return []


def audit_combo_catalog(
    position_ids: list[str], *, session: Any = requests, max_pages: int = 100
) -> dict[str, Any]:
    """Scan public active-combo catalog pages and exact-match sampled IDs.

    This endpoint lists currently eligible combo legs, so a miss is always
    inconclusive for historical PositionIds. A page cap also makes catalog
    coverage explicitly incomplete when pagination has not reached its end.
    """
    if max_pages < 1:
        raise ValueError("combo catalog page limit must be positive")
    wanted = set(position_ids)
    matches: dict[str, list[dict[str, Any]]] = {item: [] for item in wanted}
    cursor = None
    pages = 0
    try:
        while pages < max_pages:
            params: dict[str, Any] = {"limit": COMBO_PAGE_SIZE}
            if cursor is not None:
                params["cursor"] = cursor
            response = session.get(
                f"{COMBO_API}/v1/rfq/combo-markets", params=params, timeout=15
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("markets"), list):
                return {
                    "status": "COMBO_CATALOG_INVALID_RESPONSE",
                    "pages_scanned": pages,
                    "catalog_complete": False,
                    "results": [],
                }
            pages += 1
            for market in body["markets"]:
                if not isinstance(market, dict):
                    continue
                ids = _combo_market_ids(market)
                for index, value in enumerate(ids):
                    if value in wanted:
                        outcomes = _json_array(market.get("outcomes"))
                        label = (
                            outcomes[index]
                            if outcomes and index < len(outcomes)
                            else "YES" if index == 0
                            else "NO" if index == 1
                            else None
                        )
                        matches[value].append({
                            "condition_id": market.get("condition_id", market.get("conditionId")),
                            "slug": market.get("slug"),
                            "title": market.get("title"),
                            "position_id_index": index,
                            "outcome": label,
                        })
            cursor = body.get("next_cursor")
            if not cursor:
                break
    except requests.Timeout:
        return {
            "status": "COMBO_CATALOG_TIMEOUT",
            "pages_scanned": pages,
            "catalog_complete": False,
            "results": [],
        }
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        return {
            "status": "COMBO_CATALOG_HTTP_ERROR",
            "http_status": getattr(response, "status_code", None),
            "pages_scanned": pages,
            "catalog_complete": False,
            "results": [],
        }
    except requests.RequestException as exc:
        return {
            "status": "COMBO_CATALOG_REQUEST_ERROR",
            "error_type": type(exc).__name__,
            "pages_scanned": pages,
            "catalog_complete": False,
            "results": [],
        }
    except (ValueError, TypeError):
        return {
            "status": "COMBO_CATALOG_INVALID_JSON",
            "pages_scanned": pages,
            "catalog_complete": False,
            "results": [],
        }
    except Exception as exc:
        return {
            "status": "COMBO_CATALOG_CLIENT_ERROR",
            "error_type": type(exc).__name__,
            "pages_scanned": pages,
            "catalog_complete": False,
            "results": [],
        }

    complete = not cursor
    results = []
    for position_id in position_ids:
        exact = matches.get(position_id, [])
        if len(exact) == 1:
            results.append({
                "position_id": position_id,
                "status": "EXACT_COMBO_POSITION_ID_MATCH",
                **exact[0],
            })
        elif exact:
            results.append({
                "position_id": position_id,
                "status": "COMBO_POSITION_ID_MATCH_NOT_UNIQUE",
                "match_count": len(exact),
            })
        else:
            results.append({"position_id": position_id, "status": "NO_EXACT_ACTIVE_COMBO_MATCH_INCONCLUSIVE"})
    return {
        "status": "COMBO_CATALOG_COMPLETE" if complete else "COMBO_CATALOG_PAGE_LIMIT_REACHED",
        "pages_scanned": pages,
        "catalog_complete": complete,
        "page_limit": max_pages,
        "results": results,
    }


def audit_receipt_event(rpc: JsonRpcReader, expected: dict[str, Any]) -> dict[str, Any]:
    """Match one decoded 1A event to its receipt log and canonical block header."""
    tx_hash = expected.get("transaction_hash")
    try:
        if not isinstance(tx_hash, str) or len(tx_hash) != 66 or not tx_hash.startswith("0x"):
            return {"evidence_id": expected.get("evidence_id"), "status": "INVALID_SAMPLE"}
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_NOT_FOUND"}
        if str(receipt.get("transactionHash", "")).lower() != tx_hash.lower():
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_TX_HASH_MISMATCH"}
        status_value = receipt.get("status")
        if status_value is None:
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_STATUS_UNVERIFIED"}
        if _hex_block(status_value, "receipt status") != 1:
            return {"evidence_id": expected.get("evidence_id"), "status": "TRANSACTION_NOT_SUCCESSFUL"}
        expected_index = expected.get("log_index")
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return {"evidence_id": expected.get("evidence_id"), "status": "INVALID_RECEIPT_LOGS"}
        matching = [
            item for item in logs
            if isinstance(item, dict)
            and _hex_block(item.get("logIndex"), "receipt log index") == expected_index
        ]
        if len(matching) != 1:
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_LOG_MISSING_OR_AMBIGUOUS"}
        observed = decode_order_filled(matching[0])
        if not _same_event(expected, observed):
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_EVENT_MISMATCH"}
        if _hex_block(receipt.get("blockNumber"), "receipt block number") != expected.get("block_number"):
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_BLOCK_NUMBER_MISMATCH"}
        if str(receipt.get("blockHash", "")).lower() != str(expected.get("block_hash", "")).lower():
            return {"evidence_id": expected.get("evidence_id"), "status": "RECEIPT_BLOCK_HASH_MISMATCH"}

        block = rpc.call("eth_getBlockByNumber", [hex(expected["block_number"]), False])
        if not isinstance(block, dict) or str(block.get("hash", "")).lower() != str(expected.get("block_hash", "")).lower():
            return {"evidence_id": expected.get("evidence_id"), "status": "CANONICAL_BLOCK_HASH_MISMATCH"}
        if _hex_block(block.get("number"), "canonical block number") != expected.get("block_number"):
            return {"evidence_id": expected.get("evidence_id"), "status": "CANONICAL_BLOCK_NUMBER_MISMATCH"}
        return {
            "evidence_id": expected.get("evidence_id"),
            "transaction_hash": tx_hash.lower(),
            "log_index": expected_index,
            "block_number": expected.get("block_number"),
            "block_hash": str(expected.get("block_hash", "")).lower(),
            "status": "RECEIPT_AND_CANONICAL_BLOCK_MATCH",
        }
    except ProbeError as exc:
        return {
            "evidence_id": expected.get("evidence_id"),
            "status": "RPC_ERROR",
            "rpc_code": exc.rpc_code,
            "http_status": exc.http_status,
            "timed_out": exc.timed_out,
        }
    except (KeyError, TypeError, ValueError):
        return {"evidence_id": expected.get("evidence_id"), "status": "MALFORMED_EVIDENCE_OR_RECEIPT"}


def audit_gamma_position(position_id: str, session: Any = requests) -> dict[str, Any]:
    """Require one exact Gamma PositionId match; keep CTF token IDs separate."""
    try:
        response = session.get(
            f"{GAMMA_API}/markets",
            params={"position_ids": position_id, "limit": 100},
            timeout=15,
        )
        response.raise_for_status()
        markets = response.json()
    except Exception:
        return {"position_id": position_id, "status": "GAMMA_LOOKUP_ERROR"}
    if not isinstance(markets, list):
        return {"position_id": position_id, "status": "GAMMA_INVALID_RESPONSE"}

    exact: list[tuple[dict[str, Any], int]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        position_ids = _json_array(market.get("positionIds"))
        if position_ids is None:
            continue
        for index, value in enumerate(position_ids):
            if str(value) == position_id:
                exact.append((market, index))
    if not exact:
        return {
            "position_id": position_id,
            "status": "NO_EXACT_POSITION_ID_MATCH_INCONCLUSIVE",
            "clob_token_ids_treated_as_position_ids": False,
        }
    if len(exact) != 1:
        return {"position_id": position_id, "status": "POSITION_ID_MATCH_NOT_UNIQUE", "match_count": len(exact)}

    market, index = exact[0]
    outcomes = _json_array(market.get("outcomes"))
    clob_token_ids = _json_array(market.get("clobTokenIds"))
    outcome = outcomes[index] if outcomes is not None and len(outcomes) == len(_json_array(market.get("positionIds")) or []) else None
    return {
        "position_id": position_id,
        "status": "EXACT_POSITION_ID_MATCH",
        "condition_id": market.get("conditionId"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "position_id_index": index,
        "outcome_at_index": outcome,
        "outcome_alignment_status": "ALIGNED" if outcome is not None else "UNVERIFIED",
        "clob_token_ids": [str(value) for value in clob_token_ids] if clob_token_ids is not None else None,
        "clob_token_ids_treated_as_position_ids": False,
        "clob_protocol_identity": "positionID",
    }


def audit_clob_asset(position_id: str, session: Any = requests) -> dict[str, Any]:
    """Read a public CLOB book and verify its returned asset_id exactly.

    The unified official Python SDK accepts V2 position IDs as ``asset_id``
    for public order-book reads. A book lookup error (including no book) is
    inconclusive and never establishes that the ID is invalid.
    """
    try:
        response = session.get(
            f"{CLOB_API}/book",
            params={"token_id": position_id},
            timeout=15,
        )
        response.raise_for_status()
        book = response.json()
    except requests.Timeout:
        return {"position_id": position_id, "status": "CLOB_TIMEOUT"}
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        return {
            "position_id": position_id,
            "status": "CLOB_HTTP_ERROR",
            "http_status": getattr(response, "status_code", None),
        }
    except requests.RequestException as exc:
        return {
            "position_id": position_id,
            "status": "CLOB_REQUEST_ERROR",
            "error_type": type(exc).__name__,
        }
    except (ValueError, TypeError):
        return {"position_id": position_id, "status": "CLOB_INVALID_JSON"}
    except Exception as exc:
        return {
            "position_id": position_id,
            "status": "CLOB_CLIENT_ERROR",
            "error_type": type(exc).__name__,
        }
    if not isinstance(book, dict):
        return {"position_id": position_id, "status": "CLOB_INVALID_RESPONSE"}
    returned_asset_id = book.get("asset_id")
    if not isinstance(returned_asset_id, str):
        return {"position_id": position_id, "status": "CLOB_ASSET_ID_MISSING"}
    if returned_asset_id != position_id:
        return {
            "position_id": position_id,
            "returned_asset_id": returned_asset_id,
            "status": "CLOB_ASSET_ID_MISMATCH",
        }
    return {
        "position_id": position_id,
        "returned_asset_id": returned_asset_id,
        "market": book.get("market"),
        "status": "EXACT_CLOB_ASSET_ID_MATCH",
    }


def discover_active_v3_candidates(
    rpc: JsonRpcReader,
    *,
    session: Any = requests,
    max_pages: int = 100,
    lookback_blocks: int = 10_000,
) -> dict[str, Any]:
    """Find active Gamma V3 IDs, verify CLOB identity, then corroborate fills.

    Any pagination cap, API miss, or log-range miss is explicitly inconclusive.
    Only the Runtime decoder decides whether a PositionId is eligible.
    """
    candidates: dict[str, dict[str, Any]] = {}
    cursor = None
    seen_cursors = set()
    pages = 0
    complete = True
    gamma_error = None
    retries_total = 0
    try:
        for _ in range(max_pages):
            params = {"limit": 100, "active": True, "closed": False}
            if cursor:
                params["after_cursor"] = cursor
            response, retries = _gamma_get_with_retry(session, f"{GAMMA_API}/markets/keyset", params=params)
            retries_total += retries
            payload = response.json()
            if isinstance(payload, dict):
                markets = payload.get("markets", [])
                next_cursor = payload.get("next_cursor") or payload.get("nextCursor")
            elif isinstance(payload, list):
                markets, next_cursor = payload, None
            else:
                return {"status": "GAMMA_INVALID_RESPONSE_INCONCLUSIVE", "pages_scanned": pages, "candidates": []}
            pages += 1
            if not isinstance(markets, list):
                return {"status": "GAMMA_INVALID_RESPONSE_INCONCLUSIVE", "pages_scanned": pages, "candidates": []}
            for market in markets:
                if not isinstance(market, dict):
                    continue
                if market.get("active") is not True or market.get("closed") is not False:
                    continue
                position_ids = _json_array(market.get("positionIds"))
                outcomes = _json_array(market.get("outcomes"))
                if position_ids is None or outcomes is None or len(position_ids) != len(outcomes):
                    continue
                condition_id = market.get("conditionId", market.get("condition_id"))
                for index, raw_id in enumerate(position_ids):
                    try:
                        identity = decode_position_id(str(raw_id))
                    except (V3IdentityError, TypeError, ValueError):
                        continue
                    if identity["classification"] != "SUPPORTED":
                        continue
                    if not condition_matches_position(condition_id, identity["condition_id"]):
                        continue
                    expected_outcome = "YES" if identity["outcome_index"] == 0 else "NO"
                    if str(outcomes[index]).strip().upper() != expected_outcome:
                        continue
                    candidates.setdefault(identity["position_id"], {
                        "position_id": identity["position_id"],
                        "module": identity["module"],
                        "condition_id": identity["condition_id"],
                        "outcome_index": identity["outcome_index"],
                        "outcome": expected_outcome,
                        "gamma_condition_id": condition_id,
                        "market_id": market.get("id"),
                        "slug": market.get("slug"),
                        "active": market.get("active"),
                        "closed": market.get("closed"),
                        "gamma_identity_status": "EXACT_CONDITION_AND_OUTCOME_MATCH",
                    })
            if not next_cursor:
                cursor = None
                break
            if next_cursor in seen_cursors:
                complete = False
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            if cursor:
                complete = False
    except Exception as exc:
        complete = False
        response = getattr(exc, "response", None)
        failed_page_retries = getattr(exc, "gamma_retries", 0)
        retries_total += failed_page_retries
        gamma_error = {
            "phase": "gamma_page_fetch",
            "failed_page": pages + 1,
            "error_type": type(exc).__name__,
            "http_status": getattr(response, "status_code", None),
            "retries_on_failed_page": failed_page_retries,
        }

    clob_verified = []
    clob_checks = []
    for candidate in candidates.values():
        clob = audit_clob_asset(candidate["position_id"], session=session)
        clob_checks.append({**candidate, "clob_status": clob.get("status")})
        if clob.get("status") == "EXACT_CLOB_ASSET_ID_MATCH":
            clob_verified.append(clob_checks[-1])

    try:
        finalized = rpc.call("eth_getBlockByNumber", ["finalized", False])
        end_block = _hex_block(finalized.get("number"), "finalized block") if isinstance(finalized, dict) else None
        if end_block is None:
            raise ProbeError("no finalized block")
        start_block = max(0, end_block - lookback_blocks + 1)
        logs = _scan_logs(rpc, EXCHANGE_V3_ADDRESS, start_block, end_block, 500)
    except Exception as exc:
        return {
            "status": "ONCHAIN_SCAN_ERROR_INCONCLUSIVE",
            "gamma_pages_scanned": pages,
            "gamma_scan_complete": complete,
            "gamma_candidates": len(candidates),
            "gamma_error": gamma_error,
            "gamma_retries": retries_total,
            "clob_candidate_checks": clob_checks,
            "clob_verified_candidates": clob_verified,
            "error_type": type(exc).__name__,
            "event_matches": [],
            "interpretation": "No candidate was disproved; the finalized event scan was unavailable.",
        }

    wanted = {item["position_id"]: item for item in clob_verified}
    event_matches = []
    for log in logs:
        try:
            event = decode_order_filled(log)
        except (TypeError, ValueError):
            continue
        candidate = wanted.get(event["position_id"])
        if candidate is None:
            continue
        provenance = audit_receipt_event(rpc, event)
        event_matches.append({
            **candidate,
            "transaction_hash": event["transaction_hash"],
            "log_index": event["log_index"],
            "block_number": event["block_number"],
            "block_hash": event["block_hash"],
            "receipt_status": provenance["status"],
        })
    confirmed = [item for item in event_matches if item["receipt_status"] == "RECEIPT_AND_CANONICAL_BLOCK_MATCH"]
    return {
        "status": (
            "CONFIRMED_CANDIDATE_FOUND" if confirmed else
            "GAMMA_LOOKUP_ERROR_INCONCLUSIVE" if gamma_error else
            "NO_CANDIDATE_PROVEN_INCONCLUSIVE"
        ),
        "read_only": True,
        "gamma_pages_scanned": pages,
        "gamma_scan_complete": complete,
        "gamma_candidates": len(candidates),
        "gamma_error": gamma_error,
        "gamma_retries": retries_total,
        "clob_candidate_checks": clob_checks,
        "clob_verified_candidates": clob_verified,
        "finalized_block": end_block,
        "event_scan_range": {"start_block": start_block, "end_block": end_block},
        "event_matches": event_matches,
        "confirmed_candidates": confirmed,
        "interpretation": "A missing Gamma or OrderFilled match is not evidence that no eligible market exists.",
    }


def run_audit(
    source_report: dict[str, Any],
    rpc_url: str,
    *,
    receipt_sample: int = 10,
    gamma_sample: int = 25,
    combo_max_pages: int = 100,
    session: Any = requests,
    rpc: JsonRpcReader | None = None,
) -> dict[str, Any]:
    if receipt_sample < 0 or gamma_sample < 0 or combo_max_pages < 1:
        raise ValueError("receipt/gamma sample limits must be non-negative and combo_max_pages must be positive")
    if not isinstance(source_report, dict) or source_report.get("probe") != "POLYMARKET_V2_EXCHANGE_V3_COVERAGE":
        raise ValueError("input must be a V2 coverage probe JSON report")
    rpc = rpc or JsonRpcReader(rpc_url)
    chain_id = _hex_block(rpc.call("eth_chainId", []), "chain id")
    if chain_id != POLYGON_CHAIN_ID:
        raise ProbeError(f"Wrong chain: expected Polygon ({POLYGON_CHAIN_ID}), received {chain_id}")

    reconciliation = source_report.get("reconciliation")
    activity = source_report.get("activity")
    if not isinstance(reconciliation, dict) or not isinstance(activity, dict):
        raise ValueError("input report has no reconciliation/activity evidence")
    events = reconciliation.get("unique_event_samples", [])
    positions = activity.get("unique_position_id_samples", [])
    if not isinstance(events, list) or not isinstance(positions, list):
        raise ValueError("input report samples are malformed")
    receipt_results = [
        audit_receipt_event(rpc, item) if isinstance(item, dict)
        else {"status": "INVALID_SAMPLE"}
        for item in events[:receipt_sample]
    ]
    position_ids = []
    for item in positions[:gamma_sample]:
        if isinstance(item, dict) and isinstance(item.get("position_id"), str):
            position_ids.append(item["position_id"])
        else:
            position_ids.append(None)
    gamma_results = [
        audit_gamma_position(item, session=session) if isinstance(item, str)
        else {"position_id": None, "status": "INVALID_POSITION_ID_SAMPLE"}
        for item in position_ids
    ]
    clob_results = [
        audit_clob_asset(item, session=session) if isinstance(item, str)
        else {"position_id": None, "status": "INVALID_POSITION_ID_SAMPLE"}
        for item in position_ids
    ]
    decoded_results = [
        decode_v2_position_id(item) if isinstance(item, str)
        else {"position_id": None, "status": "INVALID_POSITION_ID_SAMPLE"}
        for item in position_ids
    ]
    combo_position_ids = [
        item for item, decoded in zip(position_ids, decoded_results)
        if isinstance(item, str) and decoded.get("module_id") == 3
    ]
    combo_catalog = audit_combo_catalog(
        combo_position_ids, session=session, max_pages=combo_max_pages
    ) if combo_position_ids else {
        "status": "NO_COMBO_POSITION_IDS_SAMPLED",
        "pages_scanned": 0,
        "catalog_complete": True,
        "results": [],
    }
    receipt_counts: dict[str, int] = {}
    gamma_counts: dict[str, int] = {}
    clob_counts: dict[str, int] = {}
    for result in receipt_results:
        status = str(result["status"])
        receipt_counts[status] = receipt_counts.get(status, 0) + 1
    for result in gamma_results:
        status = str(result["status"])
        gamma_counts[status] = gamma_counts.get(status, 0) + 1
    for result in clob_results:
        status = str(result["status"])
        clob_counts[status] = clob_counts.get(status, 0) + 1

    return {
        "audit": "POLYMARKET_V2_COVERAGE_1B_RECEIPT_AND_IDENTITY",
        "read_only": True,
        "runtime_authority_changed": False,
        "source_probe": source_report.get("probe"),
        "source_range": source_report.get("range"),
        "chain_id": chain_id,
        "receipt_provenance": {
            "sampled_events": len(receipt_results),
            "result_counts": dict(sorted(receipt_counts.items())),
            "results": receipt_results,
            "provider_independence": "NOT_ESTABLISHED_SINGLE_CONFIGURED_RPC",
            "completeness_claim_allowed": False,
        },
        "position_identity": {
            "sampled_position_ids": len(gamma_results),
            "result_counts": dict(sorted(gamma_counts.items())),
            "results": gamma_results,
            "decoded_v2_position_ids": decoded_results,
            "identity_rule": "Exact position ID matches are required. Module-3 combo IDs are checked against the public active-combo catalog; Gamma misses and CLOB book misses are inconclusive.",
            "combo_catalog": {
                **combo_catalog,
                "identity_rule": "Only exact position_ids/outcome.position_id matches confirm current active-combo catalog membership. Historical absence and capped pagination are inconclusive.",
            },
            "clob_order_identifier": "positionID",
            "clob_public_read": {
                "sampled_position_ids": len(clob_results),
                "result_counts": dict(sorted(clob_counts.items())),
                "results": clob_results,
                "identity_rule": "Only an exact CLOB book response asset_id match confirms that the public CLOB lookup recognized that ID; lookup errors are inconclusive.",
            },
        },
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Receipt checks corroborate sampled logs only; they do not prove eth_getLogs completeness.",
            "All RPC evidence came from one configured provider; independent-provider agreement is not established.",
            "Gamma misses are inconclusive and do not prove that a PositionId is absent.",
            "CLOB book lookup errors are inconclusive and do not prove that a PositionId is absent.",
            "CLOB token IDs are reported separately; no orders, market actions, or production integrations are performed.",
            "The public combo catalog contains currently eligible legs and may not include historical or inactive combo positions.",
            "Combo catalog pagination is capped by combo_max_pages; a capped scan does not prove catalog absence.",
            "The audit does not update Collector, Flow, Paper, or other runtime state.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only receipt provenance and V2 identity audit")
    parser.add_argument("--report", help="saved JSON report emitted by v2_coverage_probe.py")
    parser.add_argument("--discover-active", action="store_true", help="search active Gamma markets for exact V3 candidates, then verify CLOB and finalized fills")
    parser.add_argument("--lookback-blocks", type=int, default=10_000)
    parser.add_argument("--gamma-max-pages", type=int, default=100)
    parser.add_argument("--receipt-sample", type=int, default=10)
    parser.add_argument("--gamma-sample", type=int, default=25)
    parser.add_argument(
        "--combo-max-pages",
        type=int,
        default=100,
        help="maximum public combo catalog pages to inspect (100 entries per page)",
    )
    parser.add_argument("--env-file", help="optional .env file; only POLYMARKET_RPC_URL is read")
    args = parser.parse_args(argv)
    try:
        rpc_url = _rpc_url_from_config(args.env_file)
        if not rpc_url:
            print("Missing POLYMARKET_RPC_URL configuration; RPC endpoint was not contacted.", file=sys.stderr)
            return 2
        if args.discover_active:
            if args.lookback_blocks < 1 or args.gamma_max_pages < 1:
                raise ValueError("lookback-blocks and gamma-max-pages must be positive")
            report = discover_active_v3_candidates(
                JsonRpcReader(rpc_url), max_pages=args.gamma_max_pages,
                lookback_blocks=args.lookback_blocks,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if not args.report:
            parser.error("--report is required unless --discover-active is used")
        with open(args.report, "r", encoding="utf-8") as handle:
            source_report = json.load(handle)
        report = run_audit(
            source_report,
            rpc_url,
            receipt_sample=args.receipt_sample,
            gamma_sample=args.gamma_sample,
            combo_max_pages=args.combo_max_pages,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ProbeError, ValueError) as exc:
        print(f"V2 coverage 1B audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
