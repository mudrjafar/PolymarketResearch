from scripts import risk_worker


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
