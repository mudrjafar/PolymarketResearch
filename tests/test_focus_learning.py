import json
from datetime import datetime, timedelta, timezone

from machine_common import save_json_atomic
from scripts import focus_runner, learning_worker
from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_ingest import ingest_focus_queue_file
from scripts.learning_queue import enqueue_focus_event, enqueue_risk_snapshot
from scripts.learning_store import LearningStore


T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
EVIDENCE_A = "0x" + "ab" * 32 + ":1"
EVIDENCE_B = "0x" + "cd" * 32 + ":2"


def versions(sha="a"):
    return {
        "pipeline_version": "pipeline-v3",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": sha * 40,
        "config_fingerprint": f"SRC-{sha}",
    }


def focus_candidate(token, block, log_index, at, *, tx="a", price=0.42):
    return {
        "checked_at": at.isoformat(),
        "market_key": f"condition:condition-{token}|outcome:yes",
        "question": f"Market {token}",
        "condition_id": f"condition-{token}",
        "token_id": token,
        "outcome": "Yes",
        "direction": "BUY",
        "price": price,
        "classification": "DIAMOND",
        "risk_ok": True,
        "decision": "PASS",
        "reason_codes": [],
        "reasons": [],
        "warnings": [],
        "is_diamond": True,
        "cashflow_active": False,
        "signal_quality": 90.0,
        "verification": 90.0,
        "evidence_id": "0x" + tx * 64 + f":{log_index}",
        "evidence_cursor": [block, log_index],
        "evidence_at": at.isoformat(),
    }


def risk_snapshot(generation, rows, at, strategy=None):
    return {
        "source_generation_id": generation,
        "source_generated_at": at.isoformat(),
        "generated_at": at.isoformat(),
        "strategy_versions": strategy or versions(),
        "markets_checked": len(rows),
        "passed": sum(1 for row in rows if row.get("risk_ok") is True),
        "results": rows,
    }


