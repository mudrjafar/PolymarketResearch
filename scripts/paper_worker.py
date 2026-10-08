"""Resident user-controlled Paper worker.

Consumes explicit Paper OPEN/CLOSE request files. OPEN is permitted only while
the authoritative Focus snapshot is READY and the authoritative Book snapshot
is PASS with exact generation/evidence/market binding. The worker independently
fetches the current public CLOB book at execution time and never signs or
submits a real order.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from machine_common import fresh, save_json_atomic
from scripts import book_engine, book_worker, paper_engine, settlement_resolver

DATA_DIR = BASE_DIR / "data"
FOCUS_FILE = DATA_DIR / "focused_market.json"
BOOK_FILE = DATA_DIR / "book_assessment.json"
REQUEST_DIR = DATA_DIR / "paper_requests"
STATE_FILE = DATA_DIR / "paper_state.json"
EVENTS_FILE = DATA_DIR / "paper_events.jsonl"

CLOB_BASE_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/")
SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 5
ALLOWED_OPEN_AMOUNTS = (25.0, 50.0, 100.0)
MAX_PROCESSED_REQUEST_IDS = 1000


class PaperStateError(RuntimeError):
    pass


class PaperFetchError(RuntimeError):
    pass


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def new_state(now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now.isoformat(),
        "positions": [],
        "processed_request_ids": [],
    }


def load_state(now=None):
    if not STATE_FILE.exists():
        return new_state(now)

    try:
        payload = _read_json(STATE_FILE)
    except Exception as exc:
        raise PaperStateError(f"paper state unreadable: {type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise PaperStateError("paper state must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PaperStateError("paper state schema mismatch")
    if not isinstance(payload.get("positions"), list):
        raise PaperStateError("paper positions missing")
    if not isinstance(payload.get("processed_request_ids"), list):
        raise PaperStateError("paper processed ids missing")

    return payload


def _snapshot(path, now):
    if not Path(path).exists():
        return "MISSING", None

    try:
        payload = _read_json(path)
    except Exception:
        return "CORRUPT", None

    if not isinstance(payload, dict):
        return "CORRUPT", None
    if payload.get("schema_version") != 1:
        return "CORRUPT", payload
    if not payload.get("generated_at"):
        return "CORRUPT", payload
    if not fresh(payload.get("generated_at"), now=now):
        return "STALE", payload

    return "OK", payload


def read_execution_gate(now=None):
    now = now or datetime.now(timezone.utc)
    focus_status, focus_payload = _snapshot(FOCUS_FILE, now)
    book_status, book_payload = _snapshot(BOOK_FILE, now)

    if focus_status != "OK" or book_status != "OK":
        return "INPUT_NOT_OK", focus_payload, book_payload

    if focus_payload.get("input_status") != "OK":
        return "FOCUS_UPSTREAM_NOT_OK", focus_payload, book_payload
    if focus_payload.get("state") != "READY":
        return "FOCUS_NOT_READY", focus_payload, book_payload

    focus = focus_payload.get("focus")
    if not isinstance(focus, dict) or focus.get("status") != "READY":
        return "FOCUS_NOT_READY", focus_payload, book_payload

    if book_payload.get("status") != "OK":
        return "BOOK_STATUS_NOT_OK", focus_payload, book_payload
    if book_payload.get("book_ok") is not True:
        return "BOOK_BLOCK", focus_payload, book_payload

    token_id = str(focus.get("token_id") or "").strip()
    condition_id = str(focus.get("condition_id") or "").strip()
    evidence_id = str(focus.get("last_evidence_id") or "").strip()
    generation_id = str(focus_payload.get("source_generation_id") or "").strip()

    if not all((token_id, condition_id, evidence_id, generation_id)):
        return "FOCUS_BINDING_INCOMPLETE", focus_payload, book_payload

    if str(book_payload.get("token_id") or "").strip() != token_id:
        return "TOKEN_ID_MISMATCH", focus_payload, book_payload
    if str(book_payload.get("condition_id") or "").strip() != condition_id:
        return "CONDITION_ID_MISMATCH", focus_payload, book_payload
    if str(book_payload.get("source_evidence_id") or "").strip() != evidence_id:
        return "EVIDENCE_ID_MISMATCH", focus_payload, book_payload
    if str(book_payload.get("source_generation_id") or "").strip() != generation_id:
        return "GENERATION_ID_MISMATCH", focus_payload, book_payload

    return "READY_BOOK_PASS", focus_payload, book_payload


def fetch_market_info(condition_id, timeout=DEFAULT_TIMEOUT_SECONDS):
    try:
        response = requests.get(
            f"{CLOB_BASE_URL}/clob-markets/{condition_id}",
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PaperFetchError(type(exc).__name__) from exc

    if response.status_code != 200:
        raise PaperFetchError(f"HTTP_{response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PaperFetchError("INVALID_JSON") from exc

    if not isinstance(payload, dict):
        raise PaperFetchError("INVALID_PAYLOAD")

    return payload


def _append_events(events):
    if not events:
        return

    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _request_event(request_id, event_type, now, **extra):
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_at": now.isoformat(),
        "request_id": request_id,
        "type": event_type,
    }
    event.update(extra)
    return event


def _load_requests():
    if not REQUEST_DIR.exists():
        return []

    rows = []
    for path in sorted(REQUEST_DIR.glob("*.json")):
        request_id = path.stem
        try:
            payload = _read_json(path)
        except Exception:
            payload = None
        rows.append((request_id, payload))
    return rows


def _request_is_fresh(payload, now):
    return isinstance(payload, dict) and fresh(payload.get("requested_at"), now=now)


def _matches_open_request(request, focus_payload):
    focus = focus_payload.get("focus") or {}

    expected = {
        "token_id": focus.get("token_id"),
        "condition_id": focus.get("condition_id"),
        "source_generation_id": focus_payload.get("source_generation_id"),
        "source_evidence_id": focus.get("last_evidence_id"),
        "focus_locked_at": focus.get("locked_at"),
    }

    for field, value in expected.items():
        if str(request.get(field) or "").strip() != str(value or "").strip():
            return False, f"REQUEST_{field.upper()}_MISMATCH"

    return True, "OK"


def _position_mark(position, raw_book, now):
    ok, reason = paper_engine.validate_book_identity(
        raw_book,
        position.get("token_id"),
        position.get("condition_id"),
    )
    if not ok:
        position["mark_status"] = reason
        position["mark_checked_at"] = now.isoformat()
        return

    try:
        quote = paper_engine.simulate_sell(
            raw_book,
            position.get("tokens"),
            fee_rate=position.get("fee_rate", 0.0),
            fee_exponent=position.get("fee_exponent", 0.0),
        )
    except Exception as exc:
        position["mark_status"] = str(exc) or type(exc).__name__
        position["mark_checked_at"] = now.isoformat()
        return

    if not quote.get("complete"):
        position["mark_status"] = "INSUFFICIENT_EXIT_DEPTH"
        position["mark_checked_at"] = now.isoformat()
        return

    value = float(quote["net_proceeds_usd"])
    investment = float(position["investment_usd"])
    pnl = value - investment
    return_pct = pnl / investment * 100.0 if investment > 0 else 0.0

    position["mark_status"] = "OK"
    position["mark_checked_at"] = now.isoformat()
    position["mark"] = {
        "generated_at": now.isoformat(),
        "book_hash": raw_book.get("hash"),
        "book_timestamp": raw_book.get("timestamp"),
        "best_bid": quote.get("best_bid"),
        "exit_vwap": quote.get("vwap"),
        "effective_exit_price": quote.get("effective_exit_price"),
        "exit_fee_usd": quote.get("fee_usd"),
        "exit_slippage_bps": quote.get("slippage_bps"),
        "current_value_usd": round(value, 8),
        "pnl_usd": round(pnl, 8),
        "return_pct": round(return_pct, 4),
    }



def _settlement_event(position, now):
    settlement = position.get("settlement") or {}
    return _request_event(
        position.get("request_id"),
        "SETTLED",
        now,
        paper_id=position.get("paper_id"),
        token_id=position.get("token_id"),
        condition_id=position.get("condition_id"),
        payout_per_token=settlement.get("payout_per_token"),
        settlement_value_usd=settlement.get("settlement_value_usd"),
        realized_pnl_usd=position.get("realized_pnl_usd"),
        realized_return_pct=position.get("realized_return_pct"),
        finalized_block_number=settlement.get("finalized_block_number"),
        finalized_block_hash=settlement.get("finalized_block_hash"),
    )


def _check_position_settlement(position, now, binding_loader, settlement_checker):
    if not isinstance(position.get("settlement_binding"), dict):
        try:
            binding = binding_loader(
                position.get("token_id"),
                position.get("condition_id"),
            )
            if not isinstance(binding, dict):
                raise ValueError("SETTLEMENT_BINDING_INVALID")
            position["settlement_binding"] = binding
        except Exception as exc:
            position["settlement_status"] = "SETTLEMENT_CHECK_ERROR"
            position["settlement_checked_at"] = now.isoformat()
            position["settlement_error"] = str(exc).strip() or type(exc).__name__
            return None

    try:
        resolution = settlement_checker(position)
    except Exception as exc:
        position["settlement_status"] = "SETTLEMENT_CHECK_ERROR"
        position["settlement_checked_at"] = now.isoformat()
        position["settlement_error"] = str(exc).strip() or type(exc).__name__
        return None

    if not isinstance(resolution, dict):
        position["settlement_status"] = "SETTLEMENT_CHECK_ERROR"
        position["settlement_checked_at"] = now.isoformat()
        position["settlement_error"] = "SETTLEMENT_RESULT_INVALID"
        return None

    status = resolution.get("status")
    position["settlement_checked_at"] = now.isoformat()
    position["settlement_error"] = None

    if status == "UNRESOLVED":
        position["settlement_status"] = "UNRESOLVED"
        return None
    if status != "RESOLVED":
        position["settlement_status"] = "SETTLEMENT_CHECK_ERROR"
        position["settlement_error"] = "SETTLEMENT_STATUS_INVALID"
        return None

    try:
        quote = paper_engine.settlement_quote(
            position.get("tokens"),
            resolution.get("payout_numerator"),
            resolution.get("payout_denominator"),
            position.get("investment_usd"),
        )
    except Exception as exc:
        position["settlement_status"] = "SETTLEMENT_CHECK_ERROR"
        position["settlement_error"] = str(exc).strip() or type(exc).__name__
        return None

    position["status"] = "SETTLED"
    position["settled_at"] = now.isoformat()
    position["settlement_status"] = "SETTLED"
    position["settlement"] = {
        "source": resolution.get("source"),
        "chain_id": resolution.get("chain_id"),
        "ctf_contract": resolution.get("ctf_contract"),
        "market_family": resolution.get("market_family"),
        "exchange_contract": resolution.get("exchange_contract"),
        "ctf_collateral": resolution.get("ctf_collateral"),
        "outcome_index": resolution.get("outcome_index"),
        "index_set": resolution.get("index_set"),
        "payout_numerators": resolution.get("payout_numerators"),
        "payout_numerator": quote["payout_numerator"],
        "payout_denominator": quote["payout_denominator"],
        "payout_per_token": quote["payout_per_token"],
        "settlement_value_usd": quote["settlement_value_usd"],
        "finalized_block_number": resolution.get("finalized_block_number"),
        "finalized_block_hash": resolution.get("finalized_block_hash"),
    }
    position["realized_pnl_usd"] = quote["realized_pnl_usd"]
    position["realized_return_pct"] = quote["realized_return_pct"]
    position["mark_status"] = "SETTLED"
    position["mark_checked_at"] = now.isoformat()

    return _settlement_event(position, now)


def _reject(request_id, now, reason):
    return _request_event(
        request_id,
        "REJECTED",
        now,
        reason_code=reason,
    )


def _process_open(
    state,
    request_id,
    request,
    now,
    book_loader,
    market_info_loader,
    settlement_binding_loader,
):
    amount = request.get("amount_usd")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return _reject(request_id, now, "AMOUNT_INVALID")

    if amount not in ALLOWED_OPEN_AMOUNTS:
        return _reject(request_id, now, "AMOUNT_NOT_ALLOWED")

    gate, focus_payload, _ = read_execution_gate(now=now)
    if gate != "READY_BOOK_PASS":
        return _reject(request_id, now, gate)

    matches, reason = _matches_open_request(request, focus_payload)
    if not matches:
        return _reject(request_id, now, reason)

    focus = focus_payload["focus"]
    token_id = str(focus["token_id"]).strip()
    condition_id = str(focus["condition_id"]).strip()

    for position in state["positions"]:
        if (
            position.get("status") == "OPEN"
            and str(position.get("token_id")) == token_id
        ):
            return _reject(request_id, now, "POSITION_ALREADY_OPEN")

    try:
        raw_book = book_loader(token_id)
    except Exception as exc:
        return _reject(
            request_id,
            now,
            f"BOOK_FETCH_{str(exc).strip() or type(exc).__name__}",
        )

    live_analysis = book_engine.analyze_book(
        raw_book,
        focus,
        target_notionals=(amount,),
    )
    if live_analysis.get("book_ok") is not True:
        reasons = live_analysis.get("reason_codes") or ["BOOK_EXECUTION_BLOCK"]
        return _reject(request_id, now, "BOOK_" + "_".join(reasons))

    try:
        market_info = market_info_loader(condition_id)
        fee_info = paper_engine.parse_fee_info(market_info, token_id)
        settlement_binding = settlement_binding_loader(token_id, condition_id)
        if not isinstance(settlement_binding, dict):
            raise ValueError("SETTLEMENT_BINDING_INVALID")
        entry = paper_engine.simulate_buy(
            raw_book,
            amount,
            fee_rate=fee_info["fee_rate"],
            fee_exponent=fee_info["fee_exponent"],
        )
    except Exception as exc:
        return _reject(
            request_id,
            now,
            str(exc).strip() or type(exc).__name__,
        )

    if entry.get("complete") is not True or float(entry.get("net_tokens") or 0) <= 0:
        return _reject(request_id, now, "ENTRY_NOT_FULLY_EXECUTABLE")

    suffix = request_id.split("-", 1)[-1]
    paper_id = f"PAPER-{suffix}"

    position = {
        "paper_id": paper_id,
        "request_id": request_id,
        "status": "OPEN",
        "opened_at": now.isoformat(),
        "chat_id": str(request.get("chat_id") or ""),
        "token_id": token_id,
        "condition_id": condition_id,
        "outcome": focus.get("outcome"),
        "question": focus.get("question"),
        "direction": "BUY",
        "source_generation_id": focus_payload.get("source_generation_id"),
        "source_evidence_id": focus.get("last_evidence_id"),
        "focus_locked_at": focus.get("locked_at"),
        "investment_usd": float(amount),
        "tokens": float(entry["net_tokens"]),
        "fee_rate": fee_info["fee_rate"],
        "fee_exponent": fee_info["fee_exponent"],
        "settlement_binding": settlement_binding,
        "settlement_status": "PENDING",
        "settlement_checked_at": None,
        "settlement_error": None,
        "settlement": None,
        "entry": {
            "book_hash": raw_book.get("hash"),
            "book_timestamp": raw_book.get("timestamp"),
            "best_ask": entry.get("best_ask"),
            "vwap": entry.get("vwap"),
            "effective_entry_price": entry.get("effective_entry_price"),
            "gross_tokens": entry.get("gross_tokens"),
            "fee_usd": entry.get("fee_usd"),
            "net_tokens": entry.get("net_tokens"),
            "worst_price": entry.get("worst_price"),
            "slippage_bps": entry.get("slippage_bps"),
        },
        "mark_status": "PENDING",
        "mark_checked_at": None,
        "mark": None,
    }

    state["positions"].append(position)

    return _request_event(
        request_id,
        "OPENED",
        now,
        paper_id=paper_id,
        token_id=token_id,
        condition_id=condition_id,
        investment_usd=float(amount),
        entry_vwap=entry.get("vwap"),
        effective_entry_price=entry.get("effective_entry_price"),
        tokens=entry.get("net_tokens"),
        entry_fee_usd=entry.get("fee_usd"),
    )


def _process_close(state, request_id, request, now, book_loader):
    paper_id = str(request.get("paper_id") or "").strip()
    if not paper_id:
        return _reject(request_id, now, "PAPER_ID_MISSING")

    position = next(
        (
            row
            for row in state["positions"]
            if row.get("paper_id") == paper_id and row.get("status") == "OPEN"
        ),
        None,
    )
    if position is None:
        return _reject(request_id, now, "OPEN_POSITION_NOT_FOUND")

    token_id = str(position.get("token_id") or "").strip()

    try:
        raw_book = book_loader(token_id)
    except Exception as exc:
        return _reject(
            request_id,
            now,
            f"BOOK_FETCH_{str(exc).strip() or type(exc).__name__}",
        )

    ok, reason = paper_engine.validate_book_identity(
        raw_book,
        token_id,
        position.get("condition_id"),
    )
    if not ok:
        return _reject(request_id, now, reason)

    try:
        exit_quote = paper_engine.simulate_sell(
            raw_book,
            position.get("tokens"),
            fee_rate=position.get("fee_rate", 0.0),
            fee_exponent=position.get("fee_exponent", 0.0),
        )
    except Exception as exc:
        return _reject(
            request_id,
            now,
            str(exc).strip() or type(exc).__name__,
        )

    if exit_quote.get("complete") is not True:
        return _reject(request_id, now, "EXIT_NOT_FULLY_EXECUTABLE")

    value = float(exit_quote["net_proceeds_usd"])
    investment = float(position["investment_usd"])
    pnl = value - investment
    return_pct = pnl / investment * 100.0 if investment > 0 else 0.0

    position["status"] = "CLOSED"
    position["closed_at"] = now.isoformat()
    position["close_request_id"] = request_id
    position["exit"] = {
        "book_hash": raw_book.get("hash"),
        "book_timestamp": raw_book.get("timestamp"),
        "best_bid": exit_quote.get("best_bid"),
        "vwap": exit_quote.get("vwap"),
        "effective_exit_price": exit_quote.get("effective_exit_price"),
        "fee_usd": exit_quote.get("fee_usd"),
        "gross_proceeds_usd": exit_quote.get("gross_proceeds_usd"),
        "net_proceeds_usd": exit_quote.get("net_proceeds_usd"),
        "worst_price": exit_quote.get("worst_price"),
        "slippage_bps": exit_quote.get("slippage_bps"),
    }
    position["realized_pnl_usd"] = round(pnl, 8)
    position["realized_return_pct"] = round(return_pct, 4)

    return _request_event(
        request_id,
        "CLOSED",
        now,
        paper_id=paper_id,
        token_id=token_id,
        exit_vwap=exit_quote.get("vwap"),
        effective_exit_price=exit_quote.get("effective_exit_price"),
        exit_fee_usd=exit_quote.get("fee_usd"),
        realized_pnl_usd=round(pnl, 8),
        realized_return_pct=round(return_pct, 4),
    )


def run_once(
    now=None,
    book_loader=book_worker.fetch_book,
    market_info_loader=fetch_market_info,
    settlement_binding_loader=None,
    settlement_checker=None,
):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    settlement_binding_loader = (
        settlement_binding_loader or settlement_resolver.bind_position
    )
    settlement_checker = settlement_checker or settlement_resolver.check_position

    state = load_state(now=now)
    processed = set(str(x) for x in state.get("processed_request_ids", []))
    events = []

    for request_id, request in _load_requests():
        if request_id in processed:
            continue

        if not isinstance(request, dict):
            event = _reject(request_id, now, "REQUEST_CORRUPT")
        elif str(request.get("request_id") or "") != request_id:
            event = _reject(request_id, now, "REQUEST_ID_MISMATCH")
        elif request.get("schema_version") != SCHEMA_VERSION:
            event = _reject(request_id, now, "REQUEST_SCHEMA_MISMATCH")
        elif not _request_is_fresh(request, now):
            event = _reject(request_id, now, "REQUEST_STALE")
        elif request.get("action") == "OPEN":
            event = _process_open(
                state,
                request_id,
                request,
                now,
                book_loader,
                market_info_loader,
                settlement_binding_loader,
            )
        elif request.get("action") == "CLOSE":
            event = _process_close(
                state,
                request_id,
                request,
                now,
                book_loader,
            )
        else:
            event = _reject(request_id, now, "ACTION_INVALID")

        processed.add(request_id)
        events.append(event)

    for position in state["positions"]:
        if position.get("status") != "OPEN":
            continue

        settlement_event = _check_position_settlement(
            position,
            now,
            settlement_binding_loader,
            settlement_checker,
        )
        if settlement_event is not None:
            events.append(settlement_event)
            continue

        try:
            raw_book = book_loader(str(position.get("token_id") or ""))
            _position_mark(position, raw_book, now)
        except Exception as exc:
            position["mark_status"] = (
                f"BOOK_FETCH_{str(exc).strip() or type(exc).__name__}"
            )
            position["mark_checked_at"] = now.isoformat()

    state["updated_at"] = now.isoformat()
    state["processed_request_ids"] = list(processed)[-MAX_PROCESSED_REQUEST_IDS:]

    save_json_atomic(STATE_FILE, state)
    _append_events(events)

    return state, events


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    last_signature = None

    while True:
        try:
            state, events = run_once()
            opens = [p for p in state["positions"] if p.get("status") == "OPEN"]
            signature = (
                len(opens),
                tuple((p.get("paper_id"), p.get("mark_status")) for p in opens),
            )
            if signature != last_signature:
                print(f"[PAPER] open_positions={len(opens)}")
                for position in opens:
                    mark = position.get("mark") or {}
                    print(
                        f"[PAPER] {position.get('paper_id')} "
                        f"mark={position.get('mark_status')} "
                        f"pnl={mark.get('pnl_usd', '-')}"
                    )
                last_signature = signature

            for event in events:
                print(
                    f"[PAPER] {event.get('type')} "
                    f"{event.get('paper_id') or event.get('request_id')}"
                )
        except PaperStateError as exc:
            print(f"[PAPER] STATE_ERROR: {exc}")
        except Exception as exc:
            print(f"[PAPER] ERROR: {type(exc).__name__}: {exc}")

        time.sleep(max(1, int(interval)))


def self_test():
    global DATA_DIR, FOCUS_FILE, BOOK_FILE, REQUEST_DIR, STATE_FILE, EVENTS_FILE

    original = (
        DATA_DIR,
        FOCUS_FILE,
        BOOK_FILE,
        REQUEST_DIR,
        STATE_FILE,
        EVENTS_FILE,
    )
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    try:
        with tempfile.TemporaryDirectory() as temp:
            DATA_DIR = Path(temp)
            FOCUS_FILE = DATA_DIR / "focused_market.json"
            BOOK_FILE = DATA_DIR / "book_assessment.json"
            REQUEST_DIR = DATA_DIR / "paper_requests"
            STATE_FILE = DATA_DIR / "paper_state.json"
            EVENTS_FILE = DATA_DIR / "paper_events.jsonl"
            REQUEST_DIR.mkdir(parents=True)

            evidence_id = "0x" + "ab" * 32 + ":1"
            focus = {
                "token_id": "token-a",
                "condition_id": "condition-a",
                "outcome": "Yes",
                "question": "Market A",
                "direction": "BUY",
                "price": 0.50,
                "status": "READY",
                "progress": 3,
                "last_evidence_id": evidence_id,
                "locked_at": now.isoformat(),
            }
            save_json_atomic(
                FOCUS_FILE,
                {
                    "schema_version": 1,
                    "generated_at": now.isoformat(),
                    "input_status": "OK",
                    "source_generation_id": "GEN-X",
                    "state": "READY",
                    "focus": focus,
                },
            )
            save_json_atomic(
                BOOK_FILE,
                {
                    "schema_version": 1,
                    "generated_at": now.isoformat(),
                    "status": "OK",
                    "book_ok": True,
                    "source_generation_id": "GEN-X",
                    "source_evidence_id": evidence_id,
                    "token_id": "token-a",
                    "condition_id": "condition-a",
                },
            )

            open_id = "REQ-open"
            save_json_atomic(
                REQUEST_DIR / f"{open_id}.json",
                {
                    "schema_version": 1,
                    "request_id": open_id,
                    "requested_at": now.isoformat(),
                    "action": "OPEN",
                    "amount_usd": 25,
                    "chat_id": "123",
                    "token_id": "token-a",
                    "condition_id": "condition-a",
                    "source_generation_id": "GEN-X",
                    "source_evidence_id": evidence_id,
                    "focus_locked_at": now.isoformat(),
                },
            )

            def fake_book(token_id):
                assert token_id == "token-a"
                return {
                    "market": "condition-a",
                    "asset_id": "token-a",
                    "timestamp": "1",
                    "hash": "book-x",
                    "bids": [{"price": "0.49", "size": "1000"}],
                    "asks": [{"price": "0.50", "size": "1000"}],
                }

            def fake_market(condition_id):
                assert condition_id == "condition-a"
                return {
                    "t": [{"t": "token-a"}],
                    "fd": {"r": 0.05, "e": 1},
                }

            def fake_binding(token_id, condition_id):
                assert token_id == "token-a"
                assert condition_id == "condition-a"
                return {
                    "source": "POLYGON_FINALIZED_CTF",
                    "chain_id": 137,
                    "ctf_contract": "0xctf",
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "market_family": "STANDARD_CTF_V2",
                    "exchange_contract": "0xexchange",
                    "ctf_collateral": "0xcollateral",
                    "outcome_index": 0,
                    "index_set": 1,
                    "verified_block_number": 100,
                    "verified_block_hash": "0x" + "11" * 32,
                }

            def fake_unresolved(position):
                return {
                    **position["settlement_binding"],
                    "status": "UNRESOLVED",
                    "finalized_block_number": 101,
                    "finalized_block_hash": "0x" + "22" * 32,
                    "payout_denominator": 0,
                    "payout_numerators": None,
                    "payout_numerator": None,
                    "payout_per_token": None,
                }

            state, events = run_once(
                now=now,
                book_loader=fake_book,
                market_info_loader=fake_market,
                settlement_binding_loader=fake_binding,
                settlement_checker=fake_unresolved,
            )
            assert events[0]["type"] == "OPENED"
            assert len(state["positions"]) == 1
            position = state["positions"][0]
            assert position["status"] == "OPEN"
            assert position["mark_status"] == "OK"
            assert position["entry"]["fee_usd"] > 0

            close_id = "REQ-close"
            save_json_atomic(
                REQUEST_DIR / f"{close_id}.json",
                {
                    "schema_version": 1,
                    "request_id": close_id,
                    "requested_at": now.isoformat(),
                    "action": "CLOSE",
                    "paper_id": position["paper_id"],
                    "chat_id": "123",
                },
            )

            state, events = run_once(
                now=now,
                book_loader=fake_book,
                market_info_loader=fake_market,
                settlement_binding_loader=fake_binding,
                settlement_checker=fake_unresolved,
            )
            assert events[0]["type"] == "CLOSED"
            assert state["positions"][0]["status"] == "CLOSED"
            assert state["positions"][0]["realized_pnl_usd"] < 0

        print("PAPER WORKER SELF-TEST OK")
    finally:
        (
            DATA_DIR,
            FOCUS_FILE,
            BOOK_FILE,
            REQUEST_DIR,
            STATE_FILE,
            EVENTS_FILE,
        ) = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.once:
        run_once()
        return 0

    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
