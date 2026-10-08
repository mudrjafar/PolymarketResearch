"""Resident Risk worker.

Keeps Risk as its own process while reusing the existing generation-bound
risk_engine.run_once() semantics.
"""

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from scripts import risk_engine
from scripts import learning_ingest

DEFAULT_INTERVAL_SECONDS = 5


def _ingest_learning():
    try:
        learning_ingest.ingest_latest()
    except Exception as exc:
        print(f"[LEARNING] ERROR: {type(exc).__name__}: {exc}")


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    while True:
        try:
            risk_engine.run_once(verbose=False)
            _ingest_learning()
        except Exception as exc:
            print(f"[RISK] ERROR: {type(exc).__name__}: {exc}")
        time.sleep(max(1, int(interval)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.once:
        risk_engine.run_once(verbose=True)
        _ingest_learning()
        return 0

    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
