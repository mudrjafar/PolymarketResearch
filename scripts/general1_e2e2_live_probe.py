"""GENERAL1-E2E-2 isolated live runtime observer.

Only execute from a *new*, dedicated GENERAL1_E2E2_LIVE_SANDBOX clone.
Does not read or modify historical data. Uses the existing RuntimeSupervisor
and workers unchanged, and only publishes local, secret-free diagnostics.
No trading orders and no Paper OPEN/CLOSE requests are generated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_machine
from runtime_supervisor import RuntimeSupervisor, ServiceSpec

EXPECTED_NAME = "GENERAL1_E2E2_LIVE_SANDBOX"
DEFAULT_PARENT = Path(r"C:\PolymarketResearch")
CORE = frozenset(("collector", "flow", "diamond", "risk", "focus", "book", "paper"))
REPORT_NAME = "general1_e2e2_live_report.json"


class GateError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def isolation_gate(root, *, parent=DEFAULT_PARENT):
    root = Path(root)
    parent = Path(parent)
    if root.is_symlink() or root.name.casefold() != EXPECTED_NAME.casefold():
        raise GateError("Must run from the dedicated, non-symlinked live sandbox")
    if root.resolve().parent != parent.resolve():
        raise GateError("Sandbox is not under the approved independent parent")
    if not (root / ".git").is_dir():
        raise GateError("Sandbox must be a fresh Git clone")
    if (root / ".env").exists():
        raise GateError("Do not copy credentials into the sandbox")
    data = root / "data"
    if data.exists() or data.is_symlink():
        raise GateError("Sandbox already contains data; never reuse or overwrite it")
    return True


def env_value(file_path, key):
    """Read *only* the required key from an outside .env; never copy it."""
    if file_path is None:
        return None
    source = Path(file_path)
    if not source.is_file() or source.is_symlink():
        raise GateError("External RPC configuration file missing or unsafe")
    if ROOT.resolve() in source.resolve().parents:
        raise GateError("RPC configuration may not be inside the sandbox")
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        k, sep, value = stripped.partition("=")
        if sep and k.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def get_rpc(source_env):
    rpc = os.getenv("POLYMARKET_RPC_URL", "").strip() or env_value(
        source_env, "POLYMARKET_RPC_URL"
    )
    if not rpc or "YOUR-" in rpc.upper():
        raise GateError("Polygon RPC is not configured; no workers started")
    if not rpc.lower().startswith("https://"):
        raise GateError("RPC endpoint must use HTTPS")
    return rpc


def rpc_number(rpc, method):
    try:
        response = requests.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": []},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        value = data.get("result") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError("invalid RPC result")
        return int(value, 16)
    except (requests.RequestException, ValueError, TypeError) as exc:
        # Never render exception text: it could contain the full secret URL.
        raise GateError(f"Polygon RPC {method} failed ({type(exc).__name__})") from None


def safe_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return default


def db_summary(db):
    if not db.is_file():
        return {"exists": False}
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            cp = conn.execute("SELECT block_number FROM checkpoint WHERE chain_id=137").fetchone()
            blocks = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            evidence = []
            for (payload,) in conn.execute(
                "SELECT payload_json FROM trades ORDER BY block_number DESC, log_index DESC LIMIT 500"
            ):
                try:
                    row = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if (row.get("collector_version") == 4
                    and isinstance(row.get("condition_id"), str)
                    and isinstance(row.get("token_id"), str)
                    and isinstance(row.get("transaction_hash"), str)
                    and isinstance(row.get("log_index"), int)):
                    evidence.append((str(row["token_id"]), str(row["condition_id"])))
            return {
                "exists": True, "quick_check_ok": quick == "ok",
                "checkpoint": cp[0] if cp else None,
                "blocks": blocks, "trades": trades,
                "_evidence": evidence,
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"exists": True, "error_type": type(exc).__name__}


def diagnostic_records(log_file):
    out = []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                pos = line.find("[LIVE-1C] ")
                if pos >= 0:
                    try:
                        record = json.loads(line[pos + len("[LIVE-1C] "):])
                        if isinstance(record, dict):
                            out.append(record)
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def checkpoint_metrics(records, chain_start, chain_end, seconds):
    successes = [r for r in records if r.get("status") == "SUCCESS"]
    processed = sum(max(0, int(r.get("blocks") or 0)) for r in successes)
    lags_before = [r["lag_before"] for r in records if isinstance(r.get("lag_before"), int)]
    lags_after = [r["lag_after"] for r in records if isinstance(r.get("lag_after"), int)]
    rate = processed / seconds if seconds > 0 else None
    chain_rate = (chain_end - chain_start) / seconds if seconds > 0 and chain_end is not None else None
    lag_start = lags_before[0] if lags_before else None
    lag_end = lags_after[-1] if lags_after else None
    catchup = (
        None if None in (rate, chain_rate, lag_start, lag_end)
        else (rate > chain_rate and lag_end <= lag_start)
    )
    batch_ms = [
        float(r["batch_ms"]) for r in successes
        if isinstance(r.get("batch_ms"), (int, float))
    ]
    return {
        "records": len(records), "successful_batches": len(successes),
        "blocks_processed": processed, "collector_blocks_per_second": rate,
        "chain_blocks_per_second": chain_rate,
        "lag_start": lag_start, "lag_end": lag_end,
        "catchup_observed": catchup,
        "retryable_failures": sum(r.get("status") == "RETRYABLE_FAILURE" for r in records),
        "fatal_failures": sum(r.get("status") == "FATAL_FAILURE" for r in records),
        "rpc_retries": sum(int(r.get("rpc_retries") or 0) for r in records),
        "gamma_lookups": sum(int(r.get("gamma_lookup_count") or 0) for r in records),
        "new_trades_reported": sum(int(r.get("trade_count") or 0) for r in successes),
        "mean_success_batch_ms": (sum(batch_ms) / len(batch_ms)) if batch_ms else None,
        "max_success_batch_ms": max(batch_ms) if batch_ms else None,
    }


def pipeline_summary(data, db):
    flow = safe_json(data / "flow_state.json", {})
    manifest = safe_json(data / "diamond_generation.json", {})
    risk = safe_json(data / "risk_assessment.json", {})
    focus = safe_json(data / "focused_market.json", {})
    book = safe_json(data / "book_assessment.json", {})
    paper = safe_json(data / "paper_state.json", {})
    flow = flow if isinstance(flow, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    risk = risk if isinstance(risk, dict) else {}
    generation = manifest.get("generation_id")
    generation_dir = data / "diamond_generations" / str(generation)
    gen_flow = safe_json(generation_dir / "flow_state.json", {})
    analysis = safe_json(generation_dir / "diamond_analysis_v3.json", [])
    gen_flow = gen_flow if isinstance(gen_flow, dict) else {}
    analysis = analysis if isinstance(analysis, list) else []
    results = risk.get("results", [])
    results = results if isinstance(results, list) else []
    e = set(db.get("_evidence", []))
    flow_pairs = {(str(v.get("token_id")), str(v.get("condition_id"))) for v in gen_flow.values() if isinstance(v, dict)}
    analysis_pairs = {(str(v.get("token_id")), str(v.get("condition_id"))) for v in analysis if isinstance(v, dict)}
    risk_pairs = {(str(v.get("token_id")), str(v.get("condition_id"))) for v in results if isinstance(v, dict)}
    observed = e & flow_pairs & analysis_pairs & risk_pairs
    return {
        "flow_market_count": len(flow),
        "diamond_generation_exists": bool(generation),
        "diamond_analysis_count": len(analysis),
        "risk_generation_matches": bool(generation and risk.get("source_generation_id") == generation),
        "risk_decisions": len(results),
        "real_trade_to_risk_binding_observed": bool(observed and generation and risk.get("source_generation_id") == generation),
        "focus_input_status": focus.get("input_status") if isinstance(focus, dict) else None,
        "focus_state": focus.get("state") if isinstance(focus, dict) else None,
        "book_status": book.get("status") if isinstance(book, dict) else None,
        "paper_positions": len(paper.get("positions", [])) if isinstance(paper, dict) and isinstance(paper.get("positions"), list) else None,
        "paper_state_exists": isinstance(paper, dict),
    }


def compact_status(supervisor):
    full = supervisor.snapshot()
    return {
        "at": utc_now(),
        "overall_status": full["overall_status"],
        "services": {
            name: {
                "status": state["status"],
                "restart_count": state["restart_count"],
                "consecutive_failures": state["consecutive_failures"],
                "critical": state["critical"],
                "enabled": state["enabled"],
            } for name, state in full["services"].items()
        },
    }


def worker_process_environment(rpc):
    """Explicit UTF-8 worker logs even on Windows cp1252 installations.

    Preserve valid multilingual market titles; escape unpaired surrogates
    rather than crashing a critical worker while printing diagnostics.
    """
    env = os.environ.copy()
    env["POLYMARKET_RPC_URL"] = rpc
    env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env.pop(name, None)
    return env


def _percentile(values, q):
    """Linear-interpolated percentile of bounded, finite numeric samples."""
    values = sorted(float(v) for v in values if isinstance(v, (int, float))
                    and not isinstance(v, bool) and 0 <= float(v) < float("inf"))
    if not values:
        return None
    pos = (len(values) - 1) * q
    lower = int(pos)
    upper = min(len(values) - 1, lower + 1)
    return round(values[lower] + (values[upper] - values[lower]) * (pos - lower), 3)


def freshness_diagnostics(data, *, now=None):
    """Counts only, no questions, token IDs, hashes or personal information."""
    from collections import Counter
    now = now or datetime.now(timezone.utc)
    summary = {"snapshot_unique_v4_trades": 0, "invalid_block_timestamps": 0,
               "future_block_timestamps": 0, "age_seconds_p50": None,
               "age_seconds_p95": None, "ages_over_120s": 0,
               "snapshot_window_trades": {"1m": 0, "5m": 0, "15m": 0}}
    ages = []
    seen = set()
    snapshot = data / "live_trades.jsonl"
    try:
        with snapshot.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    trade = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(trade, dict) or trade.get("collector_version") != 4:
                    continue
                tx, idx = trade.get("transaction_hash"), trade.get("log_index")
                if not isinstance(tx, str) or len(tx) != 66 or not isinstance(idx, int) or isinstance(idx, bool):
                    continue
                key = (tx.lower(), idx)
                if key in seen:
                    continue
                seen.add(key)
                timestamp = trade.get("block_timestamp")
                if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
                    summary["invalid_block_timestamps"] += 1
                    continue
                try:
                    age = (now - datetime.fromtimestamp(timestamp, timezone.utc)).total_seconds()
                except (OSError, OverflowError, ValueError):
                    summary["invalid_block_timestamps"] += 1
                    continue
                if age < -5:
                    summary["future_block_timestamps"] += 1
                    continue
                ages.append(max(0.0, age))
    except (OSError, UnicodeError):
        summary["snapshot_status"] = "MISSING_OR_UNREADABLE"
        return summary
    summary["snapshot_status"] = "OK"
    summary["snapshot_unique_v4_trades"] = len(ages)
    summary["age_seconds_p50"] = _percentile(ages, .50)
    summary["age_seconds_p95"] = _percentile(ages, .95)
    summary["ages_over_120s"] = sum(age > 120 for age in ages)
    for label, seconds in (("1m", 60), ("5m", 300), ("15m", 900)):
        summary["snapshot_window_trades"][label] = sum(age <= seconds for age in ages)
    flow = safe_json(data / "flow_state.json", {})
    analysis = safe_json(data / "diamond_analysis_v3.json", [])
    risk = safe_json(data / "risk_assessment.json", {})
    flow = flow if isinstance(flow, dict) else {}
    analysis = analysis if isinstance(analysis, list) else []
    rows = risk.get("results", []) if isinstance(risk, dict) else []
    rows = rows if isinstance(rows, list) else []
    summary["flow_markets_with_trades"] = {
        label: sum(isinstance(row, dict) and isinstance(row.get("flow_" + label), dict)
                   and (row["flow_" + label].get("trade_count") or 0) > 0
                   for row in flow.values())
        for label in ("1m", "5m", "15m")
    }
    summary["flow_verified"] = sum(isinstance(row, dict) and row.get("verified") is True
                                   for row in flow.values())
    summary["diamond_classes"] = dict(Counter(str(row.get("classification", "UNKNOWN"))
                                    for row in analysis if isinstance(row, dict)))
    summary["risk_decisions"] = dict(Counter(str(row.get("decision", "UNKNOWN"))
                                     for row in rows if isinstance(row, dict)))
    summary["risk_reason_codes_top15"] = [
        [code, count] for code, count in Counter(str(code)
        for row in rows if isinstance(row, dict)
        for code in (row.get("reason_codes") or []) if isinstance(code, str)
        ).most_common(15)
    ]
    return summary


def timing_diagnostics(records):
    """Existing LIVE-1C timing counters; overlapping scopes are NOT additive."""
    keys = ("batch_ms", "rpc_ms", "get_logs_ms", "timestamp_ms",
            "gamma_lookup_ms", "gamma_refresh_ms", "decode_ms",
            "enrichment_ms", "sqlite_ms", "compatibility_ms", "backoff_ms")
    out = {"note": "Phases may overlap (e.g. RPC inside lookups); do not sum percentages.",
           "successful_batches": sum(r.get("status") == "SUCCESS" for r in records),
           "phases_ms": {}}
    for name in keys:
        samples = [r[name] for r in records if
                   (r.get("status") == "SUCCESS" or name == "backoff_ms")
                   and isinstance(r.get(name), (int, float))
                   and not isinstance(r[name], bool) and 0 <= float(r[name]) < float("inf")]
        out["phases_ms"][name] = {
            "samples": len(samples), "total": round(sum(samples), 3),
            "mean": round(sum(samples) / len(samples), 3) if samples else None,
            "p50": _percentile(samples, .5), "p95": _percentile(samples, .95),
            "max": round(max(samples), 3) if samples else None,
        }
    return out


def controlled_neartip_environment(rpc):
    """Near-tip experiment only: no production env/start-block inheritance."""
    env = worker_process_environment(rpc)
    env.pop("COLLECTOR_START_BLOCK", None)
    env["COLLECTOR_INITIAL_LOOKBACK_BLOCKS"] = "30"
    env["COLLECTOR_CONFIRMATION_BLOCKS"] = "20"
    return env


def run_live(duration, rpc, *, dashboard=False, near_tip=False):
    # A fresh sandbox has no data, so the preparation code cannot archive
    # any pre-existing user files. Never run preparation in a reused sandbox.
    isolation_gate(ROOT)
    run_env = controlled_neartip_environment(rpc) if near_tip else worker_process_environment(rpc)
    # Inherited credentials are not needed by any other worker.
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)

    start_block = rpc_number(rpc, "eth_blockNumber")
    if rpc_number(rpc, "eth_chainId") != 137:
        raise GateError("RPC is not Polygon chain 137; stopped before creating data")

    data = ROOT / "data"
    data.mkdir()
    # Generate only fresh state in the isolated clone.
    subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "prepare_v3_data.py")],
        cwd=ROOT, env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True,
    )
    logs = ROOT / "general1_e2e2_logs"
    logs.mkdir(exist_ok=False)
    specs = []
    for spec in run_machine.build_services():
        enabled = spec.enabled and spec.name not in ("telegram",)
        if spec.name == "dashboard":
            enabled = bool(dashboard)
        specs.append(ServiceSpec(spec.name, spec.cmd, spec.critical, enabled))
    names_by_cmd = {tuple(spec.cmd): spec.name for spec in specs}
    def popen(cmd, cwd):
        name = names_by_cmd[tuple(cmd)]
        # Raw worker logs stay local and should NOT be uploaded.
        with (logs / (name + ".log")).open("ab") as stream:
            return subprocess.Popen(
                cmd, cwd=cwd, env=run_env,
                stdout=stream, stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
    supervisor = RuntimeSupervisor(
        specs, cwd=ROOT, status_file=data / "runtime_status.json",
        popen_factory=popen,
    )
    statuses = []
    start_time = time.monotonic()
    interrupted = False
    fatal = None
    try:
        supervisor.start_all()
        last_sample = -15
        while time.monotonic() - start_time < duration:
            fatal = supervisor.poll_once()
            elapsed = time.monotonic() - start_time
            if elapsed - last_sample >= 15:
                statuses.append(compact_status(supervisor))
                last_sample = elapsed
                if int(elapsed) % 60 < 15:
                    print(f"[LIVE] elapsed={int(elapsed)}s status={statuses[-1]['overall_status']}", flush=True)
            if fatal:
                break
            # Paper requests are disallowed even in this isolated run.
            if list((data / "paper_requests").glob("*.json")) if (data / "paper_requests").exists() else False:
                fatal = "UNEXPECTED_PAPER_REQUEST"
                break
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        statuses.append(compact_status(supervisor))
        supervisor.shutdown()
    seconds = max(0.001, time.monotonic() - start_time)
    try:
        end_block = rpc_number(rpc, "eth_blockNumber")
    except GateError:
        end_block = None
    db = db_summary(data / "collector.sqlite3")
    records = diagnostic_records(logs / "collector.log")
    metrics = checkpoint_metrics(records, start_block, end_block, seconds)
    pipeline = pipeline_summary(data, db)
    freshness = freshness_diagnostics(data)
    performance = timing_diagnostics(records)
    final_status = statuses[-1] if statuses else {}
    worker_states = final_status.get("services", {})
    critical_running = all(worker_states.get(n, {}).get("status") == "RUNNING" for n in CORE)
    no_restarts = all(worker_states.get(n, {}).get("restart_count") == 0 for n in CORE)
    if fatal or not db.get("quick_check_ok", False) or metrics["fatal_failures"]:
        result = "BLOCKED"
    elif (not interrupted and critical_running and no_restarts and metrics["blocks_processed"] > 0
          and metrics["catchup_observed"] is True and pipeline["real_trade_to_risk_binding_observed"]
          and pipeline["paper_positions"] == 0):
        result = "PASS"
    else:
        result = "PARTIAL"
    # No RPC URLs, tokens, full worker stderr or individual wallets in report.
    report = {
        "task": "GENERAL1-FRESHNESS-1" if near_tip else "GENERAL1-E2E-2", "result": result,
        "experiment": ({"mode": "NEAR_TIP_ONLY", "initial_lookback_blocks": 30,
                        "confirmation_blocks": 20, "historical_backfill_complete": False}
                       if near_tip else {"mode": "E2E2_BASELINE"}),
        "generated_at": utc_now(), "duration_seconds": seconds,
        "target_seconds": duration, "interrupted": interrupted,
        "fatal_service": fatal, "chain_id": 137, "chain_head_before": start_block,
        "chain_head_after": end_block,
        "supervisor_status": final_status, "collector": metrics,
        "database": {k: v for k, v in db.items() if not k.startswith("_")},
        "pipeline": pipeline,
        "freshness": freshness,
        "phase_timings": performance,
        "historical_databases_touched": False,
        "raw_worker_logs_private": True,
    }
    (ROOT / REPORT_NAME).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (ROOT / "general1_e2e2_status_samples.jsonl").open("w", encoding="utf-8") as stream:
        for status in statuses:
            stream.write(json.dumps(status) + "\n")
    print(f"[RESULT] {result}: report={ROOT / REPORT_NAME}", flush=True)
    return 0 if result == "PASS" else 2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", default=None, help="External local .env; only RPC key is read")
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--near-tip", action="store_true", help="Sandbox-only 30-block initial lookback; no backfill claim")
    args = parser.parse_args(argv)
    try:
        isolation_gate(ROOT)
        if not 60 <= args.duration <= 1800:
            raise GateError("Duration must be between 60 and 1800 seconds")
        rpc = get_rpc(args.source_env)
        print("[PREFLIGHT] isolated sandbox, RPC configured, no credential output", flush=True)
        return run_live(args.duration, rpc, dashboard=args.dashboard, near_tip=args.near_tip)
    except GateError as exc:
        print(f"[BLOCKED] {exc}", flush=True)
        return 3
    except Exception as exc:
        # Never include exception messages: a provider library may echo URLs.
        print(f"[BLOCKED] local runtime failure ({type(exc).__name__}); inspect local logs", flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
