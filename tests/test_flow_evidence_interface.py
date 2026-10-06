"""Flow evidence interface - frozen contract for the Focus Engine.

flow_state[token_id] must expose

    evidence_id      "<transaction_hash>:<log_index>" (lower-case hash)
    evidence_cursor  [block, log_index]   (JSON list of two ints)
    evidence_at      block timestamp of that trade (ISO, tz-aware)

and these values may ONLY come from a trade that actually CONFIRMED the
current flow:

    * canonical chain identity (transaction_hash + log_index),
    * NEW since the last analysis of that token,
    * same side as the 5m flow direction,
    * verification check passed.

Every integration test drives the real flow_tracker.main() for exactly one
cycle against temporary files. Nothing here touches data/, the network,
Telegram or any secret.

Run only this file:
    .venv\\Scripts\\python.exe -B -m pytest tests\\test_flow_evidence_interface.py -q
"""

import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts import flow_tracker as flow
from scripts.focus_engine import label, new_state, step


TOKEN_YES = "yes-token"
TOKEN_NO = "no-token"

EVIDENCE_FIELDS = ("evidence_id", "evidence_cursor", "evidence_at")

# Minimum flow_state contract consumed downstream (Diamond / Risk / Focus).
CONTRACT_KEYS = {
    "schema_version",
    "token_id",
    "condition_id",
    "outcome",
    "direction",
    "last_trade_at",
    "source_updated_at",
    "evidence_id",
    "evidence_cursor",
    "evidence_at",
    "market",
    "flow_1m",
    "flow_5m",
    "flow_15m",
    "state",
    "confirmations",
    "verified",
    "data_confidence",
    "resolution_state",
    "remaining_seconds",
    "candidate",
    "verification_check_passed",
}


# ============================================================
# HELPERS
# ============================================================

def evidence_id_of(row):
    return f"{row['transaction_hash'].lower()}:{row['log_index']}"


def cursor_of(row):
    return [row["block"], row["log_index"]]


def assert_evidence_from(entry, row):
    assert entry["evidence_id"] == evidence_id_of(row)
    assert entry["evidence_cursor"] == cursor_of(row)
    assert entry["evidence_at"] == row["block_timestamp"]


def assert_no_evidence(entry):
    for name in EVIDENCE_FIELDS:
        assert name in entry
        assert entry[name] is None


def evidence_of(entry):
    return {name: entry[name] for name in EVIDENCE_FIELDS}


class Pipeline:
    """Runs exactly one real flow_tracker.main() cycle per run_cycle()."""

    def __init__(self, tmp_path, monkeypatch):
        self.trades_file = tmp_path / "trades.jsonl"
        self.flow_file = tmp_path / "flow_state.json"
        self.verify_file = tmp_path / "verification_state.json"
        self.rows = []

        now = datetime.now(timezone.utc)
        self.t0 = now - timedelta(seconds=30)
        self.end_date = (now + timedelta(days=2)).isoformat()

        monkeypatch.setattr(flow, "TRADES_FILE", self.trades_file)
        monkeypatch.setattr(flow, "FLOW_STATE_FILE", self.flow_file)
        monkeypatch.setattr(flow, "VERIFICATION_STATE_FILE", self.verify_file)
        monkeypatch.setattr(flow, "market_states", {})
        monkeypatch.setattr(flow, "flow_states", {})
        monkeypatch.setattr(flow, "analysis_schedule", {})

    def trade(self, n, side="BUY", token=TOKEN_YES):
        """One collector-style row. n orders it in event time (seconds)."""
        uid = n + (500 if token == TOKEN_NO else 0)
        return {
            "collector_version": 4,
            "condition_id": "same-condition",
            "token_id": token,
            "outcome": "Yes" if token == TOKEN_YES else "No",
            "question": "EVIDENCE INTERFACE TEST",
            "side_label": side,
            "trade_usd": 100.0,
            "fill_price": 0.40,
            "block": 1000 + uid,
            "log_index": uid % 7,
            "transaction_hash": f"0x{uid:064x}",
            "block_timestamp": (self.t0 + timedelta(seconds=n)).isoformat(),
            # Local ingestion time differs on purpose: evidence_at must be
            # the block timestamp, never detected_at.
            "detected_at": (self.t0 + timedelta(seconds=n + 5)).isoformat(),
            "end_date": self.end_date,
            "active": True,
            "closed": False,
            "accepting_orders": True,
        }

    def add(self, *rows):
        self.rows.extend(rows)

    def run_cycle(self):
        self.trades_file.write_text(
            "\n".join(json.dumps(row) for row in self.rows) + "\n",
            encoding="utf-8",
        )

        with patch.object(flow.time, "sleep", side_effect=InterruptedError), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                flow.main()
            except InterruptedError:
                pass

        return json.loads(self.flow_file.read_text(encoding="utf-8"))


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    return Pipeline(tmp_path, monkeypatch)


