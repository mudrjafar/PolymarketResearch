import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import diamond_filter_v3 as diamond


def _configure_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "data": data,
        "flow": data / "flow_state.json",
        "analysis": data / "diamond_analysis_v3.json",
        "candidates": data / "diamond_candidates.json",
        "diamonds": data / "diamonds.json",
        "generations": data / "diamond_generations",
        "manifest": data / "diamond_generation.json",
    }
    monkeypatch.setattr(diamond, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(diamond, "FLOW_FILE", paths["flow"], raising=False)
    monkeypatch.setattr(diamond, "ANALYSIS_FILE", paths["analysis"], raising=False)
    monkeypatch.setattr(diamond, "CANDIDATES_FILE", paths["candidates"], raising=False)
    monkeypatch.setattr(diamond, "DIAMONDS_FILE", paths["diamonds"], raising=False)
    monkeypatch.setattr(diamond, "GENERATION_DIR", paths["generations"], raising=False)
    monkeypatch.setattr(diamond, "GENERATIONS_DIR", paths["generations"], raising=False)
    monkeypatch.setattr(diamond, "GENERATION_ROOT", paths["generations"], raising=False)
    monkeypatch.setattr(diamond, "GENERATION_MANIFEST_FILE", paths["manifest"], raising=False)
    monkeypatch.setattr(diamond, "MANIFEST_FILE", paths["manifest"], raising=False)
    monkeypatch.setattr(diamond, "LIVE_TRADES_FILE", data / "live_trades.jsonl", raising=False)
    monkeypatch.setattr(diamond, "recent_trades", lambda: [])
    return paths


def _flow():
    return {
        "market-a": {
            "condition_id": "condition-a",
            "market": {"question": "A", "outcome": "Yes", "token_id": "token-a"},
        }
    }


def _analysis(row):
    return {
        "market_key": row.get("condition_id", "condition-a"),
        "question": row.get("market", {}).get("question", "A"),
        "classification": "DIAMOND",
        "diamond": True,
        "cashflow_alert": {"active": False},
        "scores": {"signal_quality": 90.0, "verification": 90.0},
    }


def _resolve_generation_dir(paths, manifest):
    rel = Path(manifest["generation_dir"])
    return rel if rel.is_absolute() else paths["data"] / rel


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _successful_run(monkeypatch, paths, flow=None):
    flow = _flow() if flow is None else flow
    _write(paths["flow"], flow)
    monkeypatch.setattr(diamond, "analyze", lambda row, live: _analysis(row))
    return diamond.run_once(verbose=False)


def test_ac1_1_complete_generation_publication(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    flow = _flow()
    _successful_run(monkeypatch, paths, flow)

    manifest = _read(paths["manifest"])
    generation = _resolve_generation_dir(paths, manifest)

    assert manifest["schema_version"] == 1
    assert manifest["generation_id"]
    assert manifest["generation_dir"] == f"diamond_generations/{manifest['generation_id']}"
    assert manifest["markets_analyzed"] == 1
    assert manifest["candidates"] == 1
    assert manifest["diamonds"] == 1

    assert _read(generation / "flow_state.json") == flow
    assert isinstance(_read(generation / "diamond_analysis_v3.json"), list)
    assert isinstance(_read(generation / "diamond_candidates.json"), list)
    assert isinstance(_read(generation / "diamonds.json"), list)


def test_ac1_2_manifest_is_last_commit_point(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    writes = []

    def recording_atomic(path, data):
        path = Path(path)
        writes.append(path)
        _write(path, data)

    monkeypatch.setattr(diamond, "atomic_save", recording_atomic)
    _successful_run(monkeypatch, paths)

    assert writes
    assert writes[-1] == paths["manifest"]
    assert paths["manifest"] not in writes[:-1]


def test_ac1_3_generation_snapshot_write_failure_keeps_manifest(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old_manifest = {"schema_version": 1, "generation_id": "old"}
    _write(paths["manifest"], old_manifest)
    attempted_generation_write = False

    real_atomic = diamond.atomic_save

    def failing_atomic(path, data):
        nonlocal attempted_generation_write
        path = Path(path)
        if "diamond_generations" in path.parts and path.name == "diamond_analysis_v3.json":
            attempted_generation_write = True
            raise OSError("generation snapshot write failed")
        return real_atomic(path, data)

    monkeypatch.setattr(diamond, "atomic_save", failing_atomic)
    with pytest.raises(OSError, match="generation snapshot write failed"):
        _successful_run(monkeypatch, paths)

    assert attempted_generation_write is True
    assert _read(paths["manifest"]) == old_manifest


def test_ac1_4_legacy_write_failure_keeps_manifest(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old_manifest = {"schema_version": 1, "generation_id": "old"}
    _write(paths["manifest"], old_manifest)

    real_atomic = diamond.atomic_save

    def failing_atomic(path, data):
        path = Path(path)
        if path == paths["analysis"]:
            raise OSError("legacy write failed")
        return real_atomic(path, data)

    monkeypatch.setattr(diamond, "atomic_save", failing_atomic)
    with pytest.raises(OSError, match="legacy write failed"):
        _successful_run(monkeypatch, paths)

    assert _read(paths["manifest"]) == old_manifest
    generations = list(paths["generations"].glob("*"))
    assert generations
    newest = generations[-1]
    assert (newest / "flow_state.json").exists()
    assert (newest / "diamond_analysis_v3.json").exists()
    assert (newest / "diamond_candidates.json").exists()
    assert (newest / "diamonds.json").exists()


def test_ac1_5_valid_empty_generation_is_published(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _write(paths["flow"], {})
    monkeypatch.setattr(diamond, "analyze", Mock(side_effect=AssertionError("analyze must not run")))

    diamond.run_once(verbose=False)

    manifest = _read(paths["manifest"])
    generation = _resolve_generation_dir(paths, manifest)
    assert manifest["markets_analyzed"] == 0
    assert manifest["candidates"] == 0
    assert manifest["diamonds"] == 0
    assert _read(generation / "flow_state.json") == {}
    assert _read(generation / "diamond_analysis_v3.json") == []
    assert _read(generation / "diamond_candidates.json") == []
    assert _read(generation / "diamonds.json") == []


def test_ac1_6_required_input_read_failure_does_not_publish(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old_manifest = {"schema_version": 1, "generation_id": "old"}
    old_legacy = [{"sentinel": True}]
    _write(paths["manifest"], old_manifest)
    _write(paths["analysis"], old_legacy)
    _write(paths["candidates"], old_legacy)
    _write(paths["diamonds"], old_legacy)
    paths["flow"].write_text("{not valid json", encoding="utf-8")

    try:
        diamond.run_once(verbose=False)
    except Exception:
        pass

    assert _read(paths["manifest"]) == old_manifest
    assert _read(paths["analysis"]) == old_legacy
    assert _read(paths["candidates"]) == old_legacy
    assert _read(paths["diamonds"]) == old_legacy
    assert not paths["generations"].exists() or not list(paths["generations"].iterdir())


def test_ac1_7_generation_ids_are_unique(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _successful_run(monkeypatch, paths)
    first = _read(paths["manifest"])["generation_id"]
    _successful_run(monkeypatch, paths)
    second = _read(paths["manifest"])["generation_id"]

    assert first != second
    assert (paths["generations"] / first).is_dir()
    assert (paths["generations"] / second).is_dir()


def test_ac1_8_orphan_partial_generation_never_becomes_committed(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    orphan = paths["generations"] / "orphan-partial"
    orphan.mkdir(parents=True)
    _write(orphan / "flow_state.json", {"orphan": True})

    _successful_run(monkeypatch, paths)

    manifest = _read(paths["manifest"])
    assert manifest["generation_id"] != "orphan-partial"
    assert _resolve_generation_dir(paths, manifest) != orphan
    assert not (orphan / "diamond_analysis_v3.json").exists()


def test_ac1_9_exact_in_memory_flow_snapshot_is_published_without_reread(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    flow_a = _flow()
    flow_b = {"market-b": {"condition_id": "condition-b", "market": {"question": "B"}}}
    _write(paths["flow"], flow_a)

    def changing_analyze(row, live):
        _write(paths["flow"], flow_b)
        return _analysis(row)

    monkeypatch.setattr(diamond, "analyze", changing_analyze)
    diamond.run_once(verbose=False)

    manifest = _read(paths["manifest"])
    generation = _resolve_generation_dir(paths, manifest)
    assert _read(paths["flow"]) == flow_b
    assert _read(generation / "flow_state.json") == flow_a


def test_ac1_10_legacy_outputs_remain_top_level_lists(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _successful_run(monkeypatch, paths)

    assert isinstance(_read(paths["analysis"]), list)
    assert isinstance(_read(paths["candidates"]), list)
    assert isinstance(_read(paths["diamonds"]), list)
