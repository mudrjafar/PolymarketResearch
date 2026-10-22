"""GENERAL1-E2E-2 safety and reporting tests: no RPC or live worker startup."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import general1_e2e2_live_probe as live


def sandbox(tmp_path):
    p = tmp_path / live.EXPECTED_NAME
    p.mkdir()
    (p / ".git").mkdir()
    return p


def test_isolation_gate_only_accepts_fresh_dedicated_clone(tmp_path):
    p = sandbox(tmp_path)
    assert live.isolation_gate(p, parent=tmp_path) is True
    (p / "data").mkdir()
    with pytest.raises(live.GateError, match="already contains data"):
        live.isolation_gate(p, parent=tmp_path)


def test_isolation_gate_rejects_wrong_parent_and_non_git_root(tmp_path):
    p = sandbox(tmp_path)
    with pytest.raises(live.GateError, match="approved"):
        live.isolation_gate(p, parent=tmp_path / "other")
    (p / ".git").rmdir()
    with pytest.raises(live.GateError, match="Git clone"):
        live.isolation_gate(p, parent=tmp_path)


def test_no_env_file_is_allowed_inside_sandbox(tmp_path):
    p = sandbox(tmp_path)
    (p / ".env").write_text("POLYMARKET_RPC_URL=https://invalid.example/", encoding="utf-8")
    with pytest.raises(live.GateError, match="Do not copy credentials"):
        live.isolation_gate(p, parent=tmp_path)


def test_rpc_url_only_loaded_from_external_file_not_stored(tmp_path, monkeypatch):
    p = sandbox(tmp_path)
    cfg = tmp_path / "secrets.env"
    cfg.write_text('# comment\nPOLYMARKET_RPC_URL="https://provider.example/test-secret"\nTELEGRAM_BOT_TOKEN=secret\n', encoding="utf-8")
    monkeypatch.setattr(live, "ROOT", p)
    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    assert live.get_rpc(cfg) == "https://provider.example/test-secret"
    assert not (p / ".env").exists()
    assert not (p / "data").exists()


def test_rpc_configuration_does_not_accept_insecure_url(tmp_path, monkeypatch):
    p = sandbox(tmp_path)
    monkeypatch.setattr(live, "ROOT", p)
    monkeypatch.setenv("POLYMARKET_RPC_URL", "http://untrusted.example/token")
    with pytest.raises(live.GateError, match="HTTPS"):
        live.get_rpc(None)


def test_rpc_error_never_raises_with_credential_url(monkeypatch):
    class Error:
        def __call__(self, *args, **kwargs):
            raise live.requests.ConnectionError("secret-in-url-token")
    monkeypatch.setattr(live.requests, "post", Error())
    with pytest.raises(live.GateError) as exc:
        live.rpc_number("https://provider.example/secret-in-url-token", "eth_chainId")
    assert "secret-in-url-token" not in str(exc.value)



def test_windows_cp1252_market_title_does_not_crash(tmp_path, monkeypatch):
    """The actual child Python interpreter must emit Unicode safely."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252:strict")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "private-chat-id")
    rpc = "https://provider.example/secret"
    child_env = live.worker_process_environment(rpc)
    assert child_env["PYTHONIOENCODING"] == "utf-8:backslashreplace"
    assert child_env["POLYMARKET_RPC_URL"] == rpc
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "TELEGRAM_CHAT_ID" not in child_env

    child = subprocess.run(
        [sys.executable, "-c", 'print("MARKET: 市場 🏀"); print(chr(0xD800))'],
        capture_output=True, env=child_env, timeout=10,
    )
    assert child.returncode == 0
    assert child.stdout.decode("utf-8").splitlines() == [
        "MARKET: 市場 🏀", r"\ud800",
    ]



def test_metric_rates_are_measured_not_assumed():
    rows = [
        {"status": "SUCCESS", "blocks": 5, "lag_before": 100, "lag_after": 95, "batch_ms": 110, "trade_count": 1,
         "rpc_retries": 2, "gamma_lookup_count": 1},
        {"status": "RETRYABLE_FAILURE", "blocks": 5, "lag_before": 95, "lag_after": 95, "batch_ms": 500},
        {"status": "SUCCESS", "blocks": 5, "lag_before": 95, "lag_after": 90, "batch_ms": 120, "trade_count": 0},
    ]
    result = live.checkpoint_metrics(rows, chain_start=1000, chain_end=1004, seconds=20.0)
    assert result["blocks_processed"] == 10
    assert result["collector_blocks_per_second"] == .5
    assert result["chain_blocks_per_second"] == .2
    assert result["lag_end"] == 90
    assert result["catchup_observed"] is True
    assert result["retryable_failures"] == 1
    assert result["rpc_retries"] == 2


