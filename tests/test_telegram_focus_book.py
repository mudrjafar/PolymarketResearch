import inspect
import json
from datetime import datetime, timedelta, timezone

import telegram_bot as bot
from machine_common import save_json_atomic


NOW = datetime.now(timezone.utc)


def configure_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    generations = data / "diamond_generations"
    generations.mkdir(parents=True)

    paths = {
        "data": data,
        "manifest": data / "diamond_generation.json",
        "generations": generations,
        "focus": data / "focused_market.json",
        "book": data / "book_assessment.json",
        "paper_requests": data / "paper_requests",
        "paper_state": data / "paper_state.json",
        "state": data / "telegram_state.json",
    }

    monkeypatch.setattr(bot, "DATA", data, raising=False)
    monkeypatch.setattr(bot, "DIAMOND_MANIFEST", paths["manifest"], raising=False)
    monkeypatch.setattr(bot, "DIAMOND_GENERATIONS", generations, raising=False)
    monkeypatch.setattr(bot, "FOCUS", paths["focus"], raising=False)
    monkeypatch.setattr(bot, "BOOK", paths["book"], raising=False)
    monkeypatch.setattr(bot, "PAPER_REQUEST_DIR", paths["paper_requests"], raising=False)
    monkeypatch.setattr(bot, "PAPER_STATE", paths["paper_state"], raising=False)
    monkeypatch.setattr(bot, "STATE", paths["state"], raising=False)

    return paths


def focus_payload(*, state="READY", input_status="OK"):
    focus = {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "price": 0.50,
        "status": "READY" if state == "READY" else "WAIT",
        "progress": 3 if state == "READY" else 2,
        "last_evidence_id": "0x" + "ab" * 32 + ":7",
        "locked_at": NOW.isoformat(),
    }
    return {
        "schema_version": 1,
        "generated_at": NOW.isoformat(),
        "input_status": input_status,
        "source_generation_id": "GEN-X",
        "state": state,
        "focus": focus,
        "last_invalidated": None,
    }


def book_payload(*, book_ok=True, status="OK"):
    return {
        "schema_version": 1,
        "generated_at": NOW.isoformat(),
        "status": status,
        "book_ok": book_ok,
        "reason_codes": [] if book_ok else ["INSUFFICIENT_DEPTH_100"],
        "source_generation_id": "GEN-X",
        "source_evidence_id": "0x" + "ab" * 32 + ":7",
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": "Yes",
        "question": "Market A",
        "best_bid": 0.49,
        "best_ask": 0.50,
        "spread_bps_mid": 202.0202,
        "quotes": [
            {
                "notional_usd": 25.0,
                "complete": True,
                "outcome_tokens": 50.0,
                "vwap": 0.50,
                "slippage_bps": 0.0,
            },
            {
                "notional_usd": 50.0,
                "complete": True,
                "outcome_tokens": 100.0,
                "vwap": 0.50,
                "slippage_bps": 0.0,
            },
            {
                "notional_usd": 100.0,
                "complete": True,
                "outcome_tokens": 198.0,
                "vwap": 0.505,
                "slippage_bps": 100.0,
            },
        ],
    }


def publish_generation(paths, *, generation_id="GEN-X", diamonds=None, analysis=None, published_at=None):
    published_at = published_at or NOW.isoformat()
    generation_dir = paths["generations"] / generation_id
    generation_dir.mkdir(parents=True)

    save_json_atomic(
        generation_dir / "diamonds.json",
        [] if diamonds is None else diamonds,
    )
    save_json_atomic(
        generation_dir / "diamond_analysis_v3.json",
        [] if analysis is None else analysis,
    )
    save_json_atomic(
        paths["manifest"],
        {
            "schema_version": 1,
            "generation_id": generation_id,
            "published_at": published_at,
            "generation_dir": f"diamond_generations/{generation_id}",
        },
    )


def test_execution_ready_requires_exact_focus_book_binding():
    ready, reason = bot.execution_binding(
        "OK",
        focus_payload(),
        "OK",
        book_payload(),
    )

    assert ready is True
    assert reason == "READY_BOOK_PASS"


def test_focus_not_ready_can_never_be_reconstructed_from_book_pass():
    ready, reason = bot.execution_binding(
        "OK",
        focus_payload(state="WAIT 2/3"),
        "OK",
        book_payload(),
    )

    assert ready is False
    assert reason == "FOCUS_NOT_READY"


def test_book_block_can_never_be_reconstructed_as_execution_ready():
    ready, reason = bot.execution_binding(
        "OK",
        focus_payload(),
        "OK",
        book_payload(book_ok=False),
    )

    assert ready is False
    assert reason == "BOOK_BLOCK"


def test_binding_rejects_generation_evidence_and_identity_mismatch():
    cases = [
        ("source_generation_id", "OTHER", "GENERATION_ID_MISMATCH"),
        ("source_evidence_id", "0x" + "cd" * 32 + ":9", "EVIDENCE_ID_MISMATCH"),
        ("token_id", "token-b", "TOKEN_ID_MISMATCH"),
        ("condition_id", "condition-b", "CONDITION_ID_MISMATCH"),
    ]

    for field, value, expected in cases:
        book = book_payload()
        book[field] = value

        ready, reason = bot.execution_binding(
            "OK",
            focus_payload(),
            "OK",
            book,
        )

        assert ready is False
        assert reason == expected


