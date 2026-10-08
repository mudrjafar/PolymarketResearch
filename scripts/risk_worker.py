"""Resident Risk worker.

Keeps Risk as its own process while reusing the existing generation-bound
risk_engine.run_once() semantics. LD-2 learning queue publication is
best-effort and never changes Risk authority or decisions.
"""

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from scripts import risk_engine

try:
    from scripts.learning_queue import enqueue_risk_snapshot
    _LEARNING_INIT_ERROR = None
except Exception as exc:  # Learning must never prevent Risk from starting.
    enqueue_risk_snapshot = None
    _LEARNING_INIT_ERROR = f"{type(exc).__name__}: {exc}"

DEFAULT_INTERVAL_SECONDS = 5


def _publish_learning_snapshot():
    if _LEARNING_INIT_ERROR is not None:
        return

    try:
        payload = risk_engine.existing_risk_output()
        if not isinstance(payload, dict):
            return
        versions = payload.get("strategy_versions")
        if not isinstance(versions, dict):
            return
        result = enqueue_risk_snapshot(
            payload,
            data_dir=risk_engine.DATA_DIR,
            strategy_versions=versions,
        )
        if result.get("status") == "QUEUED":
            print(
                f"[LEARNING] queued Risk generation "
                f"{result.get('generation_id')}"
            )
    except Exception as exc:
        # Learning is observational. A storage/versioning failure is logged
        # but can never turn a Risk PASS/BLOCK result into another decision.
        print(f"[LEARNING] QUEUE_ERROR: {type(exc).__name__}: {exc}")


def run_forever(interval=DEFAULT_INTERVAL_SECONDS):
    learning_init_reported = False

    while True:
        try:
            risk_engine.run_once(verbose=False)
            if _LEARNING_INIT_ERROR is not None and not learning_init_reported:
                print(f"[LEARNING] DISABLED: {_LEARNING_INIT_ERROR}")
                learning_init_reported = True
            else:
                _publish_learning_snapshot()
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
        if _LEARNING_INIT_ERROR is not None:
            print(f"[LEARNING] DISABLED: {_LEARNING_INIT_ERROR}")
        else:
            _publish_learning_snapshot()
        return 0

    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
