"""Combined PR35/36/38 offline fail-closed contract probes."""

import json
from datetime import datetime, timezone

import pytest

from machine_common import save_json_atomic
from scripts import paper_worker
from tests.test_paper_worker import configure, write_gate, write_open_request


NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("identity_override", [
    {"asset_type": "POSITION", "protocol_version": "V2", "position_id": "123456789012345678901234567890", "ctf_token_id": None},
    {"asset_type": None},
    {"protocol_version": None},
    {"exchange_address": "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"},
    {"ctf_token_id": "999"},
    {"execution_protocol_eligible": False},
    {"condition_id": "0x" + "cd" * 32, "asset_type": "POSITION", "protocol_version": "V2"},
])
def test_paper_open_blocks_untrusted_protocol_before_network(
    monkeypatch, tmp_path, identity_override
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    focus = json.loads(paths["focus"].read_text(encoding="utf-8"))
    focus["focus"].update(identity_override)
    save_json_atomic(paths["focus"], focus)

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected identity cannot reach network or settlement")

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=forbidden,
        market_info_loader=forbidden,
        identity_loader=forbidden,
        settlement_checker=forbidden,
    )
    assert state["positions"] == []
    assert len(events) == 1
    assert events[0]["type"] == "REJECTED"


@pytest.mark.parametrize("bad", [123, True, [], {"a": "b"}])
def test_internal_gate_never_accepts_numeric_or_json_container_identity(
    monkeypatch, tmp_path, bad
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    for source in ("focus", "book"):
        content = json.loads(paths[source].read_text(encoding="utf-8"))
        if source == "focus":
            content["focus"]["token_id"] = bad
        else:
            content["token_id"] = bad
        save_json_atomic(paths[source], content)
    assert paper_worker.read_execution_gate(now=NOW)[0] != "READY_BOOK_PASS"