def test_equal_or_lower_collector_rate_cannot_be_catchup():
    rows = [{"status": "SUCCESS", "blocks": 1, "lag_before": 10, "lag_after": 10, "batch_ms": 100}]
    result = live.checkpoint_metrics(rows, 100, 102, 10)
    assert result["catchup_observed"] is False


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_pipeline_proof_requires_actual_db_trade_and_generation_risk_binding(tmp_path):
    data = tmp_path
    generation = "g1"
    write(data / "diamond_generation.json", {"generation_id": generation})
    write(data / "diamond_generations" / generation / "flow_state.json", {
        "tok": {"token_id": "tok", "condition_id": "cid"},
    })
    write(data / "diamond_generations" / generation / "diamond_analysis_v3.json", [
        {"token_id": "tok", "condition_id": "cid", "diamond": False},
    ])
    write(data / "risk_assessment.json", {
        "source_generation_id": generation,
        "results": [{"token_id": "tok", "condition_id": "cid", "decision": "BLOCK"}],
    })
    write(data / "paper_state.json", {"positions": []})
    matching = {"_evidence": [("tok", "cid")]}
    proof = live.pipeline_summary(data, matching)
    assert proof["real_trade_to_risk_binding_observed"] is True
    proof = live.pipeline_summary(data, {"_evidence": []})
    assert proof["real_trade_to_risk_binding_observed"] is False
    write(data / "risk_assessment.json", {"source_generation_id": "wrong", "results": [{"token_id": "tok", "condition_id": "cid"}]})
    assert live.pipeline_summary(data, matching)["real_trade_to_risk_binding_observed"] is False


def test_missing_db_is_not_invented(tmp_path):
    assert live.db_summary(tmp_path / "missing.sqlite3") == {"exists": False}


def test_summary_contains_only_nonsensitive_collector_fields(tmp_path):
    path = tmp_path / "collector.log"
    path.write_text('network URL https://secret.example/token\n[LIVE-1C] {"status":"SUCCESS","blocks":5}\n', encoding="utf-8")
    records = live.diagnostic_records(path)
    assert records == [{"status": "SUCCESS", "blocks": 5}]
    assert "secret" not in json.dumps(records)


def test_runner_fails_closed_before_any_start_when_not_in_dedicated_root(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(live, "ROOT", tmp_path / "not_the_live_sandbox")
    monkeypatch.setenv("POLYMARKET_RPC_URL", "https://provider.example/secret")
    result = live.main(["--duration", "60"])
    assert result == 3
    assert "secret" not in capsys.readouterr().out

def test_live_wrapper_invokes_supervisor_and_shutdown_with_fakes(tmp_path, monkeypatch):
    """Full runner control-flow without real processes, network or credentials."""
    p = sandbox(tmp_path)
    monkeypatch.setattr(live, "ROOT", p)
    original_gate = live.isolation_gate
    monkeypatch.setattr(live, "isolation_gate", lambda root: original_gate(root, parent=tmp_path))
    monkeypatch.setattr(live.subprocess, "run", lambda *args, **kwargs: None)
    head = iter((1000, 1005))
    monkeypatch.setattr(live, "rpc_number", lambda _, method: 137 if method == "eth_chainId" else next(head))
    monkeypatch.setattr(live.run_machine, "build_services", lambda: [
        live.ServiceSpec(name, (name,)) for name in sorted(live.CORE)
    ])
    monkeypatch.setattr(live, "db_summary", lambda _: {
        "exists": True, "quick_check_ok": True, "_evidence": [("tok", "cid")],
        "checkpoint": 1000, "blocks": 10, "trades": 1,
    })
    monkeypatch.setattr(live, "diagnostic_records", lambda _: [
        {"status": "SUCCESS", "blocks": 10, "lag_before": 20, "lag_after": 10},
    ])
    monkeypatch.setattr(live, "pipeline_summary", lambda *_: {
        "real_trade_to_risk_binding_observed": True, "paper_positions": 0,
    })

    class FakeSupervisor:
        stopped = False
        def __init__(self, specs, **kwargs):
            assert {x.name for x in specs} == set(live.CORE)
            assert all(x.enabled for x in specs)
        def start_all(self):
            pass
        def poll_once(self):
            return None
        def snapshot(self):
            return {
                "overall_status": "RUNNING",
                "services": {name: {
                    "status": "RUNNING", "restart_count": 0,
                    "consecutive_failures": 0, "critical": True, "enabled": True,
                } for name in live.CORE},
            }
        def shutdown(self):
            FakeSupervisor.stopped = True

    monotonic = iter([0, 1, 2, 90, 100, 110])
    monkeypatch.setattr(live, "RuntimeSupervisor", FakeSupervisor)
    monkeypatch.setattr(live.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    rc = live.run_live(60, "https://fake-rpc.example")
    report = json.loads((p / live.REPORT_NAME).read_text(encoding="utf-8"))
    assert FakeSupervisor.stopped is True
    assert rc == 0
    assert report["result"] == "PASS"
    assert report["historical_databases_touched"] is False
    assert "fake-rpc.example" not in json.dumps(report)
    assert (p / "general1_e2e2_status_samples.jsonl").exists()
