"""Read-only Dashboard projection of the authoritative paper_state.json.

Never imports the Paper worker, changes Paper state, or creates Paper requests.
Unknown or stale mark-to-market values are displayed as unknown, not $0.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from machine_common import finite_number, fresh


def _empty(status):
    return {
        "status": status,
        "open_count": None,
        "realized_count": None,
        "open_pnl_usd": None,
        "realized_pnl_usd": None,
        "rows": [],
    }


def _number(value):
    # A missing/invalid number is unknown, never a synthetic zero.
    if value is None or isinstance(value, bool):
        return None
    return finite_number(value, None)


def read_paper_summary(data_dir, now=None):
    """Return one faithful, non-authoritative snapshot for Streamlit."""
    now = now or datetime.now(timezone.utc)
    path = Path(data_dir) / "paper_state.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return _empty("MISSING")
    except (OSError, UnicodeError, ValueError):
        return _empty("CORRUPT")

    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or not isinstance(state.get("positions"), list)
        or not isinstance(state.get("processed_request_ids"), list)
    ):
        return _empty("CORRUPT")

    positions = state["positions"]
    if any(
        not isinstance(position, dict)
        or position.get("status") not in {"OPEN", "CLOSED", "SETTLED"}
        or not isinstance(position.get("paper_id"), str)
        or not position["paper_id"].strip()
        for position in positions
    ):
        return _empty("CORRUPT")
    identifiers = [position["paper_id"] for position in positions]
    if len(identifiers) != len(set(identifiers)):
        return _empty("CORRUPT")

    status = "OK" if fresh(state.get("updated_at"), now=now) else "STALE"
    open_rows = []
    realized_rows = []
    rows = []

    for position in positions:
        kind = position["status"]
        entry = position.get("entry") if isinstance(position.get("entry"), dict) else {}
        current_value = None
        pnl = None
        return_pct = None
        mark_status = position.get("mark_status") or "UNKNOWN"

        if kind == "OPEN":
            mark = position.get("mark") if isinstance(position.get("mark"), dict) else {}
            if (
                status == "OK"
                and mark_status == "OK"
                and fresh(mark.get("generated_at"), now=now)
            ):
                current_value = _number(mark.get("current_value_usd"))
                pnl = _number(mark.get("pnl_usd"))
                return_pct = _number(mark.get("return_pct"))
            open_rows.append(pnl)
            if status != "OK":
                mark_status = "STALE_SNAPSHOT"
            elif pnl is None:
                mark_status = (
                    "MARK_UNKNOWN" if mark_status == "OK" else mark_status
                )
        elif kind == "CLOSED":
            exit_data = position.get("exit") if isinstance(position.get("exit"), dict) else {}
            current_value = _number(exit_data.get("net_proceeds_usd"))
            pnl = _number(position.get("realized_pnl_usd"))
            return_pct = _number(position.get("realized_return_pct"))
            mark_status = "CLOSED"
            realized_rows.append(pnl)
        else:
            current_value = _number(position.get("settlement_value_usd"))
            pnl = _number(position.get("realized_pnl_usd"))
            return_pct = _number(position.get("realized_return_pct"))
            mark_status = "SETTLED"
            realized_rows.append(pnl)

        rows.append({
            "Paper ID": position["paper_id"],
            "Market": position.get("question"),
            "Status": kind,
            "Outcome": position.get("outcome"),
            "Investment": _number(position.get("investment_usd")),
            "Entry": _number(entry.get("effective_entry_price")),
            "Current/Exit": current_value,
            "P/L": pnl,
            "Return %": return_pct,
            "Mark status": mark_status,
        })

    open_total = (
        round(sum(open_rows), 2)
        if status == "OK" and all(x is not None for x in open_rows)
        else None
    )
    realized_total = (
        round(sum(realized_rows), 2)
        if all(x is not None for x in realized_rows)
        else None
    )
    return {
        "status": status,
        "open_count": len(open_rows),
        "realized_count": len(realized_rows),
        "open_pnl_usd": open_total,
        "realized_pnl_usd": realized_total,
        "rows": list(reversed(rows)),
    }