def configure_runner(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "data": data,
        "risk": data / "risk_assessment.json",
        "state": data / "focus_state.json",
        "view": data / "focused_market.json",
        "events": data / "focus_events.jsonl",
    }
    monkeypatch.setattr(focus_runner, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(focus_runner, "RISK_FILE", paths["risk"], raising=False)
    monkeypatch.setattr(focus_runner, "STATE_FILE", paths["state"], raising=False)
    monkeypatch.setattr(focus_runner, "FOCUS_FILE", paths["view"], raising=False)
    monkeypatch.setattr(focus_runner, "EVENTS_FILE", paths["events"], raising=False)
    monkeypatch.setattr(focus_runner, "_LEARNING_INIT_ERROR", None, raising=False)
    return paths


def signal_row(generation, observed_at, price):
    return {
        "observed_at": observed_at.isoformat(),
        "source_generation_id": generation,
        "condition_id": "condition-A",
        "token_id": "A",
        "outcome": "Yes",
        "direction": "BUY",
        "classification": "DIAMOND",
        "diamond": True,
        "signal_price": price,
        "signal_quality": 90.0,
        "verification": 90.0,
        "entry_quality": 80.0,
        "resolution_reliability": 90.0,
    }


def queue_focus(path_root, event, strategy):
    result = enqueue_focus_event(
        event,
        data_dir=path_root,
        strategy_versions=strategy,
    )
    return result, next((path_root / "learning_queue").glob(f"focus_{result['focus_event_id']}.json"))


def test_focus_runner_projects_locked_evidence_without_changing_authority(
    monkeypatch, tmp_path
):
    paths = configure_runner(monkeypatch, tmp_path)
    captured = []

    def capture(event, *, data_dir, strategy_versions):
        captured.append((event, data_dir, strategy_versions))
        return {"status": "QUEUED", "focus_event_id": "F"}

    monkeypatch.setattr(focus_runner, "enqueue_focus_event", capture)

    candidate = focus_candidate("A", 100, 1, T0)
    save_json_atomic(
        paths["risk"],
        risk_snapshot("GEN-LOCK", [candidate], T0),
    )

    state, events, status = focus_runner.run_once(now=T0)

    assert status == "OK"
    assert state["focus"]["status"] == "LOCKED"
    assert events[-1]["type"] == "LOCKED"
    assert len(captured) == 1

    row, data_dir, frozen = captured[0]
    assert data_dir == paths["data"]
    assert frozen == versions()
    assert row["event_type"] == "LOCKED"
    assert row["source_generation_id"] == "GEN-LOCK"
    assert row["source_evidence_id"] == candidate["evidence_id"]
    assert row["source_evidence_cursor"] == [100, 1]
    assert row["source_evidence_at"] == T0.isoformat()
    assert row["locked_at"] == T0.isoformat()
    assert row["price_at_lock"] == 0.42
    assert row["ready_at"] is None
    assert row["challenger_present"] is False


def test_learning_failure_cannot_block_focus(monkeypatch, tmp_path, capsys):
    paths = configure_runner(monkeypatch, tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("learning unavailable")

    monkeypatch.setattr(focus_runner, "enqueue_focus_event", broken)
    save_json_atomic(
        paths["risk"],
        risk_snapshot(
            "GEN-LOCK",
            [focus_candidate("A", 100, 1, T0)],
            T0,
        ),
    )

    state, events, status = focus_runner.run_once(now=T0)

    assert status == "OK"
    assert state["focus"]["status"] == "LOCKED"
    assert events[-1]["type"] == "LOCKED"
    assert "FOCUS_QUEUE_ERROR" in capsys.readouterr().out


def test_lock_lineage_uses_earliest_candidate_since_last_invalidation(tmp_path):
    db = tmp_path / "learning.sqlite3"
    strategy = versions()

    with LearningStore(db) as store:
        version_id = store.register_strategy_version(strategy)
        store.insert_signal_observation(
            "SIG-1",
            version_id,
            signal_row("GEN-1", T0 - timedelta(minutes=2), 0.38),
        )
        store.insert_signal_observation(
            "SIG-2",
            version_id,
            signal_row("GEN-2", T0 - timedelta(minutes=1), 0.40),
        )

        event = {
            "event_type": "LOCKED",
            "event_at": T0.isoformat(),
            "source_generation_id": "GEN-2",
            "source_evidence_id": EVIDENCE_A,
            "source_evidence_cursor": [100, 1],
            "source_evidence_at": T0.isoformat(),
            "condition_id": "condition-A",
            "token_id": "A",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.42,
            "progress": 0,
            "fail_count": 0,
            "locked_at": T0.isoformat(),
            "price_at_lock": 0.42,
            "challenger_present": False,
            "challenger": None,
        }
        _, path = queue_focus(tmp_path, event, strategy)
        result = ingest_focus_queue_file(path, store=store)

        assert result["status"] == "INGESTED"
        row = store.conn.execute(
            "SELECT payload_json FROM focus_events WHERE event_type='LOCKED'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["candidate_first_seen_at"] == (
            T0 - timedelta(minutes=2)
        ).isoformat()
        assert payload["price_at_first_seen"] == 0.38
        assert payload["locked_at"] == T0.isoformat()
        assert payload["price_at_lock"] == 0.42
        assert payload["focus_episode_mixed_version"] is False


def test_ready_lineage_preserves_exact_evidence_and_flags_mixed_version(tmp_path):
    db = tmp_path / "learning.sqlite3"
    strategy_a = versions("a")
    strategy_b = versions("b")

    with LearningStore(db) as store:
        version_a = store.register_strategy_version(strategy_a)
        store.insert_signal_observation(
            "SIG-A",
            version_a,
            signal_row("GEN-A", T0, 0.42),
        )

        lock = {
            "event_type": "LOCKED",
            "event_at": T0.isoformat(),
            "source_generation_id": "GEN-A",
            "source_evidence_id": EVIDENCE_A,
            "source_evidence_cursor": [100, 1],
            "source_evidence_at": T0.isoformat(),
            "condition_id": "condition-A",
            "token_id": "A",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.42,
            "progress": 0,
            "fail_count": 0,
            "locked_at": T0.isoformat(),
            "price_at_lock": 0.42,
            "challenger_present": False,
            "challenger": None,
        }
        _, lock_path = queue_focus(tmp_path, lock, strategy_a)
        ingest_focus_queue_file(lock_path, store=store)

        ready_at = T0 + timedelta(minutes=3)
        ready = {
            "event_type": "READY",
            "event_at": ready_at.isoformat(),
            "source_generation_id": "GEN-B",
            "source_evidence_id": EVIDENCE_B,
            "source_evidence_cursor": [103, 2],
            "source_evidence_at": ready_at.isoformat(),
            "condition_id": "condition-A",
            "token_id": "A",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.50,
            "progress": 3,
            "fail_count": 0,
            "locked_at": T0.isoformat(),
            "price_at_lock": 0.42,
            "ready_at": ready_at.isoformat(),
            "price_at_ready": 0.50,
            "challenger_present": True,
            "challenger": {
                "token_id": "B",
                "condition_id": "condition-B",
                "outcome": "Yes",
                "direction": "BUY",
                "price": 0.44,
            },
        }
        _, ready_path = queue_focus(tmp_path, ready, strategy_b)
        result = ingest_focus_queue_file(ready_path, store=store)

        assert result["event_type"] == "READY"
        row = store.conn.execute(
            "SELECT payload_json FROM focus_events WHERE event_type='READY'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["ready_at"] == ready_at.isoformat()
        assert payload["first_ready_at"] == ready_at.isoformat()
        assert payload["price_at_ready"] == 0.50
        assert payload["wait_confirmations"] == 3
        assert payload["source_evidence_id"] == EVIDENCE_B
        assert payload["source_evidence_cursor"] == [103, 2]
        assert payload["source_evidence_at"] == ready_at.isoformat()
        assert payload["challenger_present"] is True
        assert payload["challenger"]["token_id"] == "B"
        assert payload["focus_episode_mixed_version"] is True


def test_invalidation_keeps_episode_and_first_ready_lineage(tmp_path):
    db = tmp_path / "learning.sqlite3"
    strategy = versions()

    with LearningStore(db) as store:
        version_id = store.register_strategy_version(strategy)
        store.insert_signal_observation(
            "SIG-A",
            version_id,
            signal_row("GEN-A", T0, 0.42),
        )

        lock = {
            "event_type": "LOCKED",
            "event_at": T0.isoformat(),
            "source_generation_id": "GEN-A",
            "source_evidence_id": EVIDENCE_A,
            "source_evidence_cursor": [100, 1],
            "source_evidence_at": T0.isoformat(),
            "condition_id": "condition-A",
            "token_id": "A",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.42,
            "progress": 0,
            "fail_count": 0,
            "locked_at": T0.isoformat(),
            "price_at_lock": 0.42,
            "challenger_present": False,
            "challenger": None,
        }
        _, path = queue_focus(tmp_path, lock, strategy)
        ingest_focus_queue_file(path, store=store)

        ready_at = T0 + timedelta(minutes=3)
        ready = dict(lock)
        ready.update(
            {
                "event_type": "READY",
                "event_at": ready_at.isoformat(),
                "source_generation_id": "GEN-R",
                "source_evidence_id": EVIDENCE_B,
                "source_evidence_cursor": [103, 2],
                "source_evidence_at": ready_at.isoformat(),
                "price": 0.50,
                "progress": 3,
                "ready_at": ready_at.isoformat(),
                "price_at_ready": 0.50,
            }
        )
        _, path = queue_focus(tmp_path, ready, strategy)
        ingest_focus_queue_file(path, store=store)

        invalidated_at = T0 + timedelta(minutes=4)
        invalidated = dict(ready)
        invalidated.update(
            {
                "event_type": "INVALIDATED",
                "event_at": invalidated_at.isoformat(),
                "source_generation_id": "GEN-I",
                "ready_at": None,
                "price_at_ready": None,
                "invalidated_at": invalidated_at.isoformat(),
                "invalidation_reason_codes": ["MARKET_EXPIRED"],
            }
        )
        _, path = queue_focus(tmp_path, invalidated, strategy)
        ingest_focus_queue_file(path, store=store)

        row = store.conn.execute(
            "SELECT payload_json FROM focus_events "
            "WHERE event_type='INVALIDATED'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["invalidated"] is True
        assert payload["invalidated_at"] == invalidated_at.isoformat()
        assert payload["invalidation_reason_codes"] == ["MARKET_EXPIRED"]
        assert payload["first_ready_at"] == ready_at.isoformat()
        assert payload["candidate_first_seen_at"] == T0.isoformat()


def test_learning_worker_ingests_risk_before_focus_lock(tmp_path):
    data = tmp_path / "data"
    db = data / "learning.sqlite3"
    generation = "GEN-X"
    strategy = versions()

    generation_dir = data / "diamond_generations" / generation
    generation_dir.mkdir(parents=True)
    candidate = {
        "generated_at": T0.isoformat(),
        "source_updated_at": T0.isoformat(),
        "last_trade_at": T0.isoformat(),
        "schema_version": 4,
        "market_key": "condition:condition-A|outcome:yes",
        "question": "Market A",
        "condition_id": "condition-A",
        "token_id": "A",
        "outcome": "Yes",
        "direction": "BUY",
        "price": 0.42,
        "classification": "DIAMOND",
        "diamond": True,
        "scores": {
            "signal_quality": 90.0,
            "verification": 90.0,
            "entry_quality": 80.0,
            "resolution_reliability": 90.0,
        },
        "cashflow_alert": {"active": False, "tier": None, "quality": None},
        "why_not_diamond": [],
        "resolution": {"remaining_seconds": 3600.0},
        "metrics": {
            "verified": True,
            "confirmations": 3,
            "data_confidence": 0.9,
            "trades_1m": 4,
            "trades_5m": 12,
            "trades_15m": 22,
            "volume_1m": 400.0,
            "volume_5m": 2500.0,
            "volume_15m": 5000.0,
            "net_1m": 300.0,
            "net_5m": 1800.0,
            "net_15m": 3200.0,
            "strength_1m": 0.6,
            "strength_5m": 0.55,
            "strength_15m": 0.4,
            "largest_trade_5m": 500.0,
            "largest_trade_ratio": 0.2,
        },
    }
    (generation_dir / "diamond_candidates.json").write_text(
        json.dumps([candidate]), encoding="utf-8"
    )
    (generation_dir / "flow_state.json").write_text(
        json.dumps(
            {
                "A": {
                    "token_id": "A",
                    "condition_id": "condition-A",
                    "outcome": "Yes",
                    "evidence_id": EVIDENCE_A,
                    "evidence_cursor": [100, 1],
                    "evidence_at": T0.isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )

    risk_row = focus_candidate("A", 100, 1, T0)
    enqueue_risk_snapshot(
        {
            "source_generation_id": generation,
            "source_generated_at": T0.isoformat(),
            "generated_at": T0.isoformat(),
            "markets_checked": 1,
            "passed": 1,
            "results": [risk_row],
        },
        data_dir=data,
        strategy_versions=strategy,
    )

    enqueue_focus_event(
        {
            "event_type": "LOCKED",
            "event_at": T0.isoformat(),
            "source_generation_id": generation,
            "source_evidence_id": EVIDENCE_A,
            "source_evidence_cursor": [100, 1],
            "source_evidence_at": T0.isoformat(),
            "condition_id": "condition-A",
            "token_id": "A",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.42,
            "progress": 0,
            "fail_count": 0,
            "locked_at": T0.isoformat(),
            "price_at_lock": 0.42,
            "challenger_present": False,
            "challenger": None,
        },
        data_dir=data,
        strategy_versions=strategy,
    )

    outcomes = learning_worker.run_once(data_dir=data, db_path=db)

    assert [row["status"] for row in outcomes] == ["INGESTED", "INGESTED"]
    with LearningStore(db) as store:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_observations"
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM focus_events"
        ).fetchone()[0] == 1


def test_challenger_is_projected_on_wait_transition(monkeypatch, tmp_path):
    paths = configure_runner(monkeypatch, tmp_path)
    captured = []

    def capture(event, *, data_dir, strategy_versions):
        captured.append(event)
        return {"status": "QUEUED", "focus_event_id": str(len(captured))}

    monkeypatch.setattr(focus_runner, "enqueue_focus_event", capture)

    save_json_atomic(
        paths["risk"],
        risk_snapshot(
            "GEN-1",
            [focus_candidate("A", 100, 1, T0)],
            T0,
        ),
    )
    focus_runner.run_once(now=T0)

    later = T0 + timedelta(seconds=60)
    save_json_atomic(
        paths["risk"],
        risk_snapshot(
            "GEN-2",
            [
                focus_candidate("A", 101, 1, later, tx="b", price=0.45),
                focus_candidate("B", 200, 1, later, tx="c", price=0.44),
            ],
            later,
        ),
    )
    state, events, status = focus_runner.run_once(now=later)

    assert status == "OK"
    assert state["focus"]["status"] == "WAIT"
    assert state["challenger"]["token_id"] == "B"
    assert events[-1]["type"] == "WAIT"

    wait = captured[-1]
    assert wait["event_type"] == "WAIT"
    assert wait["challenger_present"] is True
    assert wait["challenger"]["token_id"] == "B"
