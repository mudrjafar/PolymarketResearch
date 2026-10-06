"""Pure read-only order-book execution analysis.

No network, no file I/O, no order submission, no wallet access.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from machine_common import finite_number

TARGET_NOTIONALS_USD = (25.0, 50.0, 100.0)


def _number(value):
    return finite_number(value, None)


def _parse_levels(raw, *, reverse):
    if not isinstance(raw, list):
        return None, "BOOK_LEVELS_INVALID"

    levels = []
    for level in raw:
        if not isinstance(level, dict):
            return None, "BOOK_LEVEL_INVALID"

        price = _number(level.get("price"))
        size = _number(level.get("size"))

        if price is None or size is None:
            return None, "BOOK_LEVEL_INVALID"
        if not 0 < price < 1 or size <= 0:
            return None, "BOOK_LEVEL_INVALID"

        levels.append({"price": float(price), "size": float(size)})

    levels.sort(key=lambda row: row["price"], reverse=reverse)
    return levels, None


def _buy_quote(asks, notional_usd):
    remaining = float(notional_usd)
    spent = 0.0
    tokens = 0.0
    worst_price = None

    for level in asks:
        if remaining <= 1e-12:
            break

        price = level["price"]
        available_tokens = level["size"]
        level_cost = price * available_tokens
        take_cost = min(remaining, level_cost)
        take_tokens = take_cost / price

        spent += take_cost
        tokens += take_tokens
        remaining -= take_cost
        worst_price = price

    complete = remaining <= 1e-9
    vwap = (spent / tokens) if tokens > 0 else None
    best_ask = asks[0]["price"] if asks else None

    slippage = None
    slippage_bps = None
    if vwap is not None and best_ask is not None and best_ask > 0:
        slippage = vwap - best_ask
        slippage_bps = (vwap / best_ask - 1.0) * 10000.0

    return {
        "notional_usd": float(notional_usd),
        "complete": complete,
        "filled_usd": round(spent, 8),
        "fill_fraction": round(min(1.0, spent / notional_usd), 8),
        "outcome_tokens": round(tokens, 8),
        "vwap": round(vwap, 8) if vwap is not None else None,
        "worst_price": round(worst_price, 8) if worst_price is not None else None,
        "slippage": round(slippage, 8) if slippage is not None else None,
        "slippage_bps": round(slippage_bps, 4) if slippage_bps is not None else None,
    }


def analyze_book(book, focus, target_notionals=TARGET_NOTIONALS_USD):
    """Analyze BUY execution quality for the READY outcome token."""
    reasons = []

    if not isinstance(book, dict):
        return {
            "book_ok": False,
            "reason_codes": ["BOOK_PAYLOAD_INVALID"],
            "quotes": [],
        }

    if not isinstance(focus, dict):
        return {
            "book_ok": False,
            "reason_codes": ["FOCUS_PAYLOAD_INVALID"],
            "quotes": [],
        }

    token_id = str(focus.get("token_id") or "").strip()
    condition_id = str(focus.get("condition_id") or "").strip()

    asset_id = str(book.get("asset_id") or "").strip()
    market = str(book.get("market") or "").strip()

    if not token_id:
        reasons.append("TOKEN_ID_MISSING")
    if asset_id and token_id and asset_id != token_id:
        reasons.append("TOKEN_ID_MISMATCH")
    if market and condition_id and market.lower() != condition_id.lower():
        reasons.append("CONDITION_ID_MISMATCH")

    bids, bid_error = _parse_levels(book.get("bids"), reverse=True)
    asks, ask_error = _parse_levels(book.get("asks"), reverse=False)

    if bid_error:
        reasons.append(bid_error)
        bids = []
    if ask_error:
        reasons.append(ask_error)
        asks = []

    if not bids:
        reasons.append("BOOK_BIDS_EMPTY")
    if not asks:
        reasons.append("BOOK_ASKS_EMPTY")

    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None

    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        reasons.append("BOOK_CROSSED")

    midpoint = None
    spread = None
    spread_bps_mid = None
    if best_bid is not None and best_ask is not None:
        midpoint = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        if midpoint > 0:
            spread_bps_mid = spread / midpoint * 10000.0

    quotes = [_buy_quote(asks, amount) for amount in target_notionals] if asks else []

    for quote in quotes:
        if not quote["complete"]:
            reasons.append(f"INSUFFICIENT_DEPTH_{int(quote['notional_usd'])}")

    focus_price = _number(focus.get("price"))
    move_from_focus = None
    move_from_focus_bps = None
    if focus_price is not None and focus_price > 0 and best_ask is not None:
        move_from_focus = best_ask - focus_price
        move_from_focus_bps = (best_ask / focus_price - 1.0) * 10000.0

    ask_depth_tokens = sum(level["size"] for level in asks)
    ask_depth_usd = sum(level["price"] * level["size"] for level in asks)
    bid_depth_tokens = sum(level["size"] for level in bids)
    bid_depth_usd = sum(level["price"] * level["size"] for level in bids)

    reason_codes = list(dict.fromkeys(reasons))

    return {
        "book_ok": not reason_codes,
        "reason_codes": reason_codes,
        "token_id": token_id,
        "condition_id": condition_id,
        "book_hash": book.get("hash"),
        "book_timestamp": book.get("timestamp"),
        "best_bid": round(best_bid, 8) if best_bid is not None else None,
        "best_ask": round(best_ask, 8) if best_ask is not None else None,
        "midpoint": round(midpoint, 8) if midpoint is not None else None,
        "spread": round(spread, 8) if spread is not None else None,
        "spread_bps_mid": round(spread_bps_mid, 4) if spread_bps_mid is not None else None,
        "ask_depth_tokens": round(ask_depth_tokens, 8),
        "ask_depth_usd": round(ask_depth_usd, 8),
        "bid_depth_tokens": round(bid_depth_tokens, 8),
        "bid_depth_usd": round(bid_depth_usd, 8),
        "focus_price": float(focus_price) if focus_price is not None else None,
        "best_ask_move_from_focus": (
            round(move_from_focus, 8) if move_from_focus is not None else None
        ),
        "best_ask_move_from_focus_bps": (
            round(move_from_focus_bps, 4) if move_from_focus_bps is not None else None
        ),
        "min_order_size": book.get("min_order_size"),
        "tick_size": book.get("tick_size"),
        "neg_risk": book.get("neg_risk"),
        "last_trade_price": book.get("last_trade_price"),
        "quotes": quotes,
    }


def self_test():
    focus = {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "price": 0.50,
    }
    book = {
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

    result = analyze_book(book, focus)
    assert result["book_ok"] is True
    assert result["best_bid"] == 0.49
    assert result["best_ask"] == 0.50
    assert len(result["quotes"]) == 3
    assert all(quote["complete"] for quote in result["quotes"])
    assert result["quotes"][0]["vwap"] == 0.50
    assert result["quotes"][2]["vwap"] > 0.50
    print("BOOK ENGINE SELF-TEST OK")


if __name__ == "__main__":
    self_test()
