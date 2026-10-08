"""Resident read-only Book worker.

Consumes only Focus READY, fetches the public Polymarket CLOB book for the
focused outcome token, computes execution diagnostics, and publishes a
snapshot. It never signs or submits orders.
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
from scripts import book_engine
from scripts.learning_queue import enqueue_book_observation
from scripts.learning_versioning import current_strategy_versions

DATA_DIR = BASE_DIR / "data"
FOCUS_FILE = DATA_DIR / "focused_market.json"
BOOK_FILE = DATA_DIR / "book_assessment.json"

CLOB_BASE_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/")
BOOK_URL = f"{CLOB_BASE_URL}/book"

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 5
_LEARNING_INIT_ERROR = None


class BookFetchError(RuntimeError):
    pass


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def read_focus_snapshot(now=None):
    now = now or datetime.now(timezone.utc)

    if not FOCUS_FILE.exists():
        return "MISSING", None

    try:
        payload = _read_json(FOCUS_FILE)
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
    if payload.get("input_status") != "OK":
        return "UPSTREAM_NOT_OK", payload
    if payload.get("state") != "READY":
        return "NOT_READY", payload

    focus = payload.get("focus")
    if not isinstance(focus, dict):
        return "CORRUPT", payload
    if focus.get("status") != "READY":
        return "CORRUPT", payload
    if not str(focus.get("token_id") or "").strip():
        return "CORRUPT", payload

    return "READY", payload


def fetch_book(token_id, timeout=DEFAULT_TIMEOUT_SECONDS):
    try:
        response = requests.get(
            BOOK_URL,
            params={"token_id": token_id},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BookFetchError(type(exc).__name__) from exc

    if response.status_code != 200:
        raise BookFetchError(f"HTTP_{response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise BookFetchError("INVALID_JSON") from exc

    if not isinstance(payload, dict):
        raise BookFetchError("INVALID_PAYLOAD")

    return payload


def _base_output(status, now, focus_payload=None):
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "status": status,
        "book_ok": False,
        "reason_codes": [],
        "source_focus_generated_at": None,
        "source_generation_id": None,
        "source_evidence_id": None,
        "token_id": None,
        "condition_id": None,
        "outcome": None,
        "question": None,
    }

    if isinstance(focus_payload, dict):
        result["source_focus_generated_at"] = focus_payload.get("generated_at")
        result["source_generation_id"] = focus_payload.get("source_generation_id")
        focus = focus_payload.get("focus")
        if isinstance(focus, dict):
            result["source_evidence_id"] = focus.get("last_evidence_id")
            for field in ("token_id", "condition_id", "outcome", "question"):
                result[field] = focus.get(field)

    return result


def _queue_learning(output):
    """Best-effort Book attribution. Never changes Book authority/output."""
    global _LEARNING_INIT_ERROR
    if not isinstance(output, dict):
        return
    if output.get("status") not in {"OK", "API_ERROR"}:
        return
    if not all(
        str(output.get(field) or "").strip()
        for field in (
            "generated_at",
            "source_generation_id",
            "source_evidence_id",
            "condition_id",
            "token_id",
        )
    ):
        return
    try:
        versions = current_strategy_versions()
        enqueue_book_observation(
            output,
            data_dir=DATA_DIR,
            strategy_versions=versions,
        )
        _LEARNING_INIT_ERROR = None
    except Exception as exc:
        signature = f"{type(exc).__name__}: {exc}"
        if signature != _LEARNING_INIT_ERROR:
            print(f"[LEARNING] BOOK_QUEUE_ERROR: {signature}")
            _LEARNING_INIT_ERROR = signature


def run_once(now=None, book_loader=fetch_book):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    focus_status, focus_payload = read_focus_snapshot(now=now)

    if focus_status != "READY":
        output = _base_output(focus_status, now, focus_payload)
        output["reason_codes"] = [f"FOCUS_{focus_status}"]
        save_json_atomic(BOOK_FILE, output)
        return output

    focus = focus_payload["focus"]
    token_id = str(focus["token_id"]).strip()

    try:
        raw_book = book_loader(token_id)
    except Exception as exc:
        output = _base_output("API_ERROR", now, focus_payload)
        code = str(exc).strip() or type(exc).__name__
        output["reason_codes"] = ["BOOK_API_ERROR", code]
        save_json_atomic(BOOK_FILE, output)
        _queue_learning(output)
        return output

    analysis = book_engine.analyze_book(raw_book, focus)

    output = _base_output("OK", now, focus_payload)
    output.update(analysis)
    output["status"] = "OK"

    save_json_atomic(BOOK_FILE, output)
    _queue_learning(output)
    return output


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    last_signature = None

    while True:
        try:
            result = run_once()
            signature = (
                result.get("status"),
                result.get("book_ok"),
                tuple(result.get("reason_codes") or []),
                result.get("token_id"),
            )
            if signature != last_signature:
                print(
                    f"[BOOK] status={result.get('status')} "
                    f"book_ok={result.get('book_ok')} "
                    f"token={result.get('token_id') or '-'} "
                    f"reasons={','.join(result.get('reason_codes') or []) or '-'}"
                )
                last_signature = signature
        except Exception as exc:
            print(f"[BOOK] ERROR: {type(exc).__name__}: {exc}")

        time.sleep(max(1, int(interval)))


def self_test():
    global DATA_DIR, FOCUS_FILE, BOOK_FILE

    original = (DATA_DIR, FOCUS_FILE, BOOK_FILE)
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    try:
        with tempfile.TemporaryDirectory() as temp:
            DATA_DIR = Path(temp)
            FOCUS_FILE = DATA_DIR / "focused_market.json"
            BOOK_FILE = DATA_DIR / "book_assessment.json"

            focus = {
                "token_id": "token-a",
                "condition_id": "condition-a",
                "outcome": "Yes",
                "question": "Market A",
                "direction": "BUY",
                "price": 0.50,
                "status": "READY",
                "progress": 3,
                "last_evidence_id": "0x" + "ab" * 32 + ":1",
            }
            save_json_atomic(
                FOCUS_FILE,
                {
                    "schema_version": 1,
                    "generated_at": now.isoformat(),
                    "input_status": "OK",
                    "source_generation_id": "SELFTEST-X",
                    "state": "READY",
                    "focus": focus,
                },
            )

            def fake_loader(token_id):
                assert token_id == "token-a"
                return {
                    "market": "condition-a",
                    "asset_id": "token-a",
                    "timestamp": "1",
                    "hash": "abc",
                    "bids": [{"price": "0.49", "size": "1000"}],
                    "asks": [
                        {"price": "0.50", "size": "100"},
                        {"price": "0.51", "size": "200"},
                    ],
                    "min_order_size": "5",
                    "tick_size": "0.01",
                    "neg_risk": False,
                    "last_trade_price": "0.50",
                }

            result = run_once(now=now, book_loader=fake_loader)
            assert result["status"] == "OK"
            assert result["book_ok"] is True
            assert result["token_id"] == "token-a"
            assert len(result["quotes"]) == 3
            assert BOOK_FILE.exists()

        print("BOOK WORKER SELF-TEST OK")
    finally:
        DATA_DIR, FOCUS_FILE, BOOK_FILE = original


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
