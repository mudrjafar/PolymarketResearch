"""Pure execution-protocol identity contract.

Collector market research may include V3 PositionIds. Only a token explicitly
bound to one of the two monitored legacy Polygon CTF exchanges is currently
eligible for the exclusive Execution Focus. No network or runtime side effects.
"""

import re


LEGACY_CTF_EXCHANGES = frozenset({
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
})
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_CTF_TOKEN = re.compile(r"(?:[1-9][0-9]*)\Z")
_CTF_CONDITION = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_MAX_CTF_TOKEN = 1 << 256


def legacy_ctf_identity(row):
    if not isinstance(row, dict):
        return False
    token = row.get("token_id")
    condition = row.get("condition_id")
    address = row.get("exchange_address")
    return (
        isinstance(token, str)
        and _CTF_TOKEN.fullmatch(token) is not None
        and 0 < int(token) < _MAX_CTF_TOKEN
        and isinstance(condition, str)
        and _CTF_CONDITION.fullmatch(condition) is not None
        and row.get("asset_type") == "CTF_TOKEN"
        and row.get("protocol_version") == "LEGACY_CTF"
        and row.get("asset_id") == token
        and row.get("ctf_token_id") == token
        and row.get("position_id") is None
        and isinstance(address, str)
        and _ADDRESS.fullmatch(address) is not None
        and address.lower() in LEGACY_CTF_EXCHANGES
    )
