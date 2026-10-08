from scripts import diamond_filter_v3, risk_worker


def test_learning_failure_does_not_change_risk_worker_authority(monkeypatch, capsys):
    calls = []

    def fake_risk(verbose=False):
        calls.append(("risk", verbose))
        return []

    def broken_learning():
        raise RuntimeError("learning unavailable")

    monkeypatch.setattr(risk_worker.risk_engine, "run_once", fake_risk)
    monkeypatch.setattr(risk_worker.learning_ingest, "ingest_latest", broken_learning)

    risk_worker.risk_engine.run_once(verbose=False)
    risk_worker._ingest_learning()

    assert calls == [("risk", False)]
    output = capsys.readouterr().out
    assert "[LEARNING] ERROR: RuntimeError: learning unavailable" in output


def test_learning_provenance_failure_cannot_stop_diamond(monkeypatch, capsys):
    def broken_versions(_root):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(diamond_filter_v3, "strategy_versions", broken_versions)

    assert diamond_filter_v3.learning_versions_snapshot() is None
    assert "Learning provenance unavailable" in capsys.readouterr().out
