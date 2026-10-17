"""Pure identity helpers for Polymarket Exchange V3 position IDs.

This module performs no network or file I/O. It decodes the documented
position-ID bit layout and deliberately distinguishes supported binary/NegRisk
positions from research-only combinatorial positions.
"""

import json

V3_EXCHANGE_ADDRESS = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"

MODULE_BINARY = 1
MODULE_NEGRISK = 2
MODULE_COMBINATORIAL = 3

MODULE_NAMES = {
    MODULE_BINARY: "BINARY",
    MODULE_NEGRISK: "NEG_RISK",
    MODULE_COMBINATORIAL: "COMBINATORIAL",
}

SUPPORTED_MODULES = frozenset({MODULE_BINARY, MODULE_NEGRISK})
ACTIVE_GAMMA_PAGE_CAP = 100


class V3IdentityError(ValueError):
    pass


def canonical_decimal_id(value, label="position_id"):
    if isinstance(value, bool):
        raise V3IdentityError(f"{label} is invalid")
    raw = str(value or "").strip()
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise V3IdentityError(f"{label} is invalid")
    number = int(raw, 10)
    if number < 0 or number >= 1 << 256:
        raise V3IdentityError(f"{label} is outside uint256")
    return str(number), number


def decode_position_id(value):
    canonical, number = canonical_decimal_id(value)

    module_id = (number >> 248) & 0xFF
    base_hash = (number >> 120) & ((1 << 128) - 1)
    arity = (number >> 104) & 0xFFFF
    reserved = (number >> 40) & ((1 << 64) - 1)
    resolution_chain = (number >> 24) & 0xFFFF
    condition_index = (number >> 8) & 0xFFFF
    outcome_index = number & 0xFF
    condition_id = f"0x{(number >> 8):062x}"

    if module_id == MODULE_BINARY:
        valid = (
            arity == 0
            and reserved == 0
            and condition_index == 0
            and outcome_index in (0, 1)
            and resolution_chain == 0
        )
        classification = "SUPPORTED" if valid else "FAIL_CLOSED"
    elif module_id == MODULE_NEGRISK:
        valid = (
            arity >= 2
            and reserved == 0
            and condition_index <= arity
            and outcome_index in (0, 1)
            and resolution_chain == 0
        )
        classification = "SUPPORTED" if valid else "FAIL_CLOSED"
    elif module_id == MODULE_COMBINATORIAL:
        classification = "RESEARCH_ONLY"
    else:
        classification = "FAIL_CLOSED"

    return {
        "position_id": canonical,
        "module_id": module_id,
        "module": MODULE_NAMES.get(module_id, "UNKNOWN_MODULE"),
        "base_hash": f"0x{base_hash:032x}",
        "arity": arity,
        "reserved": f"0x{reserved:016x}",
        "resolution_chain": resolution_chain,
        "condition_index": condition_index,
        "outcome_index": outcome_index,
        "condition_id": condition_id,
        "classification": classification,
    }


def condition_matches_position(value, expected):
    """Match bytes31 ConditionIds and canonical ABI bytes32 right-padding.

    The V2 Position IDs spec defines ConditionId as bytes31. At a bytes32
    boundary Solidity's strict decoder requires the discarded low byte to be
    zero, so only a single trailing ``00`` is accepted as the wider form.
    """
    raw = str(value or "").strip().lower()
    expected = str(expected or "").strip().lower()

    def normalize(value, label):
        if not value.startswith("0x") or len(value) not in (64, 66):
            raise V3IdentityError(f"{label} is invalid")
        try:
            int(value[2:], 16)
        except ValueError as exc:
            raise V3IdentityError(f"{label} is invalid") from exc
        if len(value) == 66:
            if value[-2:] != "00":
                raise V3IdentityError(f"{label} is not canonical bytes31 padding")
            return value[:64]
        return value

    expected = normalize(expected, "expected V3 condition_id")
    raw = normalize(raw, "V3 condition_id")

    if raw == expected:
        return True
    return False


def expected_outcome_label(outcome_index):
    if outcome_index == 0:
        return "yes"
    if outcome_index == 1:
        return "no"
    raise V3IdentityError("V3 outcome index is unsupported")


def runtime_position_market_entries(market):
    """Return V3 asset entries accepted by the Collector's runtime rules.

    Keep the full binary/NegRisk pair and legacy CTF precedence checks shared
    between Collector normalization and read-only candidate audits.
    """
    if not isinstance(market, dict):
        return []

    def values(field):
        value = market.get(field)
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return []
            return parsed if isinstance(parsed, list) else []
        return []

    token_ids = values("clobTokenIds")
    position_ids = values("positionIds")
    outcomes = values("outcomes")
    prices = values("outcomePrices")

    if (
        len(token_ids) == 2
        and len(outcomes) == 2
        and [str(value or "").strip().casefold() for value in outcomes] == ["yes", "no"]
        and all(
            str(value or "").strip().isascii()
            and str(value or "").strip().isdecimal()
            for value in token_ids
        )
    ):
        return []
    if len(position_ids) != 2 or len(outcomes) != 2:
        return []

    labels = [str(value or "").strip() for value in outcomes]
    if [value.casefold() for value in labels] != ["yes", "no"]:
        return []

    entries = []
    for index, raw_position in enumerate(position_ids):
        try:
            position = decode_position_id(raw_position)
        except (V3IdentityError, TypeError, ValueError):
            return []
        if position["classification"] != "SUPPORTED":
            return []
        if position["outcome_index"] != index:
            return []
        if labels[index].casefold() != expected_outcome_label(position["outcome_index"]):
            return []
        try:
            condition_match = condition_matches_position(
                market.get("conditionId"), position["condition_id"],
            )
        except V3IdentityError:
            return []
        if not condition_match:
            return []

        entries.append((
            position["position_id"],
            {
                "market_id": market.get("id"),
                "condition_id": position["condition_id"],
                "question": market.get("question"),
                "outcome": labels[index],
                "price": prices[index] if index < len(prices) else None,
                "volume24hr": market.get("volume24hr"),
                "volume": market.get("volume"),
                "end_date": market.get("endDate"),
                "start_date": market.get("startDate"),
                "closed": market.get("closed"),
                "active": market.get("active"),
                "accepting_orders": market.get("acceptingOrders"),
                "asset_type": "POSITION",
                "position_id": position["position_id"],
                "protocol_version": "V2",
                "position_module": position["module"],
                "position_module_id": position["module_id"],
                "position_condition_index": position["condition_index"],
                "position_outcome_index": position["outcome_index"],
                "position_arity": position["arity"],
            },
        ))
    return entries
