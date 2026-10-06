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
        "state": data / "telegram_state.json",
    }

    monkeypatch.setattr(bot, "DATA", data, raising=False)
    monkeypatch.setattr(bot, "DIAMOND_MANIFEST", paths["manifest"], raising=False)
    monkeypatch.setattr(bot, "DIAMOND_GENERATIONS", generations, raising=False)
    monkeypatch.setattr(bot, "FOCUS", paths["focus"], raising=False)
    monkeypatch.setattr(bot, "BOOK", paths["book"], raising=False)
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


def test_execution_message_is_informational_not_trade_opening():
    message = bot.execution_message(
        focus_payload(),
        book_payload(),
    )

    assert "READY + BOOK PASS" in message
    assert "$25" in message
    assert "$50" in message
    assert "$100" in message
    assert "No trade was opened" in message


def test_telegram_has_no_paper_state_ownership_or_system_online_claim():
    source = inspect.getsource(bot)

    assert "PAPER =" not in source
    assert "def open_paper" not in source
    assert "def update_paper" not in source
    assert "positions_cmd" not in source
    assert "System online" not in source
