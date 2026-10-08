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


@pytest.mark.parametrize("bad_numerator,bad_denominator", [
    (1.0, 2),
    ("1", 2),
    (1, 2.0),
    (1, "2"),
    (True, 2),
    (1, False),
])
def test_settlement_payout_structure_requires_real_integers(
    bad_numerator,
    bad_denominator,
):
    with pytest.raises(ValueError, match="PAYOUT_STRUCTURE_INVALID"):
        paper_engine.calculate_settlement(
            10.0,
            5.0,
            bad_numerator,
            bad_denominator,
        )
