import json

import pytest

from scripts import learning_ingest
from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_store import LearningDataConflict, LearningStore


NOW = "2026-10-08T12:00:00+00:00"
EVIDENCE = "0x" + "ab" * 32 + ":7"


def versions():
    return {
        "pipeline_version": "pipeline-v1",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": "a" * 40,
        "config_fingerprint": "CFG-test",
    }


def diamond_row(*, token="token-a", classification="DIAMOND", diamond=True):
    return {
        "generated_at": NOW,
        "condition_id": "condition-" + token,
        "token_id": token,
        "outcome": "Yes",
        "direction": "BUY",
        "classification": classification,
        "diamond": diamond,
        "price": 0.55,
        "scores": {
            "signal_quality": 88.0,
            "verification": 90.0,
            "entry_quality": 70.0,
            "resolution_reliability": 85.0,
        },
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
        "cashflow_alert": {
            "active": True,
            "tier": "LARGE",
            "quality": "BROAD",
        },
        "resolution": {"remaining_seconds": 3600},
    }


def risk_row(*, token="token-a", risk_ok=True, reason_codes=None):
    return {
        "checked_at": NOW,
        "condition_id": "condition-" + token,
        "token_id": token,
        "outcome": "Yes",
        "direction": "BUY",
        "risk_ok": risk_ok,
        "decision": "PASS" if risk_ok else "BLOCK",
        "reason_codes": list(reason_codes or []),
        "reasons": [],
        "warnings": [],
        "evidence_id": EVIDENCE,
        "evidence_cursor": [100, 7],
        "evidence_at": NOW,
    }


def manifest(generation="GEN-1"):
    return {
        "schema_version": 1,
        "generation_id": generation,
        "published_at": NOW,
    }


def risk_payload(rows, generation="GEN-1"):
    return {
        "source_generation_id": generation,
        "source_generated_at": NOW,
        "generated_at": NOW,
        "results": rows,
    }


def test_ingests_all_analysis_rows_and_risk_blocks(tmp_path):
    analysis = [
        diamond_row(token="token-a"),
        diamond_row(token="token-b", classification="CANDIDATE", diamond=False),
    ]
    risks = [
        risk_row(token="token-a"),
        risk_row(token="token-b", risk_ok=False, reason_codes=["NOT_DIAMOND"]),
    ]

    with LearningStore(tmp_path / "learning.sqlite3") as store:
        result = learning_ingest.ingest_generation(
            manifest(),
            analysis,
            risk_payload(risks),
            store=store,
            versions=versions(),
        )

        assert result["signals_seen"] == 2
        assert result["signals_inserted"] == 2
        assert result["risk_decisions_inserted"] == 2

        signals = store.conn.execute(
            "SELECT token_id,classification,diamond,payload_json "
            "FROM signal_observations ORDER BY token_id"
        ).fetchall()
        assert [row["token_id"] for row in signals] == ["token-a", "token-b"]
        assert signals[1]["classification"] == "CANDIDATE"
        assert signals[1]["diamond"] == 0

        blocked = store.conn.execute(
            "SELECT risk_ok,decision,reason_codes_json FROM risk_decisions "
            "WHERE risk_ok=0"
        ).fetchone()
        assert blocked["decision"] == "BLOCK"
        assert json.loads(blocked["reason_codes_json"]) == ["NOT_DIAMOND"]


