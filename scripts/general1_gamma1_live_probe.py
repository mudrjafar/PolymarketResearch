"""GENERAL1-GAMMA-1: opt-in bounded legacy Gamma batch, isolated live observer.

No real orders, Paper requests, production databases or reused sandbox paths.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import general1_e2e2_live_probe as live


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if any(arg in ("--near-tip", "--gamma-batch", "--dashboard")
           for arg in argv):
        print("[BLOCKED] Freshness/optimization modes are fixed; dashboard disabled.")
        return 3
    old_name, old_report = live.EXPECTED_NAME, live.REPORT_NAME
    try:
        live.EXPECTED_NAME = "GENERAL1_GAMMA1_LIVE_SANDBOX"
        live.REPORT_NAME = "general1_gamma1_live_report.json"
        return live.main(["--near-tip", "--gamma-batch", *argv])
    finally:
        live.EXPECTED_NAME, live.REPORT_NAME = old_name, old_report


if __name__ == "__main__":
    raise SystemExit(main())
