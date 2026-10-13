import pytest

from scripts.book_engine import analyze_book


def focus(price=0.50):
    return {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "price": price,
    }


def book(*, bids=None, asks=None, asset_id="token-a", market="condition-a"):
    return {
        "market": market,
        "asset_id": asset_id,
        "timestamp": "123",
        "hash": "book-hash",
        "bids": bids if bids is not None else [{"price": "0.49", "size": "1000"}],
        "asks": asks if asks is not None else [
            {"price": "0.50", "size": "100"},
            {"price": "0.51", "size": "200"},
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": "0.50",
    }


def test_execution_quotes_for_25_50_100_and_vwap():
    result = analyze_book(book(), focus())

    assert result["book_ok"] is True
    assert result["best_bid"] == 0.49
    assert result["best_ask"] == 0.50
    assert result["spread"] == 0.01
    assert [q["notional_usd"] for q in result["quotes"]] == [25.0, 50.0, 100.0]
    assert all(q["complete"] for q in result["quotes"])
    assert result["quotes"][0]["vwap"] == 0.50
    assert result["quotes"][1]["vwap"] == 0.50
    assert result["quotes"][2]["vwap"] > 0.50
    assert result["quotes"][2]["slippage_bps"] > 0


def test_levels_are_sorted_by_price_not_trusted_from_wire_order():
    result = analyze_book(
        book(
            bids=[
                {"price": "0.47", "size": "100"},
                {"price": "0.49", "size": "100"},
            ],
            asks=[
                {"price": "0.53", "size": "100"},
                {"price": "0.51", "size": "100"},
            ],
        ),
        focus(),
    )

    assert result["best_bid"] == 0.49
    assert result["best_ask"] == 0.51


def test_insufficient_depth_fails_required_test_sizes():
    result = analyze_book(
        book(asks=[{"price": "0.50", "size": "60"}]),
        focus(),
    )

    assert result["book_ok"] is False
    assert "INSUFFICIENT_DEPTH_50" in result["reason_codes"]
    assert "INSUFFICIENT_DEPTH_100" in result["reason_codes"]
    assert result["quotes"][0]["complete"] is True
    assert result["quotes"][1]["complete"] is False


def test_crossed_book_fails_closed():
    result = analyze_book(
        book(
            bids=[{"price": "0.52", "size": "100"}],
            asks=[{"price": "0.51", "size": "1000"}],
        ),
        focus(),
    )

    assert result["book_ok"] is False
    assert "BOOK_CROSSED" in result["reason_codes"]


def test_identity_mismatch_fails_closed():
    result = analyze_book(
        book(asset_id="other-token", market="other-condition"),
        focus(),
    )

    assert result["book_ok"] is False
    assert "TOKEN_ID_MISMATCH" in result["reason_codes"]
    assert "CONDITION_ID_MISMATCH" in result["reason_codes"]


def test_malformed_level_is_not_coerced_to_zero():
    result = analyze_book(
        book(asks=[{"price": "0.50", "size": "not-a-number"}]),
        focus(),
    )

    assert result["book_ok"] is False
    assert "BOOK_LEVEL_INVALID" in result["reason_codes"]
    assert "BOOK_ASKS_EMPTY" in result["reason_codes"]


def test_price_move_is_measured_against_focus_price():
    result = analyze_book(
        book(asks=[{"price": "0.52", "size": "1000"}]),
        focus(price=0.50),
    )

    assert result["best_ask_move_from_focus"] == 0.02
    assert result["best_ask_move_from_focus_bps"] == 400.0


@pytest.mark.parametrize(("field", "value", "code"), [
    ("asset_id", None, "BOOK_ASSET_ID_MISSING"),
    ("asset_id", "", "BOOK_ASSET_ID_MISSING"),
    ("market", None, "BOOK_MARKET_ID_MISSING"),
    ("market", "  ", "BOOK_MARKET_ID_MISSING"),
])
def test_missing_clob_identity_cannot_produce_book_pass(field, value, code):
    raw = book()
    raw[field] = value
    result = analyze_book(raw, focus())
    assert result["book_ok"] is False
    assert code in result["reason_codes"]


@pytest.mark.parametrize(("field", "code"), [
    ("asset_id", "BOOK_ASSET_ID_MISSING"),
    ("market", "BOOK_MARKET_ID_MISSING"),
])
def test_absent_clob_identity_cannot_produce_book_pass(field, code):
    raw = book()
    raw.pop(field)
    result = analyze_book(raw, focus())
    assert result["book_ok"] is False
    assert code in result["reason_codes"]


@pytest.mark.parametrize(("field", "code"), [
    ("token_id", "TOKEN_ID_MISSING"),
    ("condition_id", "CONDITION_ID_MISSING"),
])
def test_missing_focus_identity_cannot_produce_book_pass(field, code):
    f = focus()
    f[field] = ""
    result = analyze_book(book(), f)
    assert result["book_ok"] is False
    assert code in result["reason_codes"]



@pytest.mark.parametrize(("field", "value", "reason"), [
    ("asset_id", 123, "BOOK_ASSET_ID_INVALID_TYPE"),
    ("asset_id", 0, "BOOK_ASSET_ID_INVALID_TYPE"),
    ("asset_id", True, "BOOK_ASSET_ID_INVALID_TYPE"),
    ("asset_id", [], "BOOK_ASSET_ID_INVALID_TYPE"),
    ("asset_id", {"id": "token-a"}, "BOOK_ASSET_ID_INVALID_TYPE"),
    ("market", 123, "BOOK_MARKET_ID_INVALID_TYPE"),
    ("market", False, "BOOK_MARKET_ID_INVALID_TYPE"),
    ("market", [], "BOOK_MARKET_ID_INVALID_TYPE"),
    ("market", {"id": "condition-a"}, "BOOK_MARKET_ID_INVALID_TYPE"),
])
def test_non_string_clob_identity_is_never_coerced(field, value, reason):
    raw = book()
    raw[field] = value
    result = analyze_book(raw, focus())
    assert result["book_ok"] is False
    assert reason in result["reason_codes"]


@pytest.mark.parametrize(("field", "focus_field"), [
    ("asset_id", "token_id"),
    ("market", "condition_id"),
])
def test_matching_numeric_ids_on_both_sides_do_not_pass(field, focus_field):
    raw = book()
    f = focus()
    raw[field] = 12345
    f[focus_field] = 12345
    result = analyze_book(raw, f)
    assert result["book_ok"] is False


def test_legacy_ctf_string_ids_still_match():
    f = focus()
    f["token_id"] = "123456789012345678901234567890"
    f["condition_id"] = "0x" + "ab" * 32
    raw = book(asset_id=f["token_id"], market=f["condition_id"].upper().replace("0X", "0x"))
    assert analyze_book(raw, f)["book_ok"] is True
