import json

from scripts import risk_engine, risk_worker
from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts import learning_versioning


NOW = "2026-10-08T12:00:00+00:00"


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


def configure_empty_generation(monkeypatch, tmp_path):
    data = tmp_path / "data"
    generation = data / "diamond_generations" / "GEN-X"
    generation.mkdir(parents=True)
    (data / "diamond_generation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": "GEN-X",
                "published_at": NOW,
            }
        ),
        encoding="utf-8",
    )
    (generation / "diamond_analysis_v3.json").write_text("[]", encoding="utf-8")
    (generation / "flow_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(risk_engine, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(
        risk_engine, "RISK_FILE", data / "risk_assessment.json", raising=False
    )
    return data


def test_new_risk_snapshot_freezes_strategy_versions(monkeypatch, tmp_path):
    data = configure_empty_generation(monkeypatch, tmp_path)
    frozen = versions()
    monkeypatch.setattr(
        risk_engine, "learning_strategy_versions", lambda: frozen
    )

    risk_engine.run_once(verbose=False)

    payload = json.loads(
        (data / "risk_assessment.json").read_text(encoding="utf-8")
    )
    assert payload["source_generation_id"] == "GEN-X"
    assert payload["strategy_versions"] == frozen


def test_provenance_failure_never_blocks_risk_publication(monkeypatch, tmp_path):
    data = configure_empty_generation(monkeypatch, tmp_path)
    monkeypatch.setattr(
        risk_engine, "learning_strategy_versions", lambda: None
    )

    risk_engine.run_once(verbose=False)

    payload = json.loads(
        (data / "risk_assessment.json").read_text(encoding="utf-8")
    )
    assert payload["source_generation_id"] == "GEN-X"
    assert payload["strategy_versions"] is None
    assert payload["results"] == []


def test_old_risk_snapshot_is_not_relabelled_by_new_runtime(monkeypatch):
    old_payload = {
        "source_generation_id": "OLD-GEN",
        "source_generated_at": NOW,
        "generated_at": NOW,
        "markets_checked": 1,
        "passed": 0,
        "results": [],
    }
    monkeypatch.setattr(risk_worker, "_LEARNING_INIT_ERROR", None)
    monkeypatch.setattr(
        risk_worker.risk_engine,
        "existing_risk_output",
        lambda: old_payload,
    )

    def forbidden_queue(*args, **kwargs):
        raise AssertionError("old snapshot without frozen provenance must not queue")

    monkeypatch.setattr(risk_worker, "enqueue_risk_snapshot", forbidden_queue)

    risk_worker._publish_learning_snapshot()


def test_queue_uses_versions_frozen_inside_risk_snapshot(monkeypatch):
    frozen = versions()
    payload = {
        "source_generation_id": "GEN-X",
        "source_generated_at": NOW,
        "generated_at": NOW,
        "strategy_versions": frozen,
        "markets_checked": 1,
        "passed": 1,
        "results": [],
    }
    seen = {}
    monkeypatch.setattr(risk_worker, "_LEARNING_INIT_ERROR", None)
    monkeypatch.setattr(
        risk_worker.risk_engine,
        "existing_risk_output",
        lambda: payload,
    )

    def capture(snapshot, *, data_dir, strategy_versions):
        seen["snapshot"] = snapshot
        seen["versions"] = strategy_versions
        return {"status": "ALREADY_QUEUED", "generation_id": "GEN-X"}

    monkeypatch.setattr(risk_worker, "enqueue_risk_snapshot", capture)

    risk_worker._publish_learning_snapshot()

    assert seen["snapshot"] is payload
    assert seen["versions"] == frozen


def test_current_strategy_versions_alias_matches_runtime():
    assert learning_versioning.current_strategy_versions() == learning_versioning.runtime_strategy_versions()
