"""GENERAL1 LIVE EVIDENCE GATE 1 — isolated, non-trading data observation.

Default: read-only anonymized audit. --run-seconds starts collector, flow,
diamond, risk, focus, book and learning only in a fresh separate worktree.
Never starts Paper, Telegram, dashboard or wallet code. No strategy changes.
Requires an explicitly named fresh directory containing 'livegate'.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib import request, parse

BASE = Path(__file__).resolve().parents[1]
SERVICES = {
    "collector": ["scripts/live_active_trades.py"],
    "flow": ["scripts/flow_tracker.py"],
    "diamond": ["scripts/diamond_filter_v3.py", "--watch", "--interval", "10"],
    "risk": ["scripts/risk_worker.py", "--interval", "5"],
    "focus": ["scripts/focus_runner.py", "--interval", "5"],
    "book": ["scripts/book_worker.py", "--interval", "5"],
    "learning": ["scripts/learning_worker.py", "--interval", "5"],
}
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")


def _db(path):
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn, sql):
    return list(conn.execute(sql))


def _json(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _codes(values):
    return [str(x) if SAFE_CODE.fullmatch(str(x)) else "OTHER_REDACTED"
            for x in (values if isinstance(values, list) else [])]


def _event_counts(path):
    result = Counter()
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                event = str(row.get("type") or "").upper()
                if event in {"LOCKED", "WAIT", "READY", "INVALIDATED", "RELEASED"}:
                    result[event] += 1
                else:
                    result["OTHER"] += 1
            except (ValueError, AttributeError):
                result["MALFORMED"] += 1
    return dict(sorted(result.items()))


def snapshot(data_dir):
    """Only counts, statuses and reason-code histograms; no user IDs or URLs."""
    data_dir = Path(data_dir)
    out = {"collector": {"status": "MISSING"}, "learning": {"status": "MISSING"}}
    db = _db(data_dir / "collector.sqlite3")
    if db is not None:
        try:
            cp = _rows(db, "SELECT block_number FROM checkpoint WHERE chain_id=137 LIMIT 1")
            count = _rows(db, "SELECT COUNT(*), MIN(block_number), MAX(block_number) FROM blocks")[0]
            ntrade = _rows(db, "SELECT COUNT(*) FROM trades")[0][0]
            out["collector"] = {
                "status": "READ_ONLY_COUNTS", "checkpoint": cp[0][0] if cp else None,
                "blocks": count[0], "first_block": count[1], "last_block": count[2],
                "trades": ntrade, "identity_violations": "NOT_PROVEN_BY_COUNTS",
            }
            try:
                out["collector"]["distinct_condition_ids"] = _rows(
                    db, "SELECT COUNT(DISTINCT json_extract(payload_json,'$.condition_id')) FROM trades"
                )[0][0]
                raw = _rows(db, "SELECT json_extract(payload_json,'$.asset_type'), COUNT(*) FROM trades GROUP BY 1")
                out["collector"]["asset_types"] = {
                    k if k in ("CTF_TOKEN", "POSITION") else "UNKNOWN": v
                    for k, v in raw
                }
            except sqlite3.Error:
                out["collector"]["payload_identity_analysis"] = "UNAVAILABLE"
        except sqlite3.Error:
            out["collector"] = {"status": "SCHEMA_OR_DB_ERROR"}
        finally:
            db.close()

    db = _db(data_dir / "learning.sqlite3")
    if db is not None:
        try:
            names = {"signal_observations": "classification",
                     "risk_decisions": "decision", "focus_events": "event_type",
                     "ready_opportunities": "selection_status",
                     "book_decisions": "status", "paper_trades": "status"}
            hist = {}
            counts = {}
            for table, field in names.items():
                rows = _rows(db, "SELECT " + field + ", COUNT(*) FROM " + table + " GROUP BY " + field)
                hist[table] = {str(k): v for k, v in rows}
                counts[table] = sum(v for _, v in rows)
            out["learning"] = {"status": "READ_ONLY_COUNTS", "counts": counts, "stage_histograms": hist}
        except sqlite3.Error:
            out["learning"] = {"status": "SCHEMA_OR_DB_ERROR"}
        finally:
            db.close()

    flow = _json(data_dir / "flow_state.json")
    if isinstance(flow, dict):
        out["flow_latest"] = {
            "tokens": len(flow),
            "verified": sum(x.get("verified") is True for x in flow.values()
                            if isinstance(x, dict)),
        }
    paper = _json(data_dir / "paper_state.json")
    if isinstance(paper, dict):
        positions = paper.get("positions")
        if isinstance(positions, list):
            out["paper_state_existing"] = {
                "position_count": len(positions),
                "statuses": dict(Counter(str(x.get("status") or "UNKNOWN")[:30]
                    for x in positions if isinstance(x, dict))),
                "processed_request_id_count": len(paper.get("processed_request_ids") or []),
                "journal_reconciled": "NOT_CHECKED",
            }
    event_file = data_dir / "paper_events.jsonl"
    if event_file.is_file():
        with event_file.open(encoding="utf-8") as stream:
            out["paper_existing_event_line_count"] = sum(1 for _ in stream)
    analyzed = _json(data_dir / "diamond_analysis_v3.json")
    if isinstance(analyzed, list):
        out["diamond_latest"] = {
            "analyzed": len(analyzed),
            "classifications": dict(Counter(
                str(x.get("classification") or "UNKNOWN") for x in analyzed
                if isinstance(x, dict))),
        }
    risk = _json(data_dir / "risk_assessment.json")
    if isinstance(risk, dict):
        rr = risk.get("results")
        if isinstance(rr, list):
            out["risk_latest"] = {
                "checked": len(rr),
                "pass": sum(x.get("risk_ok") is True for x in rr if isinstance(x, dict)),
                "block_reasons": dict(Counter(code for x in rr if isinstance(x, dict)
                    and x.get("risk_ok") is not True for code in _codes(x.get("reason_codes")))),
            }
    focus = _json(data_dir / "focused_market.json")
    if isinstance(focus, dict):
        out["focus_latest"] = {"state": str(focus.get("state") or "UNKNOWN")[:30],
                               "input_status": str(focus.get("input_status") or "UNKNOWN")[:30]}
    book = _json(data_dir / "book_assessment.json")
    if isinstance(book, dict):
        out["book_latest"] = {
            "status": str(book.get("status") or "UNKNOWN")[:30],
            "book_ok": book.get("book_ok") is True,
            "reason_codes": _codes(book.get("reason_codes")),
        }
    out["focus_events"] = _event_counts(data_dir / "focus_events.jsonl")
    out["paper_worker_started_by_gate"] = False
    out["market_profitability"] = "NOT_YET_MEASURABLE"
    return out


def _collector_diagnostics(path):
    """Aggregate canonical LIVE-1C batch records; never return raw log content."""
    counts = Counter()
    totals = Counter()
    stop_reasons = Counter()
    gamma_categories = Counter()
    if not path.is_file():
        return {"status": "NO_LOG"}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "[COLLECTOR] RETRYABLE_BATCH" in line:
                counts["RETRYABLE_LOG_LINE"] += 1
            if "[COLLECTOR] Storage/chain history conflict:" in line:
                reason = line.split("Storage/chain history conflict:", 1)[-1]
                if "Gamma" in reason:
                    stop_reasons["GAMMA_IDENTITY_OR_METADATA_CONFLICT"] += 1
                elif "V3 Data API" in reason or "V3 position" in reason:
                    stop_reasons["V3_IDENTITY_OR_METADATA_CONFLICT"] += 1
                elif "Block range" in reason or "Checkpoint" in reason:
                    stop_reasons["CHAIN_CHECKPOINT_OR_REORG_CONFLICT"] += 1
                elif "RPC" in reason or "Log does not" in reason:
                    stop_reasons["RPC_LOG_INTEGRITY_CONFLICT"] += 1
                else:
                    stop_reasons["OTHER_STORAGE_CONFLICT"] += 1
            if "[COLLECTOR] Fatal configuration:" in line:
                stop_reasons["FATAL_CONFIGURATION"] += 1
            if "[COLLECTOR] Stopped safely (" in line:
                stop_reasons["UNHANDLED_EXCEPTION_CLASS"] += 1
            if "[LIVE-1C] " not in line:
                continue
            try:
                row = json.loads(line.split("[LIVE-1C] ", 1)[1])
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status"))
                counts[status if status in (
                    "SUCCESS", "RETRYABLE_FAILURE", "FATAL_FAILURE",
                    "PARTIAL", "DEGRADED") else "OTHER_BATCH"] += 1
                for key in ("blocks", "trade_count", "rpc_calls", "rpc_retries",
                            "gamma_lookup_count", "gamma_lookup_ms",
                            "gamma_v3_lookup_count", "gamma_v3_lookup_ms",
                            "v3_unsupported_events", "raw_log_count"):
                    value = row.get(key)
                    if isinstance(value, (float, int)) and not isinstance(value, bool):
                        totals[key] += value
                misses = row.get("gamma_misses") or []
                totals["gamma_miss_count"] += len(misses)
                for miss in misses:
                    if not isinstance(miss, dict):
                        continue
                    success = miss.get("lookup_succeeded")
                    kind = miss.get("market_classification")
                    if success is False:
                        gamma_categories["LOOKUP_FAILED"] += 1
                    elif success is True and kind in ("ACTIVE_OPEN", "CLOSED_INACTIVE_HISTORICAL"):
                        gamma_categories[kind] += 1
                    else:
                        gamma_categories["UNCLASSIFIED"] += 1
                if isinstance(row.get("outer_retries"), int):
                    totals["max_outer_retry_streak"] = max(
                        totals["max_outer_retry_streak"], row["outer_retries"])
            except (ValueError, TypeError, IndexError):
                counts["MALFORMED_DIAGNOSTIC"] += 1
    return {"status": "PARSED", "batch_statuses": dict(sorted(counts.items())),
            "total_metrics": dict(sorted(totals.items())),
            "fatal_reason_classes": dict(sorted(stop_reasons.items())),
            "gamma_lookup_categories": dict(sorted(gamma_categories.items()))}


def _env_rpc(env_file):
    rpc = os.environ.get("POLYMARKET_RPC_URL", "").strip()
    if not rpc and env_file is not None:
        with Path(env_file).open(encoding="utf-8") as stream:
            for line in stream:
                key, delim, value = line.strip().partition("=")
                if delim and key.strip() == "POLYMARKET_RPC_URL":
                    rpc = value.strip().strip("\"'")
    parts = parse.urlsplit(rpc)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("RPC_MISSING_OR_INVALID_HTTPS_URL")
    return rpc


def _rpc_call(rpc_url, method):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
    req = request.Request(rpc_url, body, {"Content-Type": "application/json",
                                          "User-Agent": "PolymarketResearch-ReadOnly-Evidence/1.0"})
    with request.urlopen(req, timeout=10) as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
        raise ValueError("RPC_INVALID_RESPONSE")
    return int(payload["result"], 16)


def _write_report(path, report):
    path.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _build_worker_env(rpc):
    """Whitelist observational controls; never propagate Telegram or wallet secrets."""
    safe_keys = ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE",
                 "SSL_CERT_DIR", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR")
    child = {key: os.environ[key] for key in safe_keys if key in os.environ}
    child["POLYMARKET_RPC_URL"] = rpc
    child["PYTHONUNBUFFERED"] = "1"
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    settings = {}
    for key, default, ceiling in (
        ("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", 500, 1000),
        ("COLLECTOR_CONFIRMATION_BLOCKS", 20, 100),
        ("COLLECTOR_GAMMA_BATCH_LOOKUP", 0, 1),
    ):
        raw = os.environ.get(key, str(default))
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError("INVALID_COLLECTOR_OBSERVER_CONFIG")
        value = int(raw)
        minimum = 0 if key == "COLLECTOR_GAMMA_BATCH_LOOKUP" else 1
        if value < minimum or value > ceiling:
            raise ValueError("INVALID_COLLECTOR_OBSERVER_CONFIG")
        child[key] = str(value)
        settings[key] = value
    return child, settings


def live_gate(seconds, env_file):
    """Only call in a fresh dedicated worktree; never touch an existing data root."""
    if "livegate" not in BASE.name.lower():
        raise ValueError("FRESH_LIVEGATE_WORKTREE_NAME_REQUIRED")
    data = BASE / "data"
    if data.exists() and any(data.iterdir()):
        raise ValueError("DATA_ROOT_NOT_EMPTY_REFUSING_TO_RUN")
    if seconds < 60 or seconds > 600:
        raise ValueError("OBSERVATION_DURATION_OUT_OF_RANGE")
    rpc = _env_rpc(env_file)
    if _rpc_call(rpc, "eth_chainId") != 137:
        raise ValueError("RPC_NOT_POLYGON_137")
    starting_head = _rpc_call(rpc, "eth_blockNumber")
    data.mkdir(exist_ok=True)
    logs = data / "livegate_logs"
    logs.mkdir(exist_ok=False)
    procs = []
    handles = []
    observations = []
    unexpected_exits = []
    started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    # Pass only validated public-read configuration, never tokens or private keys.
    env, collector_settings = _build_worker_env(rpc)
    try:
        for name, args in SERVICES.items():
            handle = (logs / (name + ".log")).open("w", encoding="utf-8")
            handles.append(handle)
            proc = subprocess.Popen(
                [sys.executable, "-u", *args], cwd=BASE, env=env,
                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
            procs.append((name, proc))
        while time.monotonic() - started_monotonic < seconds:
            try:
                head = _rpc_call(rpc, "eth_blockNumber")
                counts = snapshot(data)["collector"]
                cp = counts.get("checkpoint")
                observations.append({"elapsed_s": round(time.monotonic() - started_monotonic, 1),
                                     "head": head, "checkpoint": cp,
                                     "lag_blocks": head - cp if cp is not None else None,
                                     "trades": counts.get("trades"),
                                     "blocks": counts.get("blocks")})
            except (OSError, ValueError, sqlite3.Error):
                observations.append({"elapsed_s": round(time.monotonic() - started_monotonic, 1),
                                     "sample_error": "RPC_OR_DB_UNAVAILABLE"})
            unexpected_exits = [name for name, proc in procs if proc.poll() is not None]
            if unexpected_exits:
                break
            time.sleep(min(15, max(0.1, seconds - (time.monotonic() - started_monotonic))))
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)
        for handle in handles:
            handle.close()
    ended = datetime.now(timezone.utc)
    data_snapshot = snapshot(data)
    collector_diagnostics = _collector_diagnostics(logs / "collector.log")
    first = next((x for x in observations if x.get("lag_blocks") is not None), None)
    last = next((x for x in reversed(observations) if x.get("lag_blocks") is not None), None)
    real_seconds = (ended - started).total_seconds()
    delta_blocks = ((last["checkpoint"] - first["checkpoint"])
                    if first and last else None)
    delta_head = (last["head"] - first["head"]) if first and last else None
    report = {
        "gate": "GENERAL1-LIVE-EVIDENCE-GATE-1",
        "mode": "ISOLATED_PUBLIC_READS_NO_PAPER",
        "started_utc": started.isoformat(), "ended_utc": ended.isoformat(),
        "elapsed_s": round(real_seconds, 2),
        "requested_s": seconds,
        "start_chain_head": starting_head,
        "collector_runtime_settings": collector_settings,
        "service_names": list(SERVICES),
        "unexpected_exits": unexpected_exits,
        "collector_blocks_per_second": (
            round(delta_blocks / (last["elapsed_s"] - first["elapsed_s"]), 4)
            if first and last and last["elapsed_s"] > first["elapsed_s"] else None),
        "chain_blocks_per_second": (
            round(delta_head / (last["elapsed_s"] - first["elapsed_s"]), 4)
            if first and last and last["elapsed_s"] > first["elapsed_s"] else None),
        "lag_first": first["lag_blocks"] if first else None,
        "lag_last": last["lag_blocks"] if last else None,
        "lag_delta": last["lag_blocks"] - first["lag_blocks"] if first and last else None,
        "samples": observations, "data": data_snapshot,
        "collector_diagnostics": collector_diagnostics,
        "collector_live": "INCONCLUSIVE",
        "natural_ready": ("OBSERVED" if data_snapshot.get("focus_events", {}).get("READY", 0)
                          else "NOT_OBSERVED_IN_WINDOW"),
        "strategy_edge": "INCONCLUSIVE",
    }
    _write_report(data / "livegate_sanitized_report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE / "data")
    parser.add_argument("--run-seconds", type=int)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.run_seconds is None:
            data = snapshot(args.data_dir)
            result = {"mode": "READ_ONLY", "data": data, "strategy_edge": "INCONCLUSIVE"}
        else:
            result = live_gate(args.run_seconds, args.env_file)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Never print exception details; RPC endpoint may be included in network errors.
        print(json.dumps({"status": "BLOCKED", "reason": (
            str(exc) if isinstance(exc, ValueError) and
            str(exc) in {"FRESH_LIVEGATE_WORKTREE_NAME_REQUIRED",
                         "DATA_ROOT_NOT_EMPTY_REFUSING_TO_RUN",
                         "OBSERVATION_DURATION_OUT_OF_RANGE",
                         "RPC_MISSING_OR_INVALID_HTTPS_URL",
                         "RPC_NOT_POLYGON_137",
                         "RPC_INVALID_RESPONSE"} else type(exc).__name__)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
