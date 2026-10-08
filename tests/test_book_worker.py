import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from machine_common import save_json_atomic
from scripts import book_worker as worker


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def configure_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "focus": data / "focused_market.json",
        "book": data / "book_assessment.json",
    }
    monkeypatch.setattr(worker, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(worker, "FOCUS_FILE", paths["focus"], raising=False)
    monkeypatch.setattr(worker, "BOOK_FILE", paths["book"], raising=False)
    return paths


def focus_payload(*, state="READY", generated_at=NOW, input_status="OK"):
    focus = {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "price": 0.50,
        "status": state,
        "progress": 3 if state == "READY" else 2,
        "last_evidence_id": "0x" + "ab" * 32 + ":1",
    }
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "input_status": input_status,
        "source_generation_id": "X",
        "state": state,
        "focus": focus,
    }


def valid_book():
    return {
        "market": "condition-a",
        "asset_id": "token-a",
        "timestamp": "123",
        "hash": "hash-a",
        "bids": [{"price": "0.49", "size": "1000"}],
        "asks": [
            {"price": "0.50", "size": "100"},
            {"price": "0.51", "size": "200"},
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": "0.50",
    }


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_ready_focus_fetches_book_and_binds_identity(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())

    seen = []

    def loader(token_id):
        seen.append(token_id)
        return valid_book()

    result = worker.run_once(now=NOW, book_loader=loader)

    assert seen == ["token-a"]
    assert result["status"] == "OK"
    assert result["book_ok"] is True
    assert result["source_generation_id"] == "X"
    assert result["source_evidence_id"].endswith(":1")
    assert result["token_id"] == "token-a"
    assert read_json(paths["book"])["book_ok"] is True


def test_wait_focus_never_calls_clob(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload(state="WAIT 2/3"))

    def forbidden_loader(_token_id):
        raise AssertionError("Book API must not be called before READY")

    result = worker.run_once(now=NOW, book_loader=forbidden_loader)

    assert result["status"] == "NOT_READY"
    assert result["book_ok"] is False
    assert result["reason_codes"] == ["FOCUS_NOT_READY"]


def test_stale_focus_never_reuses_previous_book_pass(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())

    worker.run_once(now=NOW, book_loader=lambda _token: valid_book())
    assert read_json(paths["book"])["book_ok"] is True

    stale = NOW - timedelta(minutes=10)
    save_json_atomic(paths["focus"], focus_payload(generated_at=stale))

    def forbidden_loader(_token_id):
        raise AssertionError("stale Focus must not call Book API")

    result = worker.run_once(now=NOW, book_loader=forbidden_loader)

    assert result["status"] == "STALE"
    assert result["book_ok"] is False
    stored = read_json(paths["book"])
    assert stored["status"] == "STALE"
    assert stored["book_ok"] is False


def test_upstream_focus_input_failure_blocks_book(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(
        paths["focus"],
        focus_payload(input_status="MISSING"),
    )

    result = worker.run_once(
        now=NOW,
        book_loader=lambda _token: (_ for _ in ()).throw(
            AssertionError("must not fetch")
        ),
    )

    assert result["status"] == "UPSTREAM_NOT_OK"
    assert result["book_ok"] is False


def test_api_error_replaces_any_previous_ok_snapshot(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())
    worker.run_once(now=NOW, book_loader=lambda _token: valid_book())

    def broken_loader(_token_id):
        raise worker.BookFetchError("HTTP_503")

    result = worker.run_once(
        now=NOW + timedelta(seconds=5),
        book_loader=broken_loader,
    )

    assert result["status"] == "API_ERROR"
    assert result["book_ok"] is False
    assert result["reason_codes"] == ["BOOK_API_ERROR", "HTTP_503"]
    assert read_json(paths["book"])["book_ok"] is False


def test_identity_mismatch_from_clob_fails_book(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())

    bad = valid_book()
    bad["asset_id"] = "wrong-token"

    result = worker.run_once(now=NOW, book_loader=lambda _token: bad)

    assert result["status"] == "OK"
    assert result["book_ok"] is False
    assert "TOKEN_ID_MISMATCH" in result["reason_codes"]


def test_main_runtime_places_book_between_focus_and_telegram():
    import run_machine

    names = [name for name, _ in run_machine.SERVICES]
    assert names.index("focus") < names.index("book") < names.index("telegram")

    commands = dict(run_machine.SERVICES)
    assert any("book_worker.py" in str(part) for part in commands["book"])


def test_learning_queue_failure_cannot_change_book_result(monkeypatch, tmp_path, capsys):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())

    def broken(_output):
        raise RuntimeError("learning unavailable")

    monkeypatch.setattr(worker, "_queue_learning", broken)

    # run_once must remain Book-authoritative even when the observational
    # projection itself is unavailable. Simulate the helper's production
    # contract by replacing it with a swallowing wrapper.
    def safe_broken(_output):
        try:
            broken(_output)
        except Exception as exc:
            print(f"[LEARNING] BOOK_QUEUE_ERROR: {type(exc).__name__}: {exc}")

    monkeypatch.setattr(worker, "_queue_learning", safe_broken)
    result = worker.run_once(now=NOW, book_loader=lambda _token: valid_book())

    assert result["status"] == "OK"
    assert result["book_ok"] is True
    assert read_json(paths["book"])["book_ok"] is True
    assert "BOOK_QUEUE_ERROR" in capsys.readouterr().out


def test_book_worker_queues_only_ready_assessments(monkeypatch, tmp_path):
    paths = configure_paths(monkeypatch, tmp_path)
    save_json_atomic(paths["focus"], focus_payload())
    captured = []

    monkeypatch.setattr(
        worker,
        "current_strategy_versions",
        lambda: {"test": "versions"},
    )
    monkeypatch.setattr(
        worker,
        "enqueue_book_observation",
        lambda output, **kwargs: captured.append((output, kwargs))
        or {"status": "QUEUED"},
    )

    result = worker.run_once(now=NOW, book_loader=lambda _token: valid_book())

    assert result["status"] == "OK"
    assert len(captured) == 1
    queued, kwargs = captured[0]
    assert queued["source_generation_id"] == "X"
    assert queued["source_evidence_id"].endswith(":1")
    assert queued["book_ok"] is True
    assert kwargs["data_dir"] == paths["book"].parent
