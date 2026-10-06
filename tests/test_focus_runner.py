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
        "token_id": token_id,
        "condition_id": f"condition-{token_id}",
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
                candidate("B", 100, 1, NOW, tx_byte="b"),
                candidate("A", 101, 1, NOW, tx_byte="a"),
            ],
        ),
    )

    state, events, status = runner.run_once(now=NOW)

    assert status == "OK"
    assert state["focus"]["token_id"] == "B"
    assert "score" not in state["focus"]
    assert "score_at_lock" not in state["focus"]
    assert events[-1]["type"] == "LOCKED"

    stored = read_json(paths["state"])
    assert stored["last_source_generation_id"] == "X"
    assert stored["engine_state"]["focus"]["token_id"] == "B"

    view = read_json(paths["view"])
    assert view["source_generation_id"] == "X"
    assert view["state"] == "LOCKED"
    assert view["token_id"] == "B"

    event = json.loads(paths["events"].read_text(encoding="utf-8").splitlines()[-1])
    assert event["source_generation_id"] == "X"
    assert event["type"] == "LOCKED"


def test_missing_risk_is_input_failure_not_valid_empty_generation(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("A", 100, 1, NOW)]),
    )
    runner.run_once(now=NOW)
    before = paths["state"].read_bytes()

    paths["risk"].unlink()
    state, events, status = runner.run_once(now=NOW + timedelta(seconds=30))

    assert status == "MISSING"
    assert events == []
    assert state["focus"]["token_id"] == "A"
    assert paths["state"].read_bytes() == before
    assert read_json(paths["view"])["input_status"] == "MISSING"


def test_stale_risk_does_not_advance_focus(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["risk"],
        risk_payload("X", [candidate("A", 100, 1, NOW)]),
    )
    runner.run_once(now=NOW)
    before = paths["state"].read_bytes()

    stale_at = NOW - timedelta(minutes=10)
    save_json_atomic(
        paths["risk"],
        risk_payload(
            "Y",
            [candidate("A", 101, 1, stale_at)],
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
        risk_payload("X", [candidate("A", 100, 1, NOW)]),
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
        risk_payload("X", [candidate("A", 100, 1, NOW)]),
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
