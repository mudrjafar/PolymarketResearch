import pytest

from scripts import paper_engine


def book():
    return {
        "market": "condition-a",
        "asset_id": "token-a",
        "bids": [
            {"price": "0.48", "size": "100"},
            {"price": "0.49", "size": "50"},
        ],
        "asks": [
            {"price": "0.51", "size": "100"},
            {"price": "0.50", "size": "50"},
        ],
    }


def test_buy_consumes_best_asks_and_reports_vwap():
    result = paper_engine.simulate_buy(book(), 50)

    assert result["complete"] is True
    assert result["gross_tokens"] > 99
    assert result["net_tokens"] == result["gross_tokens"]
    assert result["vwap"] > 0.50
    assert result["worst_price"] == 0.51
    assert result["slippage_bps"] > 0


def test_buy_fee_reduces_delivered_tokens():
    no_fee = paper_engine.simulate_buy(book(), 25)
    with_fee = paper_engine.simulate_buy(
        book(),
        25,
        fee_rate=0.05,
        fee_exponent=1,
    )

    assert with_fee["complete"] is True
    assert with_fee["fee_usd"] > 0
    assert with_fee["gross_tokens"] == no_fee["gross_tokens"]
    assert with_fee["net_tokens"] < no_fee["net_tokens"]
    assert with_fee["effective_entry_price"] > with_fee["vwap"]


def test_sell_consumes_best_bids_and_reports_net_proceeds():
    result = paper_engine.simulate_sell(book(), 99)

    assert result["complete"] is True
    assert result["sold_tokens"] == 99
    assert result["vwap"] < 0.49
    assert result["worst_price"] == 0.48
    assert result["net_proceeds_usd"] == result["gross_proceeds_usd"]


def test_sell_fee_reduces_net_proceeds():
    no_fee = paper_engine.simulate_sell(book(), 50)
    with_fee = paper_engine.simulate_sell(
        book(),
        50,
        fee_rate=0.05,
        fee_exponent=1,
    )

    assert with_fee["fee_usd"] > 0
    assert with_fee["gross_proceeds_usd"] == no_fee["gross_proceeds_usd"]
    assert with_fee["net_proceeds_usd"] < no_fee["net_proceeds_usd"]
    assert with_fee["effective_exit_price"] < with_fee["vwap"]


def test_insufficient_depth_is_explicit():
    result = paper_engine.simulate_sell(book(), 1000)

    assert result["complete"] is False
    assert result["fill_fraction"] < 1


def test_invalid_book_level_fails_closed():
    bad = book()
    bad["asks"] = [{"price": "nan", "size": "100"}]

    with pytest.raises(ValueError, match="BOOK_LEVEL_INVALID"):
        paper_engine.simulate_buy(bad, 25)


def test_fee_info_uses_market_metadata_and_token_binding():
    info = paper_engine.parse_fee_info(
        {
            "t": [{"t": "token-a"}, {"t": "token-b"}],
            "fd": {"r": 0.05, "e": 1},
        },
        "token-a",
    )

    assert info == {"fee_rate": 0.05, "fee_exponent": 1.0}

    with pytest.raises(ValueError, match="MARKET_TOKEN_MISMATCH"):
        paper_engine.parse_fee_info(
            {"t": [{"t": "token-b"}], "fd": {"r": 0.05, "e": 1}},
            "token-a",
        )


def test_missing_fee_data_is_fee_free_like_official_client_default():
    info = paper_engine.parse_fee_info(
        {"t": [{"t": "token-a"}], "fd": None},
        "token-a",
    )

    assert info == {"fee_rate": 0.0, "fee_exponent": 0.0}


def test_settlement_math_supports_binary_and_fractional_payouts():
    yes = paper_engine.calculate_settlement(40, 25, 1, 1)
    assert yes["payout_per_token"] == 1.0
    assert yes["settlement_value_usd"] == 40.0
    assert yes["realized_pnl_usd"] == 15.0

    lose = paper_engine.calculate_settlement(40, 25, 0, 1)
    assert lose["settlement_value_usd"] == 0.0
    assert lose["realized_pnl_usd"] == -25.0

    half = paper_engine.calculate_settlement(40, 25, 1, 2)
    assert half["payout_per_token"] == 0.5
    assert half["settlement_value_usd"] == 20.0
    assert half["realized_pnl_usd"] == -5.0


def test_settlement_math_rejects_invalid_payouts():
    with pytest.raises(ValueError, match="PAYOUT_STRUCTURE_INVALID"):
        paper_engine.calculate_settlement(40, 25, 2, 1)

    with pytest.raises(ValueError, match="PAYOUT_STRUCTURE_INVALID"):
        paper_engine.calculate_settlement(40, 25, 0, 0)


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("asset_id", None, "BOOK_ASSET_ID_MISSING"),
    ("asset_id", "", "BOOK_ASSET_ID_MISSING"),
    ("market", None, "BOOK_MARKET_ID_MISSING"),
    ("market", "", "BOOK_MARKET_ID_MISSING"),
    ("asset_id", "wrong", "TOKEN_ID_MISMATCH"),
    ("market", "wrong", "CONDITION_ID_MISMATCH"),
])
def test_paper_book_identity_fails_closed_for_missing_or_mismatched_fields(field, value, expected):
    raw = book()
    raw[field] = value
    ok, reason = paper_engine.validate_book_identity(raw, "token-a", "condition-a")
    assert not ok
    assert reason == expected


@pytest.mark.parametrize(("field", "expected"), [
    ("asset_id", "BOOK_ASSET_ID_MISSING"),
    ("market", "BOOK_MARKET_ID_MISSING"),
])
def test_paper_book_identity_requires_fields_to_exist(field, expected):
    raw = book()
    raw.pop(field)
    assert paper_engine.validate_book_identity(raw, "token-a", "condition-a") == (False, expected)


@pytest.mark.parametrize(("token_id", "condition_id", "expected"), [
    ("", "condition-a", "TOKEN_ID_MISSING"),
    ("token-a", "", "CONDITION_ID_MISSING"),
])
def test_paper_book_identity_requires_focus_binding(token_id, condition_id, expected):
    assert paper_engine.validate_book_identity(book(), token_id, condition_id) == (False, expected)



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
def test_non_string_clob_json_id_fails_paper_binding(field, value, reason):
    raw = book()
    raw[field] = value
    assert paper_engine.validate_book_identity(raw, "token-a", "condition-a") == (False, reason)


def test_numeric_source_and_numeric_clob_identity_never_match():
    raw = book()
    raw["asset_id"] = 12345
    assert paper_engine.validate_book_identity(raw, 12345, "condition-a")[0] is False
    raw = book()
    raw["market"] = 12345
    assert paper_engine.validate_book_identity(raw, "token-a", 12345)[0] is False


def test_legacy_ctf_decimal_token_and_hex_condition_still_match():
    token = "123456789012345678901234567890"
    condition = "0x" + "ab" * 32
    raw = book()
    raw["asset_id"] = token
    raw["market"] = "0x" + "AB" * 32
    assert paper_engine.validate_book_identity(raw, token, condition) == (True, "OK")
