"""RF-4A Risk -> Focus candidate publication contract tests."""

import json
from datetime import datetime, timezone
from pathlib import Path

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
    return paths


def _diamond_row(
    token_id="token-a",
    *,
    condition_id="condition-a",
    outcome="Yes",
    question="Market A",
    direction="BUY",
    price=0.42,
    is_diamond=True,
    cashflow_active=True,
    signal_quality=91.5,
    verification=83.25,
):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 4,
        "source_updated_at": now,
        "last_trade_at": now,
        "market_key": f"condition:{condition_id}|outcome:{outcome.lower()}",
        "question": question,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome,
        "direction": direction,
        "price": price,
        "classification": "DIAMOND" if is_diamond else "CANDIDATE",
        "diamond": is_diamond,
        "why_not_diamond": [] if is_diamond else ["not diamond"],
        "scores": {
            "signal_quality": signal_quality,
            "verification": verification,
            "entry_quality": 80.0,
            "resolution_reliability": 90.0,
        },
        "metrics": {
            "verified": True,
            "confirmations": 3,
            "largest_trade_ratio": 0.20,
        },
        "resolution": {"remaining_seconds": 86400},
        "cashflow_alert": {
            "active": cashflow_active,
            "quality": "BROAD",
        },
    }


def _flow_row(
    token_id="token-a",
    *,
    condition_id="condition-a",
    outcome="Yes",
    evidence_id="0x1111111111111111111111111111111111111111111111111111111111111111:7",
    evidence_cursor=None,
    evidence_at="2026-10-05T12:00:00+00:00",
):
    now = datetime.now(timezone.utc).isoformat()
    if evidence_cursor is None:
        evidence_cursor = [100, 7]
    return {
        "schema_version": 4,
        "token_id": token_id,
        "condition_id": condition_id,
        "outcome": outcome,
        "direction": "BUY",
        "verified": True,
        "confirmations": 3,
        "source_updated_at": now,
        "last_trade_at": now,
        "remaining_seconds": 86400,
        "market": {
            "active": True,
            "closed": False,
            "accepting_orders": True,
        },
        "evidence_id": evidence_id,
        "evidence_cursor": evidence_cursor,
        "evidence_at": evidence_at,
    }


def _publish_generation(
    paths,
    generation_id="X",
    *,
    analysis=None,
    flow=None,
    published_at="2026-10-05T12:01:00+00:00",
):
    analysis = [_diamond_row()] if analysis is None else analysis
    flow = {"token-a": _flow_row()} if flow is None else flow
    generation = paths["generations"] / generation_id
    generation.mkdir(parents=True, exist_ok=True)
    _write(generation / "diamond_analysis_v3.json", analysis)
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


def _run_and_output(paths):
    risk.run_once(verbose=False)
    return _read(paths["risk"])