# ============================================================
# 1. EVIDENCE TRIPLE IS PRESENT AND COMES FROM THE CONFIRMING TRADE
# ============================================================

def test_confirming_trade_populates_evidence_triple(pipeline):
    rows = [pipeline.trade(n) for n in range(10)]
    pipeline.add(*rows)

    entry = pipeline.run_cycle()[TOKEN_YES]

    assert entry["direction"] == "BUY"
    assert entry["verification_check_passed"] is True
    assert entry["confirmations"] == 1
    assert_evidence_from(entry, rows[-1])

    # Event time (block timestamp), never local ingestion time.
    assert entry["evidence_at"] != rows[-1]["detected_at"]


def test_flow_state_contract_keys_and_types_are_frozen(pipeline):
    pipeline.add(*[pipeline.trade(n) for n in range(10)])

    entry = pipeline.run_cycle()[TOKEN_YES]

    assert CONTRACT_KEYS <= set(entry)
    assert entry["schema_version"] == flow.FLOW_SCHEMA_VERSION == 4
    assert entry["token_id"] == TOKEN_YES
    assert entry["condition_id"] == "same-condition"
    assert entry["outcome"] == "Yes"

    assert isinstance(entry["evidence_id"], str)
    assert entry["evidence_id"] == entry["evidence_id"].lower()

    cursor = entry["evidence_cursor"]
    assert isinstance(cursor, list) and len(cursor) == 2
    assert all(isinstance(x, int) and not isinstance(x, bool) for x in cursor)
    assert entry["evidence_id"].endswith(f":{cursor[1]}")

    parsed = datetime.fromisoformat(entry["evidence_at"])
    assert parsed.utcoffset() is not None


def test_focus_engine_accepts_flow_evidence_as_is(pipeline):
    pipeline.add(*[pipeline.trade(n) for n in range(10)])
    entry = pipeline.run_cycle()[TOKEN_YES]

    candidate = {
        "token_id": entry["token_id"],
        "condition_id": entry["condition_id"],
        "outcome": entry["outcome"],
        "question": entry["market"]["question"],
        "direction": entry["direction"],
        "price": entry["market"]["price"],
        "score": 90,
        "risk_ok": True,
        "reason_codes": [],
        "evidence_id": entry["evidence_id"],
        "evidence_cursor": entry["evidence_cursor"],
        "evidence_at": entry["evidence_at"],
    }

    state, events = step(new_state(), [candidate], datetime.now(timezone.utc))
    focus = state["focus"]

    assert focus is not None
    assert focus["token_id"] == TOKEN_YES
    assert label(focus) == "LOCKED"
    assert focus["last_evidence_id"] == entry["evidence_id"]
    assert focus["last_evidence_cursor"] == entry["evidence_cursor"]
    assert events[-1]["type"] == "LOCKED"


# ============================================================
# 2. NO EVIDENCE WITHOUT A TRULY CONFIRMING TRADE
# ============================================================

def test_latest_trade_on_opposite_side_does_not_confirm(pipeline):
    rows = [pipeline.trade(n) for n in range(9)]
    rows.append(pipeline.trade(9, side="SELL"))  # latest trade opposes the flow
    pipeline.add(*rows)

    entry = pipeline.run_cycle()[TOKEN_YES]

    # Flow is BUY and passes verification; the latest trade just is not
    # a supporting event, so no evidence may be recorded.
    assert entry["direction"] == "BUY"
    assert entry["verification_check_passed"] is True
    assert entry["confirmations"] == 0
    assert_no_evidence(entry)


def test_unverified_flow_never_stores_evidence(pipeline):
    # 6 BUY trades: candidate (>=5 trades, >=$250) but not verified (<8 trades).
    pipeline.add(*[pipeline.trade(n) for n in range(6)])

    entry = pipeline.run_cycle()[TOKEN_YES]

    assert entry["candidate"] is True
    assert entry["verification_check_passed"] is False
    assert entry["confirmations"] == 0
    assert_no_evidence(entry)


