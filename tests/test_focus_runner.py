import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from machine_common import save_json_atomic
from scripts import focus_runner as runner


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def configure_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "risk": data / "risk_assessment.json",
        "state": data / "focus_state.json",
        "view": data / "focused_market.json",
        "events": data / "focus_events.jsonl",
    }
    monkeypatch.setattr(runner, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(runner, "RISK_FILE", paths["risk"], raising=False)
    monkeypatch.setattr(runner, "STATE_FILE", paths["state"], raising=False)
    monkeypatch.setattr(runner, "FOCUS_FILE", paths["view"], raising=False)
    monkeypatch.setattr(runner, "EVENTS_FILE", paths["events"], raising=False)
    return paths


def candidate(token_id, block, log_index, at, *, tx_byte="a", risk_ok=True, codes=None):
    return {
        "asset_type": "CTF_TOKEN",
        "protocol_version": "LEGACY_CTF",
        "exchange_address": "0xE111180000d2663C0091e4f400237545B87B996B",
        "asset_id": token_id,
        "ctf_token_id": token_id,
        "position_id": None,
        "execution_protocol_eligible": True,
        "token_id": token_id,
        "condition_id": "0x" + "ab" * 32,
        "outcome": "Yes",
        "question": f"Market {token_id}",
        "direction": "BUY",
        "price": 0.42,
        "risk_ok": risk_ok,
        "decision": "PASS" if risk_ok else "BLOCK",
        "reason_codes": list(codes or []),
        "evidence_id": "0x" + tx_byte * 64 + f":{log_index}",
        "evidence_cursor": [block, log_index],
        "evidence_at": at.isoformat(),
    }


def risk_payload(generation_id, results, generated_at=NOW):
    return {
        "source_generation_id": generation_id,
        "source_generated_at": generated_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "results": results,
    }


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_runner_preserves_authoritative_risk_order_without_score(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload(
            "X",
            [
                candidate("102", 100, 1, NOW, tx_byte="b"),
                candidate("101", 101, 1, NOW, tx_byte="a"),
            ],
        ),
    )

    state, events, status = runner.run_once(now=NOW)

    assert status == "OK"
    assert state["focus"]["token_id"] == "102"
    assert "score" not in state["focus"]
    assert "score_at_lock" not in state["focus"]
    assert events[-1]["type"] == "LOCKED"

    stored = read_json(paths["state"])
    assert stored["last_source_generation_id"] == "X"
    assert stored["engine_state"]["focus"]["token_id"] == "102"

    view = read_json(paths["view"])
    assert view["source_generation_id"] == "X"
    assert view["state"] == "LOCKED"
    assert view["token_id"] == "102"

    event = json.loads(paths["events"].read_text(encoding="utf-8").splitlines()[-1])
    assert event["source_generation_id"] == "X"
    assert event["type"] == "LOCKED"


def test_missing_risk_is_input_failure_not_valid_empty_generation(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("101", 100, 1, NOW)]),
    )
    runner.run_once(now=NOW)
    before = paths["state"].read_bytes()

    paths["risk"].unlink()
    state, events, status = runner.run_once(now=NOW + timedelta(seconds=30))

    assert status == "MISSING"
    assert events == []
    assert state["focus"]["token_id"] == "101"
    assert paths["state"].read_bytes() == before
    assert read_json(paths["view"])["input_status"] == "MISSING"


def test_stale_risk_does_not_advance_focus(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("101", 100, 1, NOW)]),
    )
    runner.run_once(now=NOW)
    before = paths["state"].read_bytes()

    stale_at = NOW - timedelta(minutes=10)
    save_json_atomic(
        paths["risk"],
        risk_payload(
            "Y",
            [candidate("101", 101, 1, stale_at)],
            generated_at=stale_at,
        ),
    )

    state, events, status = runner.run_once(now=NOW)

    assert status == "STALE"
    assert events == []
    assert state["focus"]["last_evidence_cursor"] == [100, 1]
    assert paths["state"].read_bytes() == before


def test_valid_empty_generation_is_distinct_from_missing_input(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("101", 100, 1, NOW)]),
    )
    runner.run_once(now=NOW)

    later = NOW + timedelta(seconds=30)
    save_json_atomic(paths["risk"], risk_payload("Y", [], generated_at=later))

    state, events, status = runner.run_once(now=later)

    assert status == "OK"
    assert events == []
    assert state["focus"] is not None
    assert state["focus"]["absent_since"] == later.isoformat()
    assert read_json(paths["state"])["last_source_generation_id"] == "Y"


def test_corrupt_persisted_focus_state_fails_closed(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    paths["state"].write_text("{not-json", encoding="utf-8")
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("101", 100, 1, NOW)]),
    )

    with pytest.raises(runner.FocusStateError):
        runner.run_once(now=NOW)

    assert paths["state"].read_text(encoding="utf-8") == "{not-json"
    assert not paths["events"].exists()


def test_main_runtime_keeps_risk_and_focus_as_separate_ordered_workers():
    import run_machine

    names = [name for name, _ in run_machine.SERVICES]
    assert names.index("diamond") < names.index("risk") < names.index("focus") < names.index("telegram")

    commands = dict(run_machine.SERVICES)
    assert any("risk_worker.py" in str(part) for part in commands["risk"])
    assert any("focus_runner.py" in str(part) for part in commands["focus"])



