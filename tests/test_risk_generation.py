"""AC-1B Risk generation-consumer contract tests."""

import json
from pathlib import Path

import pytest

from scripts import risk_engine as risk


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _configure_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "data": data,
        "manifest": data / "diamond_generation.json",
        "generations": data / "diamond_generations",
        "risk": data / "risk_assessment.json",
        "root_analysis": data / "diamond_analysis_v3.json",
        "root_flow": data / "flow_state.json",
    }

    monkeypatch.setattr(risk, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(risk, "RISK_FILE", paths["risk"], raising=False)
    monkeypatch.setattr(risk, "ANALYSIS_FILE", paths["root_analysis"], raising=False)
    monkeypatch.setattr(risk, "FLOW_FILE", paths["root_flow"], raising=False)

    # Accept either small constants or runtime DATA_DIR derivation in production.
    monkeypatch.setattr(risk, "GENERATION_MANIFEST_FILE", paths["manifest"], raising=False)
    monkeypatch.setattr(risk, "MANIFEST_FILE", paths["manifest"], raising=False)
    monkeypatch.setattr(risk, "GENERATIONS_DIR", paths["generations"], raising=False)
    monkeypatch.setattr(risk, "GENERATION_DIR", paths["generations"], raising=False)

    return paths


def _analysis(marker="X", token_id="token-x"):
    return [{"token_id": token_id, "analysis_marker": marker}]


def _flow(marker="X", token_id="token-x"):
    return {token_id: {"token_id": token_id, "flow_marker": marker}}


def _publish_generation(
    paths,
    generation_id="X",
    published_at="2026-10-05T12:00:00+00:00",
    analysis=None,
    flow=None,
    *,
    omit_analysis=False,
    omit_flow=False,
    corrupt_analysis=False,
    corrupt_flow=False,
):
    analysis = _analysis(generation_id) if analysis is None else analysis
    flow = _flow(generation_id) if flow is None else flow
    generation = paths["generations"] / generation_id
    generation.mkdir(parents=True, exist_ok=True)

    if not omit_analysis:
        if corrupt_analysis:
            (generation / "diamond_analysis_v3.json").write_text("{bad json", encoding="utf-8")
        else:
            _write(generation / "diamond_analysis_v3.json", analysis)

    if not omit_flow:
        if corrupt_flow:
            (generation / "flow_state.json").write_text("{bad json", encoding="utf-8")
        else:
            _write(generation / "flow_state.json", flow)

    _write(
        paths["manifest"],
        {
            "schema_version": 1,
            "generation_id": generation_id,
            "published_at": published_at,
            "generation_dir": f"diamond_generations/{generation_id}",
        },
    )
    return generation


def _stub_assess(row, flow_state):
    token_id = row["token_id"]
    flow = flow_state[token_id]
    return {
        "risk_ok": True,
        "analysis_marker": row.get("analysis_marker"),
        "flow_marker": flow.get("flow_marker"),
    }


def _run_no_raise():
    try:
        return risk.run_once(verbose=False)
    except Exception:
        return None


def test_ac1b_1_new_generation_x_processed_once(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")
    monkeypatch.setattr(risk, "assess", _stub_assess)

    risk.run_once(verbose=False)

    output = _read(paths["risk"])
    assert output["source_generation_id"] == "X"
    assert output["markets_checked"] == 1
    assert output["passed"] == 1
    assert output["results"][0]["analysis_marker"] == "X"
    assert output["results"][0]["flow_marker"] == "X"


def test_ac1b_2_same_x_is_noop_and_not_rewritten(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")
    calls = []

    def assess_once(row, flow_state):
        calls.append("assess")
        return _stub_assess(row, flow_state)

    monkeypatch.setattr(risk, "assess", assess_once)
    risk.run_once(verbose=False)
    first = paths["risk"].read_bytes()

    def forbidden_assess(*args, **kwargs):
        raise AssertionError("same generation must not be assessed twice")

    def forbidden_write(*args, **kwargs):
        raise AssertionError("same generation must not rewrite risk output")

    monkeypatch.setattr(risk, "assess", forbidden_assess)
    monkeypatch.setattr(risk, "save_json_atomic", forbidden_write)
    risk.run_once(verbose=False)

    assert calls == ["assess"]
    assert paths["risk"].read_bytes() == first


def test_ac1b_3_failed_x_is_retried(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)
    monkeypatch.setattr(risk, "assess", _stub_assess)

    real_save = risk.save_json_atomic
    attempts = []

    def fail_risk_write(path, data):
        attempts.append(Path(path))
        if Path(path) == paths["risk"]:
            raise OSError("risk output write failed")
        return real_save(path, data)

    monkeypatch.setattr(risk, "save_json_atomic", fail_risk_write)
    _run_no_raise()
    assert _read(paths["risk"]) == old

    monkeypatch.setattr(risk, "save_json_atomic", real_save)
    risk.run_once(verbose=False)

    assert _read(paths["risk"])["source_generation_id"] == "X"
    assert attempts.count(paths["risk"]) == 1


def test_ac1b_4_manifest_switch_to_y_during_x_stays_on_x(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    published_x = "2026-10-05T12:00:00+00:00"
    published_y = "2026-10-05T12:01:00+00:00"
    _publish_generation(paths, "X", published_x)
    generation_y = paths["generations"] / "Y"
    generation_y.mkdir(parents=True)
    _write(generation_y / "diamond_analysis_v3.json", _analysis("Y", "token-y"))
    _write(generation_y / "flow_state.json", _flow("Y", "token-y"))

    switched = {"done": False}

    def switching_assess(row, flow_state):
        if not switched["done"]:
            switched["done"] = True
            _write(
                paths["manifest"],
                {
                    "schema_version": 1,
                    "generation_id": "Y",
                    "published_at": published_y,
                    "generation_dir": "diamond_generations/Y",
                },
            )
        return _stub_assess(row, flow_state)

    monkeypatch.setattr(risk, "assess", switching_assess)
    risk.run_once(verbose=False)

    first = _read(paths["risk"])
    assert first["source_generation_id"] == "X"
    assert first["source_generated_at"] == published_x
    assert first["results"][0]["analysis_marker"] == "X"
    assert first["results"][0]["flow_marker"] == "X"

    monkeypatch.setattr(risk, "assess", _stub_assess)
    risk.run_once(verbose=False)
    second = _read(paths["risk"])
    assert second["source_generation_id"] == "Y"
    assert second["source_generated_at"] == published_y
    assert second["results"][0]["analysis_marker"] == "Y"
    assert second["results"][0]["flow_marker"] == "Y"


def test_ac1b_5_analysis_x_and_flow_x_are_always_paired(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")

    # Root files deliberately disagree. AC-1B must never consume them.
    _write(paths["root_analysis"], _analysis("ROOT-Y", "root-token"))
    _write(paths["root_flow"], _flow("ROOT-Z", "root-token"))

    seen = {}

    def capture_assess(row, flow_state):
        seen["analysis"] = row["analysis_marker"]
        seen["flow"] = flow_state[row["token_id"]]["flow_marker"]
        return {"risk_ok": True}

    monkeypatch.setattr(risk, "assess", capture_assess)
    risk.run_once(verbose=False)

    assert seen == {"analysis": "X", "flow": "X"}
    assert _read(paths["risk"])["source_generation_id"] == "X"


def test_ac1b_6_missing_generation_file_preserves_old_output(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X", omit_flow=True)
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)
    monkeypatch.setattr(
        risk,
        "assess",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("assess must not run")),
    )

    _run_no_raise()

    assert _read(paths["risk"]) == old


def test_ac1b_7_corrupt_generation_file_preserves_old_output(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X", corrupt_analysis=True)
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)
    monkeypatch.setattr(
        risk,
        "assess",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("assess must not run")),
    )

    _run_no_raise()

    assert _read(paths["risk"]) == old


def test_ac1b_8_source_generation_id_is_exact(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "generation-exact-123")
    monkeypatch.setattr(risk, "assess", _stub_assess)

    risk.run_once(verbose=False)

    assert _read(paths["risk"])["source_generation_id"] == "generation-exact-123"


def test_ac1b_9_source_generated_at_comes_from_manifest(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    source_time = "2026-10-05T09:17:33.123456+00:00"
    _publish_generation(paths, "X", source_time)
    monkeypatch.setattr(risk, "assess", _stub_assess)

    risk.run_once(verbose=False)

    assert _read(paths["risk"])["source_generated_at"] == source_time


def test_ac1b_10_generation_processed_only_after_successful_output_write(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")
    old = {"source_generation_id": "OLD"}
    _write(paths["risk"], old)
    assess_calls = []
    monkeypatch.setattr(
        risk,
        "assess",
        lambda row, flow: (assess_calls.append("X") or _stub_assess(row, flow)),
    )

    real_save = risk.save_json_atomic

    def fail_once(path, data):
        if Path(path) == paths["risk"]:
            raise OSError("disk full")
        return real_save(path, data)

    monkeypatch.setattr(risk, "save_json_atomic", fail_once)
    _run_no_raise()
    assert _read(paths["risk"])["source_generation_id"] == "OLD"

    monkeypatch.setattr(risk, "save_json_atomic", real_save)
    risk.run_once(verbose=False)

    assert assess_calls == ["X", "X"]
    assert _read(paths["risk"])["source_generation_id"] == "X"


def test_ac1b_11_restart_recognizes_already_processed_x(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")
    existing = {
        "source_generation_id": "X",
        "source_generated_at": "2026-10-05T12:00:00+00:00",
        "generated_at": "2026-10-05T12:00:01+00:00",
        "markets_checked": 1,
        "passed": 1,
        "results": [{"risk_ok": True}],
    }
    _write(paths["risk"], existing)

    monkeypatch.setattr(
        risk,
        "assess",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("restart must recognize persisted generation id")
        ),
    )
    monkeypatch.setattr(
        risk,
        "save_json_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("restart no-op must not rewrite output")
        ),
    )

    risk.run_once(verbose=False)

    assert _read(paths["risk"]) == existing


def test_ac1b_12_valid_empty_generation_is_processed(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    source_time = "2026-10-05T12:00:00+00:00"
    _publish_generation(paths, "X", source_time, analysis=[], flow={})
    monkeypatch.setattr(
        risk,
        "assess",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty generation must not call assess")
        ),
    )

    risk.run_once(verbose=False)

    output = _read(paths["risk"])
    assert output["source_generation_id"] == "X"
    assert output["source_generated_at"] == source_time
    assert output["markets_checked"] == 0
    assert output["passed"] == 0
    assert output["results"] == []


def test_ac1b_13_missing_manifest_is_input_not_ready_noop(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)

    risk.run_once(verbose=False)

    assert _read(paths["risk"]) == old


def test_ac1b_14_corrupt_manifest_fails_closed(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)
    paths["manifest"].write_text("{bad json", encoding="utf-8")

    _run_no_raise()

    assert _read(paths["risk"]) == old


def test_ac1b_15_manifest_without_generation_id_fails_closed(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    old = {"source_generation_id": "OLD", "sentinel": True}
    _write(paths["risk"], old)
    _write(
        paths["manifest"],
        {
            "schema_version": 1,
            "published_at": "2026-10-05T12:00:00+00:00",
        },
    )

    _run_no_raise()

    assert _read(paths["risk"]) == old