def test_rows_without_chain_identity_never_produce_evidence(pipeline):
    rows = []
    for n in range(10):
        row = pipeline.trade(n)
        del row["transaction_hash"]
        del row["log_index"]
        rows.append(row)
    pipeline.add(*rows)

    entry = pipeline.run_cycle()[TOKEN_YES]

    assert entry["verification_check_passed"] is True
    assert entry["confirmations"] == 0
    assert_no_evidence(entry)


def test_evidence_never_crosses_between_yes_and_no_tokens(pipeline):
    yes_rows = [pipeline.trade(n, "BUY", TOKEN_YES) for n in range(10)]
    no_rows = [pipeline.trade(n, "SELL", TOKEN_NO) for n in range(10)]
    pipeline.add(*yes_rows, *no_rows)

    state = pipeline.run_cycle()
    yes, no = state[TOKEN_YES], state[TOKEN_NO]

    assert_evidence_from(yes, yes_rows[-1])
    assert_evidence_from(no, no_rows[-1])
    assert yes["evidence_id"] != no["evidence_id"]
    assert (yes["direction"], no["direction"]) == ("BUY", "SELL")
    assert yes["confirmations"] == 1
    assert no["confirmations"] == 1


# ============================================================
# 3. EVIDENCE ADVANCES ONLY WITH NEW CONFIRMING TRADES
# ============================================================

def test_evidence_advances_only_with_new_confirming_trades(pipeline):
    rows = [pipeline.trade(n) for n in range(10)]
    pipeline.add(*rows)

    entry = pipeline.run_cycle()[TOKEN_YES]
    assert_evidence_from(entry, rows[-1])
    assert entry["confirmations"] == 1
    cursors = [tuple(entry["evidence_cursor"])]

    # Two further NEW confirming trades -> VERIFIED, cursor strictly increases.
    for n in (10, 11):
        row = pipeline.trade(n)
        rows.append(row)
        pipeline.add(row)
        entry = pipeline.run_cycle()[TOKEN_YES]
        assert_evidence_from(entry, row)
        cursors.append(tuple(entry["evidence_cursor"]))

    assert entry["confirmations"] == 3
    assert entry["state"] == "VERIFIED"
    assert entry["verified"] is True
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == 3

    verified_evidence = evidence_of(entry)

    # Re-running on the very same trades must not consume the evidence again.
    entry = pipeline.run_cycle()[TOKEN_YES]
    assert evidence_of(entry) == verified_evidence
    assert entry["confirmations"] == 3

    # A newer trade on the OPPOSITE side becomes the latest trade but does not
    # confirm a BUY flow: last_trade_at moves, evidence stays.
    sell = pipeline.trade(12, side="SELL")
    pipeline.add(sell)
    entry = pipeline.run_cycle()[TOKEN_YES]

    assert entry["last_trade_at"] == sell["block_timestamp"]
    assert evidence_of(entry) == verified_evidence
    assert entry["evidence_at"] == rows[11]["block_timestamp"]
    assert entry["confirmations"] == 3


def test_reset_keeps_last_consumed_evidence_and_it_cannot_count_again(monkeypatch):
    monkeypatch.setattr(flow, "market_states", {})
    stamp = datetime.now(timezone.utc).isoformat()

    for index, evidence in enumerate(("e1", "e2"), start=1):
        flow.update_market_state(
            "t", True, True, evidence, "BUY",
            evidence_cursor=[index, 0], evidence_at=stamp,
        )

    assert flow.market_states["t"]["confirmations"] == 2

    # Verification fails: the confirmation streak resets, but the last
    # consumed evidence stays recorded.
    state = flow.update_market_state("t", True, False, None, "BUY")
    assert state["confirmations"] == 0
    assert state["last_confirmation_evidence"] == "e2"
    assert state["last_confirmation_cursor"] == [2, 0]
    assert state["last_confirmation_at"] == stamp

    # The same (already consumed) evidence cannot count again ...
    state = flow.update_market_state(
        "t", True, True, "e2", "BUY",
        evidence_cursor=[2, 0], evidence_at=stamp,
    )
    assert state["confirmations"] == 0

    # ... but genuinely new evidence does.
    state = flow.update_market_state(
        "t", True, True, "e3", "BUY",
        evidence_cursor=[3, 0], evidence_at=stamp,
    )
    assert state["confirmations"] == 1
    assert state["last_confirmation_evidence"] == "e3"
    assert state["last_confirmation_cursor"] == [3, 0]
