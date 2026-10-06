import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from machine_common import fresh, finite_number, save_json_atomic

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"

ANALYSIS_FILE = DATA_DIR / "diamond_analysis_v3.json"
FLOW_FILE = DATA_DIR / "flow_state.json"
RISK_FILE = DATA_DIR / "risk_assessment.json"

SCHEMA_VERSION = 4
MAX_LARGEST_TRADE_RATIO = 0.70
REQUIRED_CONFIRMATIONS = 3


def load_json(path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        print(f"[RISK] Could not read {path.name}: {exc}")
    return default


def generation_manifest_file():
    return DATA_DIR / "diamond_generation.json"


def generation_dir(generation_id):
    return DATA_DIR / "diamond_generations" / generation_id


def load_required_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def existing_risk_output():
    try:
        if RISK_FILE.exists():
            with RISK_FILE.open("r", encoding="utf-8") as f:
                value = json.load(f)
                return value if isinstance(value, dict) else None
    except Exception:
        return None
    return None


def text(value):
    return "" if value is None else str(value).strip()


def number(value, default=None):
    return finite_number(value, default)


def flow_for(row, flow_state):
    token_id = text(row.get("token_id"))
    if not token_id or not isinstance(flow_state, dict):
        return None
    candidate = flow_state.get(token_id)
    return candidate if isinstance(candidate, dict) else None


def assess(row, flow_state, now=None):
    """Deterministic veto assessment. `now` is injectable for replay."""
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat()

    reasons = []
    codes = []
    warnings = []

    def block(reason, code):
        reasons.append(reason)
        codes.append(code)

    token_id = text(row.get("token_id"))
    condition_id = text(row.get("condition_id"))
    outcome = text(row.get("outcome"))
    direction = text(row.get("direction")).upper()
    classification = text(row.get("classification")).upper()

    price = number(row.get("price"))
    schema_version = row.get("schema_version")

    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}

    resolution = row.get("resolution")
    if not isinstance(resolution, dict):
        resolution = {}

    cashflow = row.get("cashflow_alert")
    if not isinstance(cashflow, dict):
        cashflow = {}

    why_not = row.get("why_not_diamond")
    if not isinstance(why_not, list):
        why_not = []

    flow = flow_for(row, flow_state)

    if classification != "DIAMOND":
        block("Classification is not DIAMOND", "NOT_DIAMOND")

    if row.get("diamond") is not True:
        block("Diamond flag is not true", "NOT_DIAMOND")

    if why_not:
        block("Diamond still has blockers", "DIAMOND_BLOCKERS")

    if not condition_id:
        block("condition_id missing", "IDENTITY_MISSING")
    if not token_id:
        block("token_id missing", "IDENTITY_MISSING")
    if not outcome:
        block("outcome missing", "IDENTITY_MISSING")

    if schema_version != SCHEMA_VERSION:
        block(f"Unsupported schema_version: {schema_version}", "SCHEMA_MISMATCH")

    if not fresh(row.get("source_updated_at"), now=now):
        block("Diamond source data stale or missing", "STALE_DIAMOND")
    if not fresh(row.get("last_trade_at"), now=now):
        block("Latest trade stale or missing", "STALE_TRADE")

    if direction != "BUY":
        block(f"Direction {direction or 'UNKNOWN'} not eligible", "DIRECTION_NOT_BUY")

    if price is None or not 0 < price < 1:
        block("Invalid market price", "PRICE_INVALID")

    confirmations = int(number(metrics.get("confirmations"), 0) or 0)
    verified = metrics.get("verified") is True

    if confirmations < REQUIRED_CONFIRMATIONS:
        block(
            f"Only {confirmations}/{REQUIRED_CONFIRMATIONS} confirmations",
            "CONFIRMATIONS_LOW",
        )
    if not verified:
        block("Diamond verification is not VERIFIED", "NOT_VERIFIED")

    largest_ratio = number(metrics.get("largest_trade_ratio"))
    if largest_ratio is None:
        block("Largest-trade ratio unavailable", "CONCENTRATION_UNKNOWN")
    elif not 0 <= largest_ratio <= 1:
        block("Largest-trade ratio outside [0,1]", "CONCENTRATION_INVALID")
    elif largest_ratio >= MAX_LARGEST_TRADE_RATIO:
        block(
            f"Whale concentration too high ({largest_ratio:.2f})",
            "WHALE_CONCENTRATION",
        )

    if cashflow.get("quality") == "WHALE_DOMINATED":
        block("Cashflow is whale dominated", "WHALE_DOMINATED")

    remaining = number(resolution.get("remaining_seconds"))
    if remaining is None:
        block("Resolution timing unavailable", "RESOLUTION_UNKNOWN")
    elif remaining <= 0:
        block("Market expired", "MARKET_EXPIRED")

    if flow is None:
        block("Matching token flow state missing", "FLOW_MISSING")
    else:
        flow_token = text(flow.get("token_id"))
        flow_condition = text(flow.get("condition_id"))
        flow_outcome = text(flow.get("outcome"))

        if flow_token != token_id:
            block("Flow token_id mismatch", "IDENTITY_MISMATCH")
        if flow_condition != condition_id:
            block("Flow condition_id mismatch", "IDENTITY_MISMATCH")
        if flow_outcome.lower() != outcome.lower():
            block("Flow outcome mismatch", "IDENTITY_MISMATCH")

        if flow.get("schema_version") != SCHEMA_VERSION:
            block("Flow schema mismatch", "SCHEMA_MISMATCH")

        if not fresh(flow.get("source_updated_at"), now=now):
            block("Flow source stale", "STALE_FLOW")
        if not fresh(flow.get("last_trade_at"), now=now):
            block("Flow latest trade stale or missing", "STALE_FLOW_TRADE")

        market = flow.get("market")
        if not isinstance(market, dict):
            market = {}

        if market.get("active") is not True:
            block("Market not confirmed active", "MARKET_NOT_ACTIVE")
        if market.get("closed") is not False:
            block("Market closed status unsafe", "MARKET_CLOSED_UNSAFE")
        if market.get("accepting_orders") is not True:
            block("Market not accepting orders", "MARKET_NOT_ACCEPTING")

        if flow.get("verified") is not True:
            block("Flow is not VERIFIED", "FLOW_NOT_VERIFIED")

        flow_confirmations = int(number(flow.get("confirmations"), 0) or 0)
        if flow_confirmations < REQUIRED_CONFIRMATIONS:
            block(
                f"Flow has only {flow_confirmations}/{REQUIRED_CONFIRMATIONS} confirmations",
                "FLOW_CONFIRMATIONS_LOW",
            )

        flow_direction = text(flow.get("direction")).upper()
        if not flow_direction:
            block("Flow direction missing", "FLOW_DIRECTION_UNKNOWN")
        elif flow_direction != direction:
            block(
                f"Flow direction {flow_direction} diverges from Diamond direction {direction}",
                "FLOW_DIRECTION_DIVERGENT",
            )

        flow_remaining = number(flow.get("remaining_seconds"))
        if flow_remaining is None:
            block("Flow resolution timing unavailable", "FLOW_RESOLUTION_UNKNOWN")
        elif flow_remaining <= 0:
            block("Flow says market expired", "FLOW_MARKET_EXPIRED")

    risk_ok = not reasons
    return {
        "checked_at": checked_at,
        "market_key": row.get("market_key"),
        "question": row.get("question"),
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome,
        "direction": direction,
        "price": price,
        "classification": classification,
        "risk_ok": risk_ok,
        "decision": "PASS" if risk_ok else "BLOCK",
        "reasons": reasons,
        "reason_codes": list(dict.fromkeys(codes)),
        "warnings": warnings,
    }