def test_runner_v3_only_never_locks(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    row = candidate("107", 100, 1, NOW)
    row.update(asset_type="POSITION", protocol_version="V2",
               ctf_token_id=None, position_id="107",
               execution_protocol_eligible=False)
    save_json_atomic(paths["risk"], risk_payload("V3", [row]))
    state, events, status = runner.run_once(now=NOW)
    assert status == "OK"
    assert state["focus"] is None
    assert events == []


def test_runner_v3_ranked_before_legacy_locks_legacy(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    row = candidate("107", 100, 1, NOW)
    row.update(asset_type="POSITION", protocol_version="V2",
               ctf_token_id=None, position_id="107",
               execution_protocol_eligible=False)
    save_json_atomic(paths["risk"], risk_payload("RANKED", [
        row, candidate("105", 101, 1, NOW)
    ]))
    state, events, status = runner.run_once(now=NOW)
    assert status == "OK"
    assert state["focus"]["token_id"] == "105"
    assert events[-1]["type"] == "LOCKED"


def test_runner_releases_old_ambiguous_lock_even_without_risk(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["risk"], risk_payload(
        "X", [candidate("106", 100, 1, NOW)]
    ))
    runner.run_once(now=NOW)
    snapshot = read_json(paths["state"])
    active = snapshot["engine_state"]["focus"]
    for field in ("asset_type", "protocol_version", "ctf_token_id",
                  "position_id", "execution_protocol_eligible"):
        active.pop(field, None)
    save_json_atomic(paths["state"], snapshot)
    paths["risk"].unlink()

    state, events, status = runner.run_once(now=NOW + timedelta(seconds=30))
    assert status == "MISSING"
    assert state["focus"] is None
    assert [e["type"] for e in events] == ["INVALIDATED"]
    assert read_json(paths["view"])["input_status"] == "MISSING"



@pytest.mark.parametrize("failure_mode", ["MISSING", "STALE", "CORRUPT"])
def test_old_v3_focus_invalidates_with_original_generation_and_learning_once(
    monkeypatch, tmp_path, failure_mode
):
    paths = configure_paths(monkeypatch, tmp_path)
    enqueue = []
    monkeypatch.setattr(runner, "_LEARNING_INIT_ERROR", None, raising=False)
    monkeypatch.setattr(
        runner, "enqueue_focus_event",
        lambda event, *, data_dir, strategy_versions: enqueue.append(
            (dict(event), dict(strategy_versions))
        ),
    )
    original_risk = risk_payload("GEN-ORIGINAL", [candidate("101", 100, 1, NOW)])
    original_risk["strategy_versions"] = {"git_commit_sha": "a" * 40}
    save_json_atomic(paths["risk"], original_risk)
    locked, events, status = runner.run_once(now=NOW)
    assert locked["focus"]["token_id"] == "101"
    assert status == "OK"
    assert len(enqueue) == 1
    assert read_json(paths["state"])["last_source_generation_id"] == "GEN-ORIGINAL"

    wrapper = read_json(paths["state"])
    wrapper["engine_state"]["focus"].update(
        asset_type="POSITION", protocol_version="V2",
        position_id="101", ctf_token_id=None,
        execution_protocol_eligible=False,
    )
    save_json_atomic(paths["state"], wrapper)
    later = NOW + timedelta(seconds=30)
    if failure_mode == "MISSING":
        paths["risk"].unlink()
    elif failure_mode == "CORRUPT":
        paths["risk"].write_text("{bad-json", encoding="utf-8")
    else:
        stale = risk_payload(
            "GEN-CHANGED", [candidate("102", 101, 1, NOW)],
            generated_at=NOW - timedelta(minutes=10),
        )
        stale["strategy_versions"] = {"git_commit_sha": "b" * 40}
        save_json_atomic(paths["risk"], stale)

    new_state, events, status = runner.run_once(now=later)
    assert status == failure_mode
    assert new_state["focus"] is None
    assert new_state["challenger"] is None
    assert [e["type"] for e in events] == ["INVALIDATED"]
    assert read_json(paths["state"])["last_source_generation_id"] == "GEN-ORIGINAL"
    event = json.loads(paths["events"].read_text(encoding="utf-8").splitlines()[-1])
    assert event["source_generation_id"] == "GEN-ORIGINAL"
    assert len(enqueue) == 2
    assert enqueue[-1][0]["source_generation_id"] == "GEN-ORIGINAL"
    assert enqueue[-1][1]["git_commit_sha"] == "a" * 40

    # Restart against the still missing/stale/corrupt Risk input.
    new_state, second_events, status = runner.run_once(now=later + timedelta(seconds=3))
    assert status == failure_mode
    assert new_state["focus"] is None
    assert second_events == []
    assert len(enqueue) == 2


def test_pre_metadata_focus_releases_without_inventing_learning_provenance(
    monkeypatch, tmp_path
):
    paths = configure_paths(monkeypatch, tmp_path)
    emitted = []
    monkeypatch.setattr(runner, "_LEARNING_INIT_ERROR", None, raising=False)
    monkeypatch.setattr(
        runner, "enqueue_focus_event",
        lambda event, *, data_dir, strategy_versions: emitted.append(event),
    )
    save_json_atomic(paths["risk"], risk_payload(
        "LEGACY-GEN", [candidate("101", 100, 1, NOW)]
    ))
    runner.run_once(now=NOW)
    wrapper = read_json(paths["state"])
    for key in ("last_strategy_versions", "last_source_generated_at"):
        wrapper.pop(key, None)
    wrapper["engine_state"]["focus"].pop("asset_type")
    save_json_atomic(paths["state"], wrapper)
    paths["risk"].unlink()
    state, events, status = runner.run_once(now=NOW + timedelta(seconds=10))
    assert state["focus"] is None
    assert events[0]["learning_projection_status"] == "UNAVAILABLE_PROVENANCE"
    assert read_json(paths["state"])["last_source_generation_id"] == "LEGACY-GEN"
    assert len(emitted) == 0
