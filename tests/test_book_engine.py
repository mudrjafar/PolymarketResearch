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
