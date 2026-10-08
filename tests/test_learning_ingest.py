import json
from datetime import datetime, timezone

from scripts import learning_worker
from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_queue import enqueue_risk_snapshot
from scripts.learning_store import LearningStore

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
NOW_S = NOW.isoformat()
EVIDENCE_A = "0x" + "ab" * 32 + ":7"
EVIDENCE_B = "0x" + "cd" * 32 + ":8"


def versions():
    return {
        "pipeline_version": "pipeline-v3",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": "a" * 40,
        "config_fingerprint": "SRC-test",
    }


def candidate(
    token,
    *,
    condition="condition-a",
    outcome="Yes",
    classification="DIAMOND",
    diamond=True,
    price=0.55,
    signal_quality=88.0,
    cashflow_active=False,
):
    return {
        "generated_at": NOW_S,
        "source_updated_at": NOW_S,
        "last_trade_at": NOW_S,
        "schema_version": 4,
        "market_key": f"condition:{condition}|outcome:{outcome.lower()}",
        "question": f"Market {token}",
        "outcome": outcome,
        "condition_id": condition,
        "token_id": token,
        "price": price,
        "classification": classification,
        "diamond": diamond,
        "direction": "BUY",
        "scores": {
            "signal_quality": signal_quality,
            "verification": 90.0,
            "entry_quality": 70.0,
            "resolution_reliability": 85.0,
        },
        "cashflow_alert": {
            "active": cashflow_active,
            "tier": "VERY_LARGE" if cashflow_active else None,
            "quality": "BROAD" if cashflow_active else None,
        },
        "why_not_diamond": [] if diamond else ["Signal Quality below threshold"],
        "resolution": {"remaining_seconds": 3600.0},
        "metrics": {
            "state": "VERIFIED",
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


def flow(token, evidence_id, log_index, *, condition="condition-a", outcome="Yes"):
    return {
        "schema_version": 4,
        "token_id": token,
        "condition_id": condition,
        "outcome": outcome,
        "evidence_id": evidence_id,
        "evidence_cursor": [100, log_index],
        "evidence_at": NOW_S,
    }


def risk(
    token,
    *,
    condition="condition-a",
    outcome="Yes",
    classification="DIAMOND",
    risk_ok=True,
    reason_codes=None,
    cashflow_active=False,
):
    return {
        "checked_at": NOW_S,
        "market_key": f"condition:{condition}|outcome:{outcome.lower()}",
        "question": f"Market {token}",
        "condition_id": condition,
        "token_id": token,
        "outcome": outcome,
        "direction": "BUY",
        "price": 0.55,
        "classification": classification,
        "risk_ok": risk_ok,
        "decision": "PASS" if risk_ok else "BLOCK",
        "reasons": [],
        "reason_codes": list(reason_codes or []),
        "warnings": [],
        "is_diamond": classification == "DIAMOND",
        "cashflow_active": cashflow_active,
        "signal_quality": 88.0,
        "verification": 90.0,
    }


def write_generation(data_dir, generation_id, candidates, flows):
    generation_dir = data_dir / "diamond_generations" / generation_id
    generation_dir.mkdir(parents=True)
    (generation_dir / "diamond_candidates.json").write_text(
        json.dumps(candidates), encoding="utf-8"
    )
    (generation_dir / "flow_state.json").write_text(
        json.dumps(flows), encoding="utf-8"
    )


def risk_payload(generation_id, rows):
    return {
        "source_generation_id": generation_id,
        "source_generated_at": NOW_S,
        "generated_at": NOW_S,
        "markets_checked": len(rows),
        "passed": sum(1 for row in rows if row.get("risk_ok") is True),
        "results": rows,
    }


def test_ld2_ingests_diamond_and_risk_block_without_recomputing(tmp_path):
    data = tmp_path / "data"
    db = data / "learning.sqlite3"
    generation = "GEN-1"

    diamond = candidate("token-a")
    blocked = candidate(
        "token-b",
        classification="CANDIDATE",
        diamond=False,
        signal_quality=68.0,
    )
    write_generation(
        data,
        generation,
        [diamond, blocked],
        {
            "token-a": flow("token-a", EVIDENCE_A, 7),
            "token-b": flow("token-b", EVIDENCE_B, 8),
        },
    )

    # Include a LOW row to prove LD-2 stores the frozen candidate population,
    # not every scheduler/analyzed market.
    rows = [
        risk("token-a", risk_ok=True),
        risk(
            "token-b",
            classification="CANDIDATE",
            risk_ok=False,
            reason_codes=["NOT_DIAMOND"],
        ),
        risk(
            "token-low",
            classification="LOW",
            risk_ok=False,
            reason_codes=["NOT_DIAMOND"],
        ),
    ]
    payload = risk_payload(generation, rows)

    queued = enqueue_risk_snapshot(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )
    assert queued["status"] == "QUEUED"

    outcomes = learning_worker.run_once(data_dir=data, db_path=db)
    assert outcomes == [
        {
            "status": "INGESTED",
            "generation_id": generation,
            "signals_ingested": 2,
        }
    ]
    assert not list((data / "learning_queue").glob("risk_*.json"))

    with LearningStore(db) as store:
        signals = store.conn.execute(
            "SELECT * FROM signal_observations ORDER BY signal_quality DESC"
        ).fetchall()
        risks = store.conn.execute(
            "SELECT * FROM risk_decisions ORDER BY decision DESC"
        ).fetchall()
        marker = store.fetch_one(
            "SELECT * FROM generation_ingestions WHERE generation_id=?",
            (generation,),
        )

        assert len(signals) == 2
        assert len(risks) == 2
        assert marker["signals_ingested"] == 2
        assert marker["risk_markets_checked"] == 3

        stored = {
            row["decision"]: json.loads(row["payload_json"])
            for row in risks
        }
        assert stored["PASS"]["risk_ok"] is True
        assert stored["BLOCK"]["risk_ok"] is False
        assert stored["BLOCK"]["reason_codes"] == ["NOT_DIAMOND"]

        signal_payloads = [
            json.loads(row["payload_json"])
            for row in signals
        ]
        by_token = {row["token_id"]: row for row in signal_payloads}
        assert by_token["token-a"]["source_evidence_id"] == EVIDENCE_A
        assert by_token["token-a"]["source_evidence_cursor"] == [100, 7]
        assert by_token["token-b"]["source_evidence_id"] == EVIDENCE_B
        assert by_token["token-b"]["signal_quality"] == 68.0


def test_ld2_queue_is_durable_and_idempotent(tmp_path):
    data = tmp_path / "data"
    db = data / "learning.sqlite3"
    generation = "GEN-2"
    row = candidate("token-a")
    write_generation(
        data,
        generation,
        [row],
        {"token-a": flow("token-a", EVIDENCE_A, 7)},
    )
    payload = risk_payload(generation, [risk("token-a")])

    first = enqueue_risk_snapshot(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )
    second = enqueue_risk_snapshot(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )
    assert first["status"] == "QUEUED"
    assert second["status"] == "ALREADY_QUEUED"

    learning_worker.run_once(data_dir=data, db_path=db)

    # Recreate the same durable message as if cleanup failed after DB commit.
    enqueue_risk_snapshot(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )
    outcomes = learning_worker.run_once(data_dir=data, db_path=db)
    assert outcomes[0]["status"] == "ALREADY_INGESTED"

    with LearningStore(db) as store:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_observations"
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM risk_decisions"
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM generation_ingestions"
        ).fetchone()[0] == 1


def test_low_only_risk_generation_is_not_queued(tmp_path):
    payload = risk_payload(
        "GEN-LOW",
        [
            risk(
                "token-low",
                classification="LOW",
                risk_ok=False,
                reason_codes=["NOT_DIAMOND"],
            )
        ],
    )
    result = enqueue_risk_snapshot(
        payload,
        data_dir=tmp_path / "data",
        strategy_versions=versions(),
    )
    assert result["status"] == "NO_RELEVANT_SIGNALS"
    assert not list((tmp_path / "data" / "learning_queue").glob("*.json"))


def test_corrupt_queue_does_not_block_later_generation(tmp_path):
    data = tmp_path / "data"
    db = data / "learning.sqlite3"
    queue = data / "learning_queue"
    queue.mkdir(parents=True)
    (queue / "risk_000-bad.json").write_text("{bad-json", encoding="utf-8")

    generation = "GEN-GOOD"
    row = candidate("token-a")
    write_generation(
        data,
        generation,
        [row],
        {"token-a": flow("token-a", EVIDENCE_A, 7)},
    )
    enqueue_risk_snapshot(
        risk_payload(generation, [risk("token-a")]),
        data_dir=data,
        strategy_versions=versions(),
    )

    outcomes = learning_worker.run_once(data_dir=data, db_path=db)
    assert outcomes[0]["status"] == "ERROR"
    assert outcomes[1]["status"] == "INGESTED"

    with LearningStore(db) as store:
        assert store.generation_ingested(generation) is True


def test_runtime_places_learning_after_risk_without_becoming_focus_authority():
    import run_machine

    names = [name for name, _ in run_machine.SERVICES]
    assert names.index("risk") < names.index("learning") < names.index("focus")