def test_rf4a_1_pass_result_contains_identity_fields(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    row = _diamond_row()
    _publish_generation(paths, analysis=[row])

    result = _run_and_output(paths)["results"][0]

    assert result["risk_ok"] is True
    assert result["condition_id"] == row["condition_id"]
    assert result["token_id"] == row["token_id"]
    assert result["outcome"] == row["outcome"]
    assert result["question"] == row["question"]
    assert result["direction"] == row["direction"]
    assert result["price"] == row["price"]


def test_rf4a_2_pass_result_contains_risk_fields(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths)

    result = _run_and_output(paths)["results"][0]

    assert result["risk_ok"] is True
    assert result["decision"] == "PASS"
    assert result["reason_codes"] == []


def test_rf4a_3_pass_result_contains_exact_evidence_triple_from_flow_x(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    evidence = {
        "evidence_id": "0x2222222222222222222222222222222222222222222222222222222222222222:9",
        "evidence_cursor": [777, 9],
        "evidence_at": "2026-10-05T12:00:09+00:00",
    }
    flow = {"token-a": _flow_row(**evidence)}
    _publish_generation(paths, "X", flow=flow)

    result = _run_and_output(paths)["results"][0]

    assert result["evidence_id"] == evidence["evidence_id"]
    assert result["evidence_cursor"] == evidence["evidence_cursor"]
    assert result["evidence_at"] == evidence["evidence_at"]


def test_rf4a_4_root_flow_y_cannot_replace_generation_x_evidence(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    flow_x = {
        "token-a": _flow_row(
            evidence_id="0x1111111111111111111111111111111111111111111111111111111111111111:1",
            evidence_cursor=[100, 1],
            evidence_at="2026-10-05T12:00:00+00:00",
        )
    }
    _publish_generation(paths, "X", flow=flow_x)

    _write(
        paths["root_flow"],
        {
            "token-a": _flow_row(
                evidence_id="0x3333333333333333333333333333333333333333333333333333333333333333:99",
                evidence_cursor=[999, 99],
                evidence_at="2026-10-05T12:59:59+00:00",
            )
        },
    )

    result = _run_and_output(paths)["results"][0]

    assert result["evidence_id"] == "0x1111111111111111111111111111111111111111111111111111111111111111:1"
    assert result["evidence_cursor"] == [100, 1]
    assert result["evidence_at"] == "2026-10-05T12:00:00+00:00"


def test_rf4a_5_v3_ranking_fields_are_passthrough(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    row = _diamond_row(
        is_diamond=True,
        cashflow_active=False,
        signal_quality=87.75,
        verification=79.5,
    )
    _publish_generation(paths, analysis=[row])

    result = _run_and_output(paths)["results"][0]

    assert result["is_diamond"] is True
    assert result["cashflow_active"] is False
    assert result["signal_quality"] == 87.75
    assert result["verification"] == 79.5


def test_rf4a_6_no_numeric_focus_score_is_created(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths)

    result = _run_and_output(paths)["results"][0]

    for forbidden in (
        "score",
        "focus_score",
        "composite_score",
        "diamond_quality",
        "candidate_rank",
    ):
        assert forbidden not in result


def test_rf4a_7_candidate_order_preserves_upstream_v3_order(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    rows = [
        _diamond_row("token-a", question="A", cashflow_active=True, signal_quality=95, verification=90),
        _diamond_row("token-b", condition_id="condition-b", question="B", cashflow_active=False, signal_quality=99, verification=99),
        _diamond_row("token-c", condition_id="condition-c", question="C", cashflow_active=False, signal_quality=80, verification=70),
    ]
    flows = {
        "token-a": _flow_row("token-a"),
        "token-b": _flow_row("token-b", condition_id="condition-b"),
        "token-c": _flow_row("token-c", condition_id="condition-c"),
    }
    _publish_generation(paths, analysis=rows, flow=flows)

    output = _run_and_output(paths)

    assert [row["token_id"] for row in output["results"]] == ["token-a", "token-b", "token-c"]


def test_rf4a_8_exact_ranking_tie_preserves_upstream_order(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    rows = [
        _diamond_row("token-b", condition_id="condition-b", question="B", cashflow_active=True, signal_quality=90, verification=80),
        _diamond_row("token-a", condition_id="condition-a", question="A", cashflow_active=True, signal_quality=90, verification=80),
    ]
    flows = {
        "token-b": _flow_row("token-b", condition_id="condition-b"),
        "token-a": _flow_row("token-a", condition_id="condition-a"),
    }
    _publish_generation(paths, analysis=rows, flow=flows)

    output = _run_and_output(paths)

    assert [row["token_id"] for row in output["results"]] == ["token-b", "token-a"]


def test_rf4a_9_block_semantics_remain_unchanged(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    row = _diamond_row(direction="SELL")
    flow = {"token-a": _flow_row()}
    _publish_generation(paths, analysis=[row], flow=flow)

    result = _run_and_output(paths)["results"][0]

    assert result["risk_ok"] is False
    assert result["decision"] == "BLOCK"
    assert "DIRECTION_NOT_BUY" in result["reason_codes"]


def test_rf4a_10_missing_flow_evidence_does_not_fabricate_evidence(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    flow = _flow_row()
    flow.pop("evidence_id")
    flow.pop("evidence_cursor")
    flow.pop("evidence_at")
    _publish_generation(paths, flow={"token-a": flow})

    result = _run_and_output(paths)["results"][0]

    assert result["risk_ok"] is False
    assert result["decision"] == "BLOCK"
    assert "EVIDENCE_MISSING" in result["reason_codes"]
    assert result.get("evidence_id") is None
    assert result.get("evidence_cursor") is None
    assert result.get("evidence_at") is None


def test_rf4a_11_ac1b_same_x_noop_remains_intact(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    _publish_generation(paths, "X")

    risk.run_once(verbose=False)
    first = paths["risk"].read_bytes()

    def forbidden_write(*args, **kwargs):
        raise AssertionError("same X must remain a no-op")

    monkeypatch.setattr(risk, "save_json_atomic", forbidden_write)
    risk.run_once(verbose=False)

    assert paths["risk"].read_bytes() == first


def test_rf4a_12_ac1b_generation_consistency_remains_intact(monkeypatch, tmp_path):
    paths = _configure_paths(monkeypatch, tmp_path)
    row_x = _diamond_row()
    row_x["generation_marker"] = "X"
    flow_x = _flow_row()
    flow_x["generation_marker"] = "X"
    _publish_generation(paths, "X", analysis=[row_x], flow={"token-a": flow_x})

    root_row = _diamond_row()
    root_row["generation_marker"] = "ROOT-Y"
    root_flow = _flow_row()
    root_flow["generation_marker"] = "ROOT-Z"
    _write(paths["root_analysis"], [root_row])
    _write(paths["root_flow"], {"token-a": root_flow})

    seen = {}

    real_assess = risk.assess

    def capture(row, flow_state, now=None):
        seen["analysis"] = row["generation_marker"]
        seen["flow"] = flow_state[row["token_id"]]["generation_marker"]
        return real_assess(row, flow_state, now=now)

    monkeypatch.setattr(risk, "assess", capture)
    output = _run_and_output(paths)

    assert seen == {"analysis": "X", "flow": "X"}
    assert output["source_generation_id"] == "X"
