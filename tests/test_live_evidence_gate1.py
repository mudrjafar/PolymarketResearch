"""No real RPC and no real trades: safety/contract tests for the live gate."""
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import live_evidence_gate1 as gate


def test_no_paper_or_wallet_process_in_observation():
    assert "paper" not in gate.SERVICES
    assert "telegram" not in gate.SERVICES
    assert "dashboard" not in gate.SERVICES
    assert set(gate.SERVICES) == {
        "collector", "flow", "diamond", "risk", "focus", "book", "learning"
    }


def test_missing_data_is_nonmutating_and_does_not_claim_profitability(tmp_path):
    result = gate.snapshot(tmp_path)
    assert result["collector"]["status"] == "MISSING"
    assert result["learning"]["status"] == "MISSING"
    assert result["market_profitability"] == "NOT_YET_MEASURABLE"
    assert list(tmp_path.iterdir()) == []


def test_existing_collector_is_read_only_and_no_raw_ids(tmp_path):
    dbpath = tmp_path / "collector.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.executescript("""
            CREATE TABLE checkpoint(chain_id INTEGER, block_number INTEGER);
            CREATE TABLE blocks(block_number INTEGER);
            CREATE TABLE trades(payload_json TEXT);
            INSERT INTO checkpoint VALUES (137, 10);
            INSERT INTO blocks VALUES (10);
        """)
        db.execute("INSERT INTO trades VALUES (?)", (json.dumps({
            "condition_id": "private-condition-token-id",
            "asset_type": "CTF_TOKEN"
        }),))
    before = dbpath.stat().st_size
    output = gate.snapshot(tmp_path)
    serialized = json.dumps(output)
    assert dbpath.stat().st_size == before
    assert output["collector"]["trades"] == 1
    assert output["collector"]["distinct_condition_ids"] == 1
    assert output["collector"]["asset_types"] == {"CTF_TOKEN": 1}
    assert "private-condition-token-id" not in serialized


def test_focus_and_risk_aggregation_redacts_identifiers(tmp_path):
    (tmp_path / "focus_events.jsonl").write_text(
        json.dumps({"type": "LOCKED", "token_id": "private-token"}) + "\n" +
        json.dumps({"type": "READY", "token_id": "private-token"}) + "\n",
        encoding="utf-8")
    (tmp_path / "risk_assessment.json").write_text(json.dumps({
        "results": [{"risk_ok": False, "reason_codes": [
            "STALE_TRADE", "rpc https://secret.example/api?a=b"
        ]}, {"risk_ok": True, "reason_codes": []}]
    }), encoding="utf-8")
    output = gate.snapshot(tmp_path)
    assert output["focus_events"] == {"LOCKED": 1, "READY": 1}
    assert output["risk_latest"]["block_reasons"] == {
        "STALE_TRADE": 1, "OTHER_REDACTED": 1}
    assert "private-token" not in json.dumps(output)
    assert "secret.example" not in json.dumps(output)


def test_disallow_live_in_normal_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BASE", tmp_path / "production")
    with pytest.raises(ValueError, match="FRESH_LIVEGATE_WORKTREE"):
        gate.live_gate(300, None)
    assert not (tmp_path / "production").exists()


def test_disallow_existing_sandbox_data(tmp_path, monkeypatch):
    sandbox = tmp_path / "test-livegate"
    data = sandbox / "data"
    data.mkdir(parents=True)
    (data / "paper_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gate, "BASE", sandbox)
    with pytest.raises(ValueError, match="DATA_ROOT_NOT_EMPTY"):
        gate.live_gate(300, None)


def test_invalid_rpc_never_printed(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=never-use\nPOLYMARKET_RPC_URL=http://wrong\n",
                        encoding="utf-8")
    with pytest.raises(ValueError, match="RPC_MISSING_OR_INVALID_HTTPS_URL"):
        gate._env_rpc(env_file)


def test_diagnostic_aggregation_never_exposes_tokens_or_urls(tmp_path):
    p = tmp_path / "collector.log"
    p.write_text(
        '[LIVE-1C] ' + json.dumps({
            "status": "SUCCESS", "blocks": 4, "trade_count": 2,
            "rpc_calls": 3, "rpc_retries": 1, "gamma_lookup_count": 5,
            "gamma_lookup_ms": 125.5, "outer_retries": 2,
            "gamma_misses": ["sensitive-wallet-id"],
            "samples": [{"wallet": "hidden"}],
        }) + "\n" +
        '[COLLECTOR] RETRYABLE_BATCH (RetryableCollectorError)' + "\n",
        encoding="utf-8",
    )
    result = gate._collector_diagnostics(p)
    assert result["batch_statuses"]["SUCCESS"] == 1
    assert result["total_metrics"]["rpc_retries"] == 1
    assert result["total_metrics"]["gamma_lookup_ms"] == 125.5
    assert result["total_metrics"]["gamma_miss_count"] == 1
    assert result["total_metrics"]["max_outer_retry_streak"] == 2
    assert "sensitive-wallet-id" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)


def test_preexisting_flow_paper_summary_is_sanitized(tmp_path):
    (tmp_path / "flow_state.json").write_text(json.dumps({
        "private-token": {"verified": True},
        "another-token": {"verified": False}
    }), encoding="utf-8")
    (tmp_path / "paper_state.json").write_text(json.dumps({
        "positions": [{"status": "OPEN", "token_id": "secret-id"}],
        "processed_request_ids": ["secret-request"]
    }), encoding="utf-8")
    (tmp_path / "paper_events.jsonl").write_text(
        json.dumps({"event": "OPEN", "token_id": "secret-id"}) + "\n",
        encoding="utf-8")
    result = gate.snapshot(tmp_path)
    assert result["flow_latest"] == {"tokens": 2, "verified": 1}
    assert result["paper_state_existing"]["position_count"] == 1
    assert result["paper_state_existing"]["processed_request_id_count"] == 1
    assert result["paper_existing_event_line_count"] == 1
    assert "secret-id" not in json.dumps(result)
    assert "secret-request" not in json.dumps(result)


def test_public_rpc_uses_same_identifiable_headers_as_provider_probe(monkeypatch):
    import io

    def fake_urlopen(req, timeout):
        assert req.get_header("User-agent") == "PolymarketResearch-ReadOnly-Evidence/1.0"
        assert req.get_header("Content-type") == "application/json"
        assert timeout == 10
        return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x89"}')

    monkeypatch.setattr(gate.request, "urlopen", fake_urlopen)
    assert gate._rpc_call("https://polygon.drpc.org", "eth_chainId") == 137


def test_fatal_and_gamma_reason_classification_never_leaks_raw_ids(tmp_path):
    path = tmp_path / "collector.log"
    rows = [
        "[COLLECTOR] Storage/chain history conflict: Gamma asset identity is unavailable for token PRIVATE_IDENTIFIER",
        "[LIVE-1C] " + json.dumps({"status": "FATAL_FAILURE",
            "gamma_misses": [
                {"token_id": "PRIVATE_IDENTIFIER", "lookup_succeeded": True,
                 "market_classification": "CLOSED_INACTIVE_HISTORICAL"},
                {"token_id": "PRIVATE_IDENTIFIER_2", "lookup_succeeded": False,
                 "market_classification": "LOOKUP_FAILED"}
            ]})
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    result = gate._collector_diagnostics(path)
    assert result["fatal_reason_classes"] == {
        "GAMMA_IDENTITY_OR_METADATA_CONFLICT": 1
    }
    assert result["gamma_lookup_categories"] == {
        "CLOSED_INACTIVE_HISTORICAL": 1, "LOOKUP_FAILED": 1
    }
    assert "PRIVATE_IDENTIFIER" not in json.dumps(result)


def test_isolated_worker_receives_explicit_collector_lookback_without_secrets(monkeypatch):
    monkeypatch.setenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", "30")
    monkeypatch.setenv("COLLECTOR_CONFIRMATION_BLOCKS", "20")
    monkeypatch.setenv("COLLECTOR_GAMMA_BATCH_LOOKUP", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-bot-token")
    monkeypatch.setenv("TRADING_PRIVATE_KEY", "private-wallet-key")
    child, settings = gate._build_worker_env("https://polygon.drpc.org")
    assert child["COLLECTOR_INITIAL_LOOKBACK_BLOCKS"] == "30"
    assert child["COLLECTOR_CONFIRMATION_BLOCKS"] == "20"
    assert settings["COLLECTOR_INITIAL_LOOKBACK_BLOCKS"] == 30
    assert "TELEGRAM_BOT_TOKEN" not in child
    assert "TRADING_PRIVATE_KEY" not in child
    assert "private-bot-token" not in json.dumps(child)


def test_observer_rejects_invalid_collector_scope(monkeypatch):
    monkeypatch.setenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", "0")
    with pytest.raises(ValueError, match="INVALID_COLLECTOR_OBSERVER_CONFIG"):
        gate._build_worker_env("https://polygon.drpc.org")
    monkeypatch.setenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", "3000")
    with pytest.raises(ValueError, match="INVALID_COLLECTOR_OBSERVER_CONFIG"):
        gate._build_worker_env("https://polygon.drpc.org")
    monkeypatch.setenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", "30")
    monkeypatch.setenv("COLLECTOR_GAMMA_BATCH_LOOKUP", "invalid")
    with pytest.raises(ValueError, match="INVALID_COLLECTOR_OBSERVER_CONFIG"):
        gate._build_worker_env("https://polygon.drpc.org")
