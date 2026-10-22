"""GENERAL1-FRESHNESS-1 one-shot isolated near-tip probe.

Uses the validated E2E-2 supervisor with a separate, never-reused sandbox
identity. No production database or .env is copied. No Paper requests/orders.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import general1_e2e2_live_probe as live

live.EXPECTED_NAME = "GENERAL1_FRESHNESS1_LIVE_SANDBOX"
live.REPORT_NAME = "general1_freshness1_live_report.json"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--near-tip" in argv:
        print("[BLOCKED] --near-tip is already enforced by this entry point.")
        return 3
    return live.main(["--near-tip", *argv])


if __name__ == "__main__":
    raise SystemExit(main())
