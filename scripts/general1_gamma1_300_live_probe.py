"""Fixed 300-second isolated GENERAL1-GAMMA-1 throughput comparison.

The isolation gate remains authoritative. No replay or deletion of old sandboxes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import general1_e2e2_live_probe as live


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and (len(argv) != 2 or argv[0] != "--source-env"):
        print("[BLOCKED] Only an external --source-env path is accepted.")
        return 3
    previous_name, previous_report = live.EXPECTED_NAME, live.REPORT_NAME
    try:
        live.EXPECTED_NAME = "GENERAL1_GAMMA1_300_LIVE_SANDBOX"
        live.REPORT_NAME = "general1_gamma1_300_live_report.json"
        return live.main(["--near-tip", "--gamma-batch", "--duration", "300", *argv])
    finally:
        live.EXPECTED_NAME, live.REPORT_NAME = previous_name, previous_report


if __name__ == "__main__":
    raise SystemExit(main())
