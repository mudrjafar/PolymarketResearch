"""Resident Focus runner.

Consumes the generation-bound Risk snapshot, advances the pure Focus state
machine, persists durable state, and publishes a compatibility view/events.
No Telegram, orderbook, paper trading, or execution logic lives here.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from machine_common import fresh, save_json_atomic
from scripts import focus_engine

DATA_DIR = BASE_DIR / "data"
RISK_FILE = DATA_DIR / "risk_assessment.json"
STATE_FILE = DATA_DIR / "focus_state.json"
FOCUS_FILE = DATA_DIR / "focused_market.json"
EVENTS_FILE = DATA_DIR / "focus_events.jsonl"

RUNNER_SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 5


class FocusStateError(RuntimeError):
    """Persisted Focus authority is unreadable or structurally invalid."""


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def load_engine_state():
    if not STATE_FILE.exists():
        return focus_engine.new_state()

    try:
        payload = _read_json(STATE_FILE)
    except Exception as exc:
        raise FocusStateError(f"focus state unreadable: {type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise FocusStateError("focus state wrapper must be an object")
    if payload.get("schema_version") != RUNNER_SCHEMA_VERSION:
        raise FocusStateError("focus state wrapper schema mismatch")

    state = payload.get("engine_state")
    if not isinstance(state, dict):
        raise FocusStateError("focus engine state missing")

    return state


def read_risk_snapshot(now=None):
    now = now or datetime.now(timezone.utc)

    if not RISK_FILE.exists():
        return "MISSING", None

    try:
        payload = _read_json(RISK_FILE)
    except Exception:
        return "CORRUPT", None

    if not isinstance(payload, dict):
        return "CORRUPT", None
    if not str(payload.get("source_generation_id") or "").strip():
        return "CORRUPT", None
    if not isinstance(payload.get("results"), list):
        return "CORRUPT", None
    if not payload.get("generated_at"):
        return "CORRUPT", None

    if not fresh(payload.get("generated_at"), now=now):
        return "STALE", payload

    return "OK", payload


def _state_wrapper(state, source_generation_id, now):
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "updated_at": now.isoformat(),
        "last_source_generation_id": source_generation_id,
        "engine_state": state,
    }


def _view(state, input_status, payload, now):
    focus = state.get("focus") if isinstance(state, dict) else None
    source_generation_id = None
    source_generated_at = None

    if isinstance(payload, dict):
        source_generation_id = payload.get("source_generation_id")
        source_generated_at = payload.get("source_generated_at")

    view = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "input_status": input_status,
        "source_generation_id": source_generation_id,
        "source_generated_at": source_generated_at,
        "state": focus_engine.label(focus),
        "focus": focus,
        "challenger": state.get("challenger") if isinstance(state, dict) else None,
        "last_invalidated": state.get("last_invalidated") if isinstance(state, dict) else None,
    }

    if isinstance(focus, dict):
        for field in (
            "token_id",
            "condition_id",
            "outcome",
            "question",
            "direction",
            "price",
            "status",
            "progress",
        ):
            if field in focus:
                view[field] = focus[field]

    return view


def _append_events(events, source_generation_id):
    if not events:
        return

    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with EVENTS_FILE.open("a", encoding="utf-8") as stream:
        for event in events:
            row = dict(event)
            row["source_generation_id"] = source_generation_id
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_once(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    state = load_engine_state()
    input_status, payload = read_risk_snapshot(now=now)

    # Input failure is not equivalent to a valid empty Risk generation.
    # Preserve Focus authority unchanged until a valid current snapshot exists.
    if input_status != "OK":
        save_json_atomic(FOCUS_FILE, _view(state, input_status, payload, now))
        return state, [], input_status

    candidates = [
        row
        for row in payload["results"]
        if isinstance(row, dict)
    ]

    new_state, events = focus_engine.step(state, candidates, now=now)
    generation_id = str(payload["source_generation_id"])

    # Durable state is authority. Events and the compatibility view are
    # downstream projections of the already-committed state transition.
    save_json_atomic(
        STATE_FILE,
        _state_wrapper(new_state, generation_id, now),
    )
    _append_events(events, generation_id)
    save_json_atomic(
        FOCUS_FILE,
        _view(new_state, "OK", payload, now),
    )

    return new_state, events, "OK"


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    last_status = None

    while True:
        try:
            state, events, status = run_once()
            if status != last_status:
                print(f"[FOCUS] input={status}")
                last_status = status
            for event in events:
                print(
                    f"[FOCUS] {event.get('type')} | "
                    f"{event.get('question') or event.get('token_id')} | "
                    f"{event.get('state')}"
                )
        except FocusStateError as exc:
            # Never reset corrupted durable Focus authority automatically.
            print(f"[FOCUS] STATE_ERROR: {exc}")
        except Exception as exc:
            print(f"[FOCUS] ERROR: {type(exc).__name__}: {exc}")

        time.sleep(max(1, int(interval)))


def _self_test_candidate(token_id, tx_byte):
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    return {
        "token_id": token_id,
        "condition_id": f"condition-{token_id}",
        "outcome": "Yes",
        "question": f"Market {token_id}",
        "direction": "BUY",
        "price": 0.42,
        "risk_ok": True,
        "decision": "PASS",
        "reason_codes": [],
        "evidence_id": "0x" + tx_byte * 64 + ":1",
        "evidence_cursor": [100, 1],
        "evidence_at": now.isoformat(),
    }


def self_test():
    global DATA_DIR, RISK_FILE, STATE_FILE, FOCUS_FILE, EVENTS_FILE

    original = (DATA_DIR, RISK_FILE, STATE_FILE, FOCUS_FILE, EVENTS_FILE)
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    try:
        with tempfile.TemporaryDirectory() as temp:
            DATA_DIR = Path(temp)
            RISK_FILE = DATA_DIR / "risk_assessment.json"
            STATE_FILE = DATA_DIR / "focus_state.json"
            FOCUS_FILE = DATA_DIR / "focused_market.json"
            EVENTS_FILE = DATA_DIR / "focus_events.jsonl"

            save_json_atomic(
                RISK_FILE,
                {
                    "source_generation_id": "SELFTEST-X",
                    "source_generated_at": now.isoformat(),
                    "generated_at": now.isoformat(),
                    "results": [
                        _self_test_candidate("B", "b"),
                        _self_test_candidate("A", "a"),
                    ],
                },
            )

            state, events, status = run_once(now=now)

            assert status == "OK"
            assert state["focus"]["token_id"] == "B"
            assert "score" not in state["focus"]
            assert events[-1]["type"] == "LOCKED"
            assert STATE_FILE.exists()
            assert FOCUS_FILE.exists()
            assert EVENTS_FILE.exists()

        print("FOCUS RUNNER SELF-TEST OK")
    finally:
        DATA_DIR, RISK_FILE, STATE_FILE, FOCUS_FILE, EVENTS_FILE = original


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