def test_signal_payload_preserves_learning_features_and_unknown_probability(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        learning_ingest.ingest_generation(
            manifest(),
            [diamond_row()],
            risk_payload([risk_row()]),
            store=store,
            versions=versions(),
        )
        row = store.conn.execute(
            "SELECT payload_json FROM signal_observations"
        ).fetchone()
        payload = json.loads(row["payload_json"])

        assert payload["generation_rank"] == 1
        assert payload["volume_5m"] == 2500.0
        assert payload["strength_15m"] == 0.4
        assert payload["cashflow_quality"] == "BROAD"
        assert payload["time_to_resolution_seconds"] == 3600
        assert payload["forecast_probability"] is None
        assert payload["probability_model_version"] is None


def test_same_generation_is_idempotent(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        first = learning_ingest.ingest_generation(
            manifest(),
            [diamond_row()],
            risk_payload([risk_row()]),
            store=store,
            versions=versions(),
        )
        second = learning_ingest.ingest_generation(
            manifest(),
            [diamond_row()],
            risk_payload([risk_row()]),
            store=store,
            versions=versions(),
        )

        assert first["signals_inserted"] == 1
        assert first["risk_decisions_inserted"] == 1
        assert second["signals_inserted"] == 0
        assert second["risk_decisions_inserted"] == 0
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_observations"
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM risk_decisions"
        ).fetchone()[0] == 1


def test_changed_immutable_generation_fails_closed(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        learning_ingest.ingest_generation(
            manifest(),
            [diamond_row()],
            risk_payload([risk_row()]),
            store=store,
            versions=versions(),
        )
        changed = diamond_row()
        changed["price"] = 0.75

        with pytest.raises(LearningDataConflict):
            learning_ingest.ingest_generation(
                manifest(),
                [changed],
                risk_payload([risk_row()]),
                store=store,
                versions=versions(),
            )


def test_generation_binding_mismatch_fails_before_write(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        with pytest.raises(learning_ingest.LearningIngestError, match="does not match"):
            learning_ingest.ingest_generation(
                manifest("GEN-A"),
                [diamond_row()],
                risk_payload([risk_row()], generation="GEN-B"),
                store=store,
                versions=versions(),
            )
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_observations"
        ).fetchone()[0] == 0


def test_missing_or_duplicate_risk_identity_fails_closed(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        with pytest.raises(learning_ingest.LearningIngestError, match="missing"):
            learning_ingest.ingest_generation(
                manifest(),
                [diamond_row()],
                risk_payload([]),
                store=store,
                versions=versions(),
            )

    with LearningStore(tmp_path / "learning2.sqlite3") as store:
        with pytest.raises(learning_ingest.LearningIngestError, match="Duplicate"):
            learning_ingest.ingest_generation(
                manifest(),
                [diamond_row()],
                risk_payload([risk_row(), risk_row()]),
                store=store,
                versions=versions(),
            )


def test_ingest_latest_reads_candidate_generation_archive(tmp_path):
    data = tmp_path / "data"
    generations = data / "diamond_generations"
    generation = generations / "GEN-X"
    generation.mkdir(parents=True)

    manifest_path = data / "diamond_generation.json"
    risk_path = data / "risk_assessment.json"
    db_path = data / "learning.sqlite3"

    manifest_path.write_text(json.dumps(manifest("GEN-X")), encoding="utf-8")
    (generation / "diamond_candidates.json").write_text(
        json.dumps([diamond_row(token="token-a")]), encoding="utf-8"
    )
    risk_path.write_text(
        json.dumps(risk_payload([risk_row(token="token-a")], generation="GEN-X")),
        encoding="utf-8",
    )

    result = learning_ingest.ingest_latest(
        manifest_file=manifest_path,
        generations_dir=generations,
        risk_file=risk_path,
        db_file=db_path,
        versions=versions(),
    )

    assert result["generation_id"] == "GEN-X"
    with LearningStore(db_path) as store:
        row = store.conn.execute(
            "SELECT source_generation_id,token_id FROM signal_observations"
        ).fetchone()
        assert dict(row) == {
            "source_generation_id": "GEN-X",
            "token_id": "token-a",
        }


def test_ingest_latest_requires_frozen_generation_versions(tmp_path):
    data = tmp_path / "data"
    generations = data / "diamond_generations"
    generation = generations / "GEN-X"
    generation.mkdir(parents=True)
    manifest_path = data / "diamond_generation.json"
    risk_path = data / "risk_assessment.json"
    db_path = data / "learning.sqlite3"

    manifest_path.write_text(json.dumps(manifest("GEN-X")), encoding="utf-8")
    (generation / "diamond_candidates.json").write_text(
        json.dumps([diamond_row()]), encoding="utf-8"
    )
    risk_path.write_text(
        json.dumps(risk_payload([risk_row()], generation="GEN-X")),
        encoding="utf-8",
    )

    with pytest.raises(
        learning_ingest.LearningIngestError,
        match="lacks frozen strategy_versions",
    ):
        learning_ingest.ingest_latest(
            manifest_file=manifest_path,
            generations_dir=generations,
            risk_file=risk_path,
            db_file=db_path,
        )


def test_extra_low_risk_rows_do_not_expand_learning_population(tmp_path):
    candidate = diamond_row(token="token-a")
    extra = risk_row(token="token-low", risk_ok=False, reason_codes=["NOT_DIAMOND"])
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        result = learning_ingest.ingest_generation(
            manifest(),
            [candidate],
            risk_payload([risk_row(token="token-a"), extra]),
            store=store,
            versions=versions(),
        )
        assert result["signals_seen"] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_observations"
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM risk_decisions"
        ).fetchone()[0] == 1
