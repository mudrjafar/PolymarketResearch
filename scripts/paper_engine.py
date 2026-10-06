"""Pure Paper execution math.

Simulates taker-style BUY entry from asks and SELL exit into bids.
No network, file I/O, Telegram, wallet, signing, or order submission.
"""

import math
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from machine_common import finite_number


FEE_DECIMALS = 5


def _number(value):
    return finite_number(value, None)


def _parse_levels(raw, *, reverse):
    if not isinstance(raw, list):
        return None, "BOOK_LEVELS_INVALID"

    levels = []
    for row in raw:
        if not isinstance(row, dict):
            return None, "BOOK_LEVEL_INVALID"

        price = _number(row.get("price"))
        size = _number(row.get("size"))
        if price is None or size is None:
            return None, "BOOK_LEVEL_INVALID"
        if not 0 < price < 1 or size <= 0:
            return None, "BOOK_LEVEL_INVALID"

        levels.append({"price": float(price), "size": float(size)})

    levels.sort(key=lambda row: row["price"], reverse=reverse)
    return levels, None


def validate_book_identity(book, token_id, condition_id):
    if not isinstance(book, dict):
        return False, "BOOK_PAYLOAD_INVALID"

    token_id = str(token_id or "").strip()
    condition_id = str(condition_id or "").strip()
    asset_id = str(book.get("asset_id") or "").strip()
    market = str(book.get("market") or "").strip()

    if not token_id:
        return False, "TOKEN_ID_MISSING"
    if asset_id and asset_id != token_id:
        return False, "TOKEN_ID_MISMATCH"
    if market and condition_id and market.lower() != condition_id.lower():
        return False, "CONDITION_ID_MISMATCH"

    return True, "OK"


def parse_fee_info(market_info, token_id):
    """Return the per-market V2 fee curve from CLOB market metadata."""
    if not isinstance(market_info, dict):
        raise ValueError("MARKET_INFO_INVALID")

    token_id = str(token_id or "").strip()
    tokens = market_info.get("t")
    if not isinstance(tokens, list):
        raise ValueError("MARKET_TOKENS_INVALID")

    seen = {
        str(row.get("t") or "").strip()
        for row in tokens
        if isinstance(row, dict)
    }
    if token_id not in seen:
        raise ValueError("MARKET_TOKEN_MISMATCH")

    fee_data = market_info.get("fd")
    if fee_data is None:
        return {"fee_rate": 0.0, "fee_exponent": 0.0}
    if not isinstance(fee_data, dict):
        raise ValueError("FEE_INFO_INVALID")

    rate = _number(fee_data.get("r", 0.0))
    exponent = _number(fee_data.get("e", 0.0))
    if rate is None or exponent is None or rate < 0 or exponent < 0:
        raise ValueError("FEE_INFO_INVALID")

    return {
        "fee_rate": float(rate),
        "fee_exponent": float(exponent),
    }


def platform_fee_usd(tokens, price, fee_rate, fee_exponent):
    tokens = _number(tokens)
    price = _number(price)
    fee_rate = _number(fee_rate)
    fee_exponent = _number(fee_exponent)

    if None in (tokens, price, fee_rate, fee_exponent):
        raise ValueError("FEE_INPUT_INVALID")
    if tokens < 0 or not 0 < price < 1 or fee_rate < 0 or fee_exponent < 0:
        raise ValueError("FEE_INPUT_INVALID")
    if tokens == 0 or fee_rate == 0:
        return 0.0

    curve = fee_rate * (price * (1.0 - price)) ** fee_exponent
    fee = tokens * curve
    if not math.isfinite(fee) or fee < 0:
        raise ValueError("FEE_RESULT_INVALID")
    return fee