def focus_candidate_result(row, flow_state, risk_result):
    """Attach frozen RF-4A Focus contract data without changing Risk semantics."""
    result = dict(risk_result)

    flow = flow_for(row, flow_state)
    if isinstance(flow, dict):
        for field in ("evidence_id", "evidence_cursor", "evidence_at"):
            if field in flow:
                result[field] = flow[field]

    cashflow = row.get("cashflow_alert")
    if not isinstance(cashflow, dict):
        cashflow = {}

    scores = row.get("scores")
    if not isinstance(scores, dict):
        scores = {}

    result["is_diamond"] = row.get("diamond")
    result["cashflow_active"] = cashflow.get("active")
    result["signal_quality"] = scores.get("signal_quality")
    result["verification"] = scores.get("verification")

    return result

def run_once(verbose=True):
    # Bind exactly one Diamond generation at the start of the run.
    try:
        manifest = load_required_json(generation_manifest_file())
    except Exception as exc:
        if verbose:
            print(f"[RISK] Diamond generation not ready ({type(exc).__name__})")
        return []

    if not isinstance(manifest, dict):
        if verbose:
            print("[RISK] Diamond generation manifest invalid")
        return []

    generation_id = text(manifest.get("generation_id"))
    source_generated_at = manifest.get("published_at")
    if not generation_id or not source_generated_at:
        if verbose:
            print("[RISK] Diamond generation manifest incomplete")
        return []

    previous = existing_risk_output()
    if isinstance(previous, dict) and previous.get("source_generation_id") == generation_id:
        results = previous.get("results")
        return results if isinstance(results, list) else []

    bound_dir = generation_dir(generation_id)
    try:
        analysis = load_required_json(bound_dir / "diamond_analysis_v3.json")
        flow_state = load_required_json(bound_dir / "flow_state.json")
    except Exception as exc:
        if verbose:
            print(f"[RISK] Diamond generation {generation_id} incomplete ({type(exc).__name__})")
        return []

    # A valid empty generation is [] + {}. Wrong top-level shapes fail closed.
    if not isinstance(analysis, list) or not isinstance(flow_state, dict):
        if verbose:
            print(f"[RISK] Diamond generation {generation_id} has invalid shapes")
        return []

    results = [
        focus_candidate_result(row, flow_state, assess(row, flow_state))
        for row in analysis
        if isinstance(row, dict)
    ]
    passed = [row for row in results if row["risk_ok"]]

    payload = {
        "source_generation_id": generation_id,
        "source_generated_at": source_generated_at,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets_checked": len(results),
        "passed": len(passed),
        "results": results,
    }

    # Successful atomic output publication is the only processed marker.
    save_json_atomic(RISK_FILE, payload)

    if verbose:
        print("=" * 72)
        print("DIAMOND INTELLIGENCE - RISK ENGINE V2")
        print("=" * 72)
        print(f"Source generation: {generation_id}")
        print(f"Markets checked: {len(results)}")
        print(f"Risk PASS: {len(passed)}")
        print(f"Risk BLOCK: {len(results) - len(passed)}")
        for row in passed[:10]:
            print(f"PASS | {row['question']} | {row['outcome']} | {row['price']}")

    return results


