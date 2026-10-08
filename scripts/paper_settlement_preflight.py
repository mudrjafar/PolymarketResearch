"""Read-only live acceptance preflight for Paper settlement.

This script never mutates Paper state, never signs, and never submits orders.
It verifies the configured Polygon RPC/finality path and can optionally prove
one known unresolved and one known resolved legacy CTF position.
"""

import argparse
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from scripts import paper_settlement


ZERO_CONDITION = "0x" + "00" * 32
SCHEMA_VERSION = 1


def _example(condition_id, token_id, outcome, label):
    values = (condition_id, token_id, outcome)
    present = [value is not None and str(value).strip() != "" for value in values]
    if any(present) and not all(present):
        raise ValueError(f"{label}_EXAMPLE_INCOMPLETE")
    if not any(present):
        return None
    return {
        "condition_id": str(condition_id).strip(),
        "token_id": str(token_id).strip(),
        "outcome": str(outcome).strip(),
    }


def _check_example(
    sample,
    expected_status,
    *,
    rpc_url,
    heimdall_url,
    finalized,
):
    identity = paper_settlement.resolve_position_identity(
        sample["condition_id"],
        sample["token_id"],
        rpc_url=rpc_url,
        block_tag=hex(finalized.number),
        validate_chain=False,
    )
    paper_settlement.validate_outcome_identity(
        sample["outcome"],
        identity["outcome_index"],
    )
    position = {
        "condition_id": sample["condition_id"],
        "token_id": sample["token_id"],
        "outcome": sample["outcome"],
        **identity,
    }
    result = paper_settlement.check_settlement(
        position,
        rpc_url=rpc_url,
        heimdall_url=heimdall_url,
    )
    actual = result.get("status") if isinstance(result, dict) else None
    return {
        "expected_status": expected_status,
        "actual_status": actual,
        "ok": actual == expected_status,
        "reason_code": result.get("reason_code") if isinstance(result, dict) else "INVALID_RESULT",
        "settlement_read_block": result.get("settlement_read_block") if isinstance(result, dict) else None,
        "settlement_read_block_hash": result.get("settlement_read_block_hash") if isinstance(result, dict) else None,
        "settlement_finality_source": result.get("settlement_finality_source") if isinstance(result, dict) else None,
    }


def run_preflight(
    *,
    rpc_url=None,
    heimdall_url=None,
    unresolved_example=None,
    resolved_example=None,
):
    rpc_url = str(rpc_url or os.getenv("POLYMARKET_RPC_URL", "")).strip()
    heimdall_url = str(
        heimdall_url
        if heimdall_url is not None
        else os.getenv("POLYMARKET_HEIMDALL_URL", "")
    ).strip()

    output = {
        "schema_version": SCHEMA_VERSION,
        "status": "SETTLEMENT_ACCEPTANCE_BLOCKED",
        "chain_id": None,
        "finalized_block": None,
        "finalized_block_hash": None,
        "finality_source": None,
        "historical_ctf_read_ok": False,
        "examples": {},
        "reason_code": None,
    }

    try:
        output["chain_id"] = paper_settlement._assert_polygon_chain(rpc_url)
        finalized = paper_settlement.get_finalized_block(
            rpc_url=rpc_url,
            heimdall_url=heimdall_url,
        )
        output["finalized_block"] = finalized.number
        output["finalized_block_hash"] = finalized.block_hash
        output["finality_source"] = finalized.source

        # A zero condition is intentionally synthetic. The CTF public mapping
        # getter should return an integer (normally zero). This proves that the
        # provider can execute a historical eth_call against the exact finalized
        # block without using a market lifecycle heuristic.
        denominator = paper_settlement._ctf_uint(
            "payoutDenominator(bytes32)",
            ["bytes32"],
            [bytes.fromhex(ZERO_CONDITION[2:])],
            rpc_url=rpc_url,
            block_tag=hex(finalized.number),
        )
        if not isinstance(denominator, int) or isinstance(denominator, bool):
            raise paper_settlement.SettlementSourceError(
                "HISTORICAL_CTF_READ_INVALID"
            )
        output["historical_ctf_read_ok"] = True

        if unresolved_example is not None:
            output["examples"]["unresolved"] = _check_example(
                unresolved_example,
                paper_settlement.UNRESOLVED,
                rpc_url=rpc_url,
                heimdall_url=heimdall_url,
                finalized=finalized,
            )

        if resolved_example is not None:
            output["examples"]["resolved"] = _check_example(
                resolved_example,
                paper_settlement.FINAL_SETTLED,
                rpc_url=rpc_url,
                heimdall_url=heimdall_url,
                finalized=finalized,
            )

        supplied = set(output["examples"])
        if supplied == {"unresolved", "resolved"}:
            if all(row["ok"] for row in output["examples"].values()):
                output["status"] = "SETTLEMENT_ACCEPTANCE_OK"
                return output, 0
            output["reason_code"] = "EXAMPLE_STATUS_MISMATCH"
            return output, 2

        output["status"] = "RPC_PREFLIGHT_OK"
        output["reason_code"] = "KNOWN_EXAMPLES_REQUIRED_FOR_FULL_ACCEPTANCE"
        return output, 0

    except (paper_settlement.SettlementSourceError, paper_settlement.SettlementIdentityError) as exc:
        output["reason_code"] = str(exc) or type(exc).__name__
        return output, 2
    except Exception as exc:
        output["reason_code"] = type(exc).__name__
        return output, 2


def _print_human(result):
    print("PAPER SETTLEMENT LIVE PREFLIGHT - READ ONLY")
    print(f"status={result.get('status')}")
    print(f"chain_id={result.get('chain_id')}")
    print(
        "finalized="
        f"{result.get('finalized_block')} "
        f"{result.get('finalized_block_hash')} "
        f"source={result.get('finality_source')}"
    )
    print(f"historical_ctf_read_ok={result.get('historical_ctf_read_ok')}")
    for label, row in (result.get("examples") or {}).items():
        print(
            f"{label}="
            f"{row.get('actual_status')} "
            f"expected={row.get('expected_status')} "
            f"ok={row.get('ok')}"
        )
    if result.get("reason_code"):
        print(f"reason={result.get('reason_code')}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--heimdall-url", default=None)
    parser.add_argument("--unresolved-condition")
    parser.add_argument("--unresolved-token")
    parser.add_argument("--unresolved-outcome")
    parser.add_argument("--resolved-condition")
    parser.add_argument("--resolved-token")
    parser.add_argument("--resolved-outcome")
    args = parser.parse_args(argv)

    try:
        unresolved = _example(
            args.unresolved_condition,
            args.unresolved_token,
            args.unresolved_outcome,
            "UNRESOLVED",
        )
        resolved = _example(
            args.resolved_condition,
            args.resolved_token,
            args.resolved_outcome,
            "RESOLVED",
        )
    except ValueError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "SETTLEMENT_ACCEPTANCE_BLOCKED",
            "reason_code": str(exc),
        }
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            _print_human(result)
        return 2

    result, code = run_preflight(
        heimdall_url=args.heimdall_url,
        unresolved_example=unresolved,
        resolved_example=resolved,
    )
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_human(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
