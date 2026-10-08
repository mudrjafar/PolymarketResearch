"""Resident LD-2 learning ingestion worker.

Consumes durable Risk queue snapshots and frozen Diamond generation artifacts.
It is observational only: no signal, Risk, Focus, Book, or Paper authority.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from scripts.learning_ingest import (
    finalize_ready_selection_from_paper_state,
    ingest_book_queue_file,
    ingest_focus_queue_file,
    ingest_paper_open_queue_file,
    ingest_queue_file,
)
from scripts.learning_store import LearningStore

DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "learning.sqlite3"
QUEUE_DIR = DATA_DIR / "learning_queue"
DEFAULT_INTERVAL_SECONDS = 5


def _generation_id(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if isinstance(payload, dict):
            value = str(payload.get("source_generation_id") or "").strip()
            return value or None
    except Exception:
        pass
    return None


def _focus_queue_sort_key(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        event = payload.get("event") if isinstance(payload, dict) else None
        event_at = str(event.get("event_at") or "") if isinstance(event, dict) else ""
        event_id = str(payload.get("focus_event_id") or "") if isinstance(payload, dict) else ""
        return (0 if event_at else 1, event_at, event_id, Path(path).name)
    except Exception:
        return (2, "", "", Path(path).name)


def run_once(*, data_dir=DATA_DIR, db_path=DB_FILE):
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    queue_dir = data_dir / "learning_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    outcomes = []
    with LearningStore(db_path) as store:
        already = store.ingested_generation_ids()

        for path in sorted(queue_dir.glob("risk_*.json")):
            generation_id = _generation_id(path)

            if generation_id and generation_id in already:
                try:
                    path.unlink()
                except OSError:
                    pass
                outcomes.append(
                    {
                        "status": "ALREADY_INGESTED",
                        "generation_id": generation_id,
                        "signals_ingested": 0,
                    }
                )
                continue

            try:
                result = ingest_queue_file(
                    path,
                    data_dir=data_dir,
                    store=store,
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "status": "ERROR",
                        "generation_id": generation_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "queue_file": path.name,
                    }
                )
                continue

            outcomes.append(result)
            if result.get("status") in {"INGESTED", "ALREADY_INGESTED"}:
                already.add(str(result.get("generation_id")))
                try:
                    path.unlink()
                except OSError:
                    pass

        # Focus lifecycle events are processed only after Risk generation
        # ingestion attempts, so LOCK lineage can resolve its signal history.
        for path in sorted(
            queue_dir.glob("focus_*.json"),
            key=_focus_queue_sort_key,
        ):
            try:
                result = ingest_focus_queue_file(path, store=store)
            except Exception as exc:
                outcomes.append(
                    {
                        "status": "ERROR",
                        "focus_event_id": None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "queue_file": path.name,
                    }
                )
                continue

            outcomes.append(result)
            if result.get("status") in {"INGESTED", "ALREADY_INGESTED"}:
                try:
                    path.unlink()
                except OSError:
                    pass

        # Book can race ahead of READY ingestion. A missing READY is therefore
        # retryable: keep the queue file until Focus lineage is available.
        for path in sorted(queue_dir.glob("book_*.json")):
            try:
                result = ingest_book_queue_file(path, store=store)
            except Exception as exc:
                outcomes.append(
                    {
                        "status": "ERROR",
                        "book_observation_id": None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "queue_file": path.name,
                    }
                )
                continue

            outcomes.append(result)
            if result.get("status") in {"INGESTED", "ALREADY_INGESTED"}:
                try:
                    path.unlink()
                except OSError:
                    pass

        # Paper OPEN attribution runs after READY/Book ingestion. Missing READY
        # remains retryable and never mutates Paper authority.
        for path in sorted(queue_dir.glob("paper_open_*.json")):
            try:
                result = ingest_paper_open_queue_file(path, store=store)
            except Exception as exc:
                outcomes.append(
                    {
                        "status": "ERROR",
                        "paper_open_snapshot_id": None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "queue_file": path.name,
                    }
                )
                continue

            outcomes.append(result)
            if result.get("status") in {"INGESTED", "ALREADY_INGESTED"}:
                try:
                    path.unlink()
                except OSError:
                    pass

        selection_result = finalize_ready_selection_from_paper_state(
            data_dir=data_dir,
            store=store,
        )
        if (
            selection_result.get("selected")
            or selection_result.get("finalized")
            or selection_result.get("deferred")
        ):
            outcomes.append(
                {
                    "status": "SELECTION_RECONCILED",
                    "reconciliation_status": selection_result.get("status"),
                    **selection_result,
                    "status": "SELECTION_RECONCILED",
                }
            )

    return outcomes


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    last_errors = set()

    while True:
        try:
            outcomes = run_once()
            current_errors = set()

            for row in outcomes:
                status = row.get("status")
                generation_id = row.get("generation_id") or "?"
                if status == "INGESTED" and row.get("paper_id"):
                    print(
                        f"[LEARNING] PAPER_OPEN paper={row.get('paper_id')} "
                        f"ready={row.get('ready_id')}"
                    )
                elif status == "SELECTION_RECONCILED":
                    print(
                        f"[LEARNING] SELECTION selected={row.get('selected', 0)} "
                        f"finalized={row.get('finalized', 0)} "
                        f"deferred={row.get('deferred', 0)}"
                    )
                elif status == "INGESTED" and row.get("book_decision_id"):
                    print(
                        f"[LEARNING] BOOK status={row.get('book_status')} "
                        f"ready={row.get('ready_id')} "
                        f"ok={row.get('book_ok')}"
                    )
                elif status == "INGESTED" and row.get("focus_event_id"):
                    print(
                        f"[LEARNING] FOCUS {row.get('event_type')} "
                        f"token={row.get('token_id')} "
                        f"event={row.get('focus_event_id')}"
                    )
                elif status == "INGESTED":
                    print(
                        f"[LEARNING] INGESTED generation={generation_id} "
                        f"signals={row.get('signals_ingested', 0)}"
                    )
                elif status == "ERROR":
                    signature = (
                        str(row.get("queue_file") or ""),
                        str(row.get("error") or ""),
                    )
                    current_errors.add(signature)
                    if signature not in last_errors:
                        print(
                            f"[LEARNING] ERROR generation={generation_id} "
                            f"{row.get('error')}"
                        )

            last_errors = current_errors
        except Exception as exc:
            print(f"[LEARNING] STORE_ERROR: {type(exc).__name__}: {exc}")

        time.sleep(max(1, int(interval)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.once:
        for result in run_once():
            print(result)
        return 0

    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