def simulate_buy(book, notional_usd, *, fee_rate=0.0, fee_exponent=0.0):
    """Consume asks by USD notional and return net tokens after BUY taker fees."""
    amount = _number(notional_usd)
    if amount is None or amount <= 0:
        raise ValueError("NOTIONAL_INVALID")

    asks, error = _parse_levels(book.get("asks") if isinstance(book, dict) else None, reverse=False)
    if error:
        raise ValueError(error)
    if not asks:
        raise ValueError("BOOK_ASKS_EMPTY")

    remaining = float(amount)
    spent = 0.0
    gross_tokens = 0.0
    net_tokens = 0.0
    fee_usd_raw = 0.0
    worst_price = None

    for level in asks:
        if remaining <= 1e-12:
            break

        price = level["price"]
        level_cost = price * level["size"]
        take_cost = min(remaining, level_cost)
        take_tokens = take_cost / price
        fee_usd = platform_fee_usd(take_tokens, price, fee_rate, fee_exponent)
        fee_tokens = fee_usd / price

        spent += take_cost
        gross_tokens += take_tokens
        net_tokens += max(0.0, take_tokens - fee_tokens)
        fee_usd_raw += fee_usd
        remaining -= take_cost
        worst_price = price

    complete = remaining <= 1e-9
    vwap = spent / gross_tokens if gross_tokens > 0 else None
    effective_price = spent / net_tokens if net_tokens > 0 else None
    best_ask = asks[0]["price"]

    slippage_bps = None
    if vwap is not None and best_ask > 0:
        slippage_bps = (vwap / best_ask - 1.0) * 10000.0

    return {
        "complete": complete,
        "requested_usd": round(float(amount), 8),
        "spent_usd": round(spent, 8),
        "fill_fraction": round(min(1.0, spent / amount), 8),
        "gross_tokens": round(gross_tokens, 8),
        "fee_usd": round(fee_usd_raw, FEE_DECIMALS),
        "net_tokens": round(net_tokens, 8),
        "vwap": round(vwap, 8) if vwap is not None else None,
        "effective_entry_price": (
            round(effective_price, 8) if effective_price is not None else None
        ),
        "best_ask": round(best_ask, 8),
        "worst_price": round(worst_price, 8) if worst_price is not None else None,
        "slippage_bps": (
            round(slippage_bps, 4) if slippage_bps is not None else None
        ),
    }


def simulate_sell(book, token_amount, *, fee_rate=0.0, fee_exponent=0.0):
    """Consume bids by token amount and return net proceeds after SELL taker fees."""
    amount = _number(token_amount)
    if amount is None or amount <= 0:
        raise ValueError("TOKEN_AMOUNT_INVALID")

    bids, error = _parse_levels(book.get("bids") if isinstance(book, dict) else None, reverse=True)
    if error:
        raise ValueError(error)
    if not bids:
        raise ValueError("BOOK_BIDS_EMPTY")

    remaining = float(amount)
    sold = 0.0
    gross_proceeds = 0.0
    fee_usd_raw = 0.0
    worst_price = None

    for level in bids:
        if remaining <= 1e-12:
            break

        price = level["price"]
        take_tokens = min(remaining, level["size"])
        proceeds = take_tokens * price
        fee_usd = platform_fee_usd(take_tokens, price, fee_rate, fee_exponent)

        sold += take_tokens
        gross_proceeds += proceeds
        fee_usd_raw += fee_usd
        remaining -= take_tokens
        worst_price = price

    complete = remaining <= 1e-9
    vwap = gross_proceeds / sold if sold > 0 else None
    net_proceeds = max(0.0, gross_proceeds - fee_usd_raw)
    effective_price = net_proceeds / sold if sold > 0 else None
    best_bid = bids[0]["price"]

    slippage_bps = None
    if vwap is not None and best_bid > 0:
        slippage_bps = (1.0 - vwap / best_bid) * 10000.0

    return {
        "complete": complete,
        "requested_tokens": round(float(amount), 8),
        "sold_tokens": round(sold, 8),
        "fill_fraction": round(min(1.0, sold / amount), 8),
        "gross_proceeds_usd": round(gross_proceeds, 8),
        "fee_usd": round(fee_usd_raw, FEE_DECIMALS),
        "net_proceeds_usd": round(net_proceeds, 8),
        "vwap": round(vwap, 8) if vwap is not None else None,
        "effective_exit_price": (
            round(effective_price, 8) if effective_price is not None else None
        ),
        "best_bid": round(best_bid, 8),
        "worst_price": round(worst_price, 8) if worst_price is not None else None,
        "slippage_bps": (
            round(slippage_bps, 4) if slippage_bps is not None else None
        ),
    }


def self_test():
    book = {
        "bids": [
            {"price": "0.49", "size": "50"},
            {"price": "0.48", "size": "100"},
        ],
        "asks": [
            {"price": "0.50", "size": "50"},
            {"price": "0.51", "size": "100"},
        ],
    }

    buy = simulate_buy(book, 50)
    assert buy["complete"] is True
    assert buy["gross_tokens"] > 99
    assert buy["net_tokens"] == buy["gross_tokens"]

    sell = simulate_sell(book, buy["net_tokens"])
    assert sell["complete"] is True
    assert sell["net_proceeds_usd"] < 50

    fee_buy = simulate_buy(book, 25, fee_rate=0.05, fee_exponent=1)
    assert fee_buy["fee_usd"] > 0
    assert fee_buy["net_tokens"] < fee_buy["gross_tokens"]

    print("PAPER ENGINE SELF-TEST OK")


if __name__ == "__main__":
    self_test()