def test_focus_and_book_snapshot_statuses_distinguish_missing_corrupt_stale(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)

    assert bot.focus_snapshot() == ("MISSING", None)
    assert bot.book_snapshot() == ("MISSING", None)

    paths["focus"].write_text("{bad-json", encoding="utf-8")
    paths["book"].write_text("[]", encoding="utf-8")
    assert bot.focus_snapshot()[0] == "CORRUPT"
    assert bot.book_snapshot()[0] == "CORRUPT"

    stale = (NOW - timedelta(minutes=10)).isoformat()
    focus = focus_payload()
    book = book_payload()
    focus["generated_at"] = stale
    book["generated_at"] = stale
    save_json_atomic(paths["focus"], focus)
    save_json_atomic(paths["book"], book)

    assert bot.focus_snapshot()[0] == "STALE"
    assert bot.book_snapshot()[0] == "STALE"


def test_generation_snapshot_distinguishes_valid_empty_from_input_failure(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)

    assert bot.diamonds_snapshot()[0] == "MISSING"

    publish_generation(paths, diamonds=[], analysis=[])

    status, rows = bot.diamonds_snapshot()
    assert status == "OK"
    assert rows == []

    status, rows = bot.analyses_snapshot()
    assert status == "OK"
    assert rows == []


def test_stale_generation_is_not_treated_as_empty(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    stale = (NOW - timedelta(minutes=10)).isoformat()

    publish_generation(
        paths,
        diamonds=[],
        analysis=[],
        published_at=stale,
    )

    assert bot.diamonds_snapshot()[0] == "STALE"
    assert bot.analyses_snapshot()[0] == "STALE"


def test_keyboard_exposes_analysis_only_no_paper_action():
    row = {"market_key": "market-a"}

    markup = bot.keyboard(row)
    callback_data = [
        button.callback_data
        for row_buttons in markup.inline_keyboard
        for button in row_buttons
    ]

    assert callback_data == [f"a:{bot.sid(row)}"]
    assert all(not value.startswith("p:") for value in callback_data)


def test_execution_message_is_informational_and_user_controlled():
    message = bot.execution_message(
        focus_payload(),
        book_payload(),
    )

    assert "READY + BOOK PASS" in message
    assert "$25" in message
    assert "$50" in message
    assert "$100" in message
    assert "Choose a virtual amount" in message
    assert "Telegram only submits the request" in message


def test_paper_open_keyboard_exposes_only_explicit_user_amounts():
    markup = bot.paper_open_keyboard()
    callback_data = [
        button.callback_data
        for row_buttons in markup.inline_keyboard
        for button in row_buttons
    ]

    assert callback_data == ["po:25", "po:50", "po:100"]


def test_submit_open_request_writes_intent_but_not_paper_state(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    focus = focus_payload()
    book = book_payload()
    save_json_atomic(paths["focus"], focus)
    save_json_atomic(paths["book"], book)

    request_id, err = bot.submit_open_request(25, "123")

    assert err is None
    request_path = paths["paper_requests"] / f"{request_id}.json"
    assert request_path.exists()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["action"] == "OPEN"
    assert request["amount_usd"] == 25.0
    assert request["token_id"] == "token-a"
    assert request["source_generation_id"] == "GEN-X"
    assert request["source_evidence_id"] == focus["focus"]["last_evidence_id"]
    assert not paths["paper_state"].exists()


def test_submit_close_request_reads_state_and_writes_close_intent_only(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    original_state = {
        "schema_version": 1,
        "updated_at": NOW.isoformat(),
        "positions": [
            {
                "paper_id": "PAPER-abc",
                "status": "OPEN",
                "token_id": "token-a",
            }
        ],
        "processed_request_ids": [],
    }
    save_json_atomic(paths["paper_state"], original_state)

    request_id, err = bot.submit_close_request("PAPER-abc", "123")

    assert err is None
    request = json.loads(
        (paths["paper_requests"] / f"{request_id}.json").read_text(encoding="utf-8")
    )
    assert request["action"] == "CLOSE"
    assert request["paper_id"] == "PAPER-abc"
    assert json.loads(paths["paper_state"].read_text(encoding="utf-8")) == original_state


def test_telegram_has_no_paper_state_ownership_or_system_online_claim():
    source = inspect.getsource(bot)

    assert "PAPER =" not in source
    assert "def open_paper" not in source
    assert "def update_paper" not in source
    assert "save_json_atomic(PAPER_STATE" not in source
    assert "System online" not in source


def test_settled_paper_message_surfaces_authoritative_result():
    message = bot.paper_position_message(
        {
            "status": "SETTLED",
            "question": "Market A",
            "outcome": "Yes",
            "investment_usd": 25.0,
            "tokens": 40.0,
            "entry": {"vwap": 0.6, "effective_entry_price": 0.625},
            "mark_status": "SETTLED",
            "payout_per_token": 1.0,
            "settlement_value_usd": 40.0,
            "realized_pnl_usd": 15.0,
            "realized_return_pct": 60.0,
            "settlement_finality_source": "RPC_FINALIZED",
        }
    )

    assert "PAPER SETTLED" in message
    assert "Payout/token: 1.0000" in message
    assert "Settlement value: $40.00" in message
    assert "Realized P/L $15.00 (60.00%)" in message
    assert "RPC_FINALIZED" in message
