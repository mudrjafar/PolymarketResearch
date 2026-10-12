"""Pure identity helpers for Polymarket Exchange V3 position IDs.

This module performs no network or file I/O. It decodes the documented
position-ID bit layout and deliberately distinguishes supported binary/NegRisk
positions from research-only combinatorial positions.
"""

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
            and condition_index == 0
            and outcome_index in (0, 1)
            and resolution_chain == 0
        )
        classification = "SUPPORTED" if valid else "FAIL_CLOSED"
    elif module_id == MODULE_NEGRISK:
        valid = (
            arity >= 2
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
    """Match canonical bytes31 or its bytes32 right-padded representation."""
    raw = str(value or "").strip().lower()
    expected = str(expected or "").strip().lower()

    if len(expected) != 64 or not expected.startswith("0x"):
        raise V3IdentityError("expected V3 condition_id is invalid")
    try:
        int(expected[2:], 16)
    except ValueError as exc:
        raise V3IdentityError("expected V3 condition_id is invalid") from exc

    if raw == expected:
        return True

    if len(raw) == 66 and raw.startswith("0x") and raw[2:64] == expected[2:] and raw[64:] == "00":
        return True

    return False


def expected_outcome_label(outcome_index):
    if outcome_index == 0:
        return "yes"
    if outcome_index == 1:
        return "no"
    raise V3IdentityError("V3 outcome index is unsupported")
