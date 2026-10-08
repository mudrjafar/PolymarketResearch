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

from scripts.learning_ingest import ingest_queue_file
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
                if status == "INGESTED":
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
