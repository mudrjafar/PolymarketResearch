"""Pure offline allow-list tests; no network or orders."""

import pytest

from scripts.protocol_identity import legacy_ctf_identity


TOKEN = "123456789123456789"


def _legacy():
    return {
        "token_id": TOKEN,
        "condition_id": "0x" + "ab" * 32,
        "asset_type": "CTF_TOKEN",
        "protocol_version": "LEGACY_CTF",
        "asset_id": TOKEN,
        "ctf_token_id": TOKEN,
        "position_id": None,
        "exchange_address": "0xE111180000d2663C0091e4f400237545B87B996B",
    }


def test_known_legacy_ctf_exchange_is_eligible():
    assert legacy_ctf_identity(_legacy()) is True
    neg_risk = _legacy()
    neg_risk["exchange_address"] = "0xe2222d279d744050d28e00520010520000310F59"
    assert legacy_ctf_identity(neg_risk) is True


@pytest.mark.parametrize(("field", "value"), [
    ("asset_type", "POSITION"),
    ("asset_type", None),
    ("protocol_version", "V2"),
    ("protocol_version", None),
    ("ctf_token_id", None),
    ("ctf_token_id", 123),
    ("asset_id", 123),
    ("position_id", TOKEN),
    ("token_id", None),
    ("condition_id", ""),
    ("exchange_address", None),
    ("exchange_address", "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"),
    ("exchange_address", "0x+" + "a" * 39),
    ("exchange_address", "0x" + "a" * 39 + " "),
])
def test_non_executable_or_unknown_identity_is_blocked(field, value):
    source = _legacy()
    source[field] = value
    assert legacy_ctf_identity(source) is False