def self_test():
    now = datetime.now(timezone.utc)

    flow = {
        "schema_version": 4,
        "token_id": "yes-token",
        "condition_id": "condition",
        "outcome": "Yes",
        "direction": "BUY",
        "verified": True,
        "confirmations": 3,
        "source_updated_at": now.isoformat(),
        "last_trade_at": now.isoformat(),
        "remaining_seconds": 86400,
        "market": {"active": True, "closed": False, "accepting_orders": True},
    }

    row = {
        "schema_version": 4,
        "source_updated_at": now.isoformat(),
        "last_trade_at": now.isoformat(),
        "market_key": "condition:condition|outcome:yes",
        "question": "SELF TEST",
        "condition_id": "condition",
        "token_id": "yes-token",
        "outcome": "Yes",
        "direction": "BUY",
        "price": 0.42,
        "classification": "DIAMOND",
        "diamond": True,
        "why_not_diamond": [],
        "metrics": {"verified": True, "confirmations": 3, "largest_trade_ratio": 0.20},
        "resolution": {"remaining_seconds": 86400},
        "cashflow_alert": {"quality": "BROAD"},
    }

    state = {"yes-token": flow}
    passed = 0
    total = 7

    def check(name, r, f, expected):
        nonlocal passed
        result = assess(r, f, now=now)["decision"]
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] {name}: {result} (expected {expected})")
        assert result == expected, name
        passed += 1

    print("RISK ENGINE SELF-TEST (V2)")
    check("1. Clean Diamond + fresh Flow", row, state, "PASS")

    stale = dict(row)
    stale["source_updated_at"] = "2000-01-01T00:00:00Z"
    check("2. Stale Diamond evidence", stale, state, "BLOCK")

    sell = dict(row)
    sell["direction"] = "SELL"
    check("3. SELL direction", sell, state, "BLOCK")

    whale = dict(row)
    whale["metrics"] = dict(row["metrics"])
    whale["metrics"]["largest_trade_ratio"] = 0.80
    check("4. Whale dominated (Diamond)", whale, state, "BLOCK")

    flow_div_conf = dict(flow)
    flow_div_conf["confirmations"] = 2
    check("5. Diamond 3/3, Flow 2/3", row, {"yes-token": flow_div_conf}, "BLOCK")

    flow_div_dir = dict(flow)
    flow_div_dir["direction"] = "SELL"
    check("6. Diamond BUY, Flow SELL", row, {"yes-token": flow_div_dir}, "BLOCK")

    flow_unverified = dict(flow)
    flow_unverified["verified"] = False
    check("7. Diamond verified, Flow unverified", row, {"yes-token": flow_unverified}, "BLOCK")

    print(f"RISK ENGINE SELF-TEST OK (V2) - {passed}/{total} cases passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    run_once()


if __name__ == "__main__":
    main()
