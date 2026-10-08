import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from machine_common import fresh, finite_number, save_json_atomic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

ANALYSIS = DATA / "diamond_analysis_v3.json"
DIAMONDS = DATA / "diamonds.json"
DIAMOND_MANIFEST = DATA / "diamond_generation.json"
DIAMOND_GENERATIONS = DATA / "diamond_generations"
FOCUS = DATA / "focused_market.json"
BOOK = DATA / "book_assessment.json"
PAPER_REQUEST_DIR = DATA / "paper_requests"
PAPER_STATE = DATA / "paper_state.json"
STATE = DATA / "telegram_state.json"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ENV_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WATCH_INTERVAL = int(os.getenv("TELEGRAM_WATCH_INTERVAL", "5"))


def load(path, default):
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except Exception:
        return default


def save(path, data):
    save_json_atomic(path, data)


def num(value, default=0):
    return finite_number(value, default)


def authorized(update):
    return bool(
        ENV_CHAT
        and update.effective_chat
        and str(update.effective_chat.id) == ENV_CHAT
    )


def sid(row):
    return hashlib.sha1(
        str(row.get("market_key", "")).encode()
    ).hexdigest()[:12]


def _diamond_generation_snapshot(filename):
    if not DIAMOND_MANIFEST.exists():
        return "MISSING", None

    try:
        with DIAMOND_MANIFEST.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except Exception:
        return "CORRUPT", None

    if not isinstance(manifest, dict):
        return "CORRUPT", None

    generation_id = str(manifest.get("generation_id") or "").strip()
    published_at = manifest.get("published_at")
    if not generation_id or not published_at:
        return "CORRUPT", manifest

    if not fresh(published_at):
        return "STALE", manifest

    path = DIAMOND_GENERATIONS / generation_id / filename
    if not path.exists():
        return "MISSING", manifest

    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except Exception:
        return "CORRUPT", manifest

    if not isinstance(payload, list):
        return "CORRUPT", manifest

    return "OK", payload


def analyses_snapshot():
    return _diamond_generation_snapshot("diamond_analysis_v3.json")


def diamonds_snapshot():
    return _diamond_generation_snapshot("diamonds.json")


def analyses():
    status, value = analyses_snapshot()
    return value if status == "OK" else []


def diamonds():
    status, value = diamonds_snapshot()
    if status != "OK":
        return []

    return [
        row
        for row in value
        if isinstance(row, dict)
        and fresh(row.get("source_updated_at"))
        and fresh(row.get("last_trade_at"))
    ]


def _default_state():
    return {
        "chat_ids": [],
        "active_diamonds": [],
        "cashflow_levels": {},
        "focus_alert_key": None,
        "focus_invalidated_key": None,
        "execution_ready_key": None,
        "paper_open_alerts": [],
        "paper_closed_alerts": [],
        "paper_settled_alerts": [],
    }


def state():
    value = load(STATE, _default_state())
    if not isinstance(value, dict):
        return _default_state()

    result = _default_state()
    result.update(value)
    return result


def find_row(short):
    for row in analyses():
        if sid(row) == short:
            return row
    return None


def money(value):
    return f"${num(value):,.2f}"


def chat_ids():
    return {ENV_CHAT} if ENV_CHAT else set()


def register_chat(chat_id):
    if str(chat_id) != ENV_CHAT:
        return
    current = state()
    current["chat_ids"] = [ENV_CHAT]
    save(STATE, current)


def scoreline(row):
    scores = row.get("scores", {})
    return (
        f"Signal {num(scores.get('signal_quality')):.0f} | "
        f"Verify {num(scores.get('verification')):.0f} | "
        f"Entry {num(scores.get('entry_quality')):.0f} | "
        f"Resolution {num(scores.get('resolution_reliability')):.0f}"
    )


def short_message(row):
    cash = row.get("cashflow_alert", {})
    lines = [
        f"{row.get('classification', '?')} | {row.get('question', 'Unknown')}",
        f"Outcome: {row.get('outcome', '?')} @ {num(row.get('price')):.4f}",
        f"Flow direction: {row.get('direction', 'UNKNOWN')}",
        scoreline(row),
    ]
    if cash.get("active"):
        lines.append(
            f"💰 {cash.get('tier')} {cash.get('quality')} | "
            f"{money(cash.get('volume_5m'))}/5m"
        )
    return "\n".join(lines)


def analysis_message(row):
    parts = [
        "🔎 MARKET ANALYSIS",
        "",
        row.get("question", "Unknown"),
        f"Outcome: {row.get('outcome', '?')} @ {num(row.get('price')):.4f}",
        f"Status: {row.get('classification', '?')}",
        scoreline(row),
    ]
    entry = row.get("entry", {})
    resolution = row.get("resolution", {})
    metrics = row.get("metrics", {})
    parts += [
        "",
        (
            f"5m: {metrics.get('trades_5m', 0)} trades | "
            f"{money(metrics.get('volume_5m'))} | "
            f"net {money(metrics.get('net_5m'))} | "
            f"strength {num(metrics.get('strength_5m')):.2f}"
        ),
        (
            f"15m: net {money(metrics.get('net_15m'))} | "
            f"strength {num(metrics.get('strength_15m')):.2f}"
        ),
        f"Confirmations: {metrics.get('confirmations', 0)}/3",
        f"Max gross room to $1: {entry.get('max_resolution_upside_pct', '?')}%",
        (
            f"Timing: {resolution.get('derived_timing', 'UNKNOWN')} | "
            f"end: {resolution.get('end_date') or 'unknown'}"
        ),
    ]

    good = (
        row.get("signal", {}).get("reasons", [])
        + row.get("verification", {}).get("reasons", [])
        + row.get("entry", {}).get("reasons", [])
    )[:6]
    if good:
        parts += ["", "WHY IT IS INTERESTING"] + [f"✅ {item}" for item in good]

    blocks = row.get("why_not_diamond", [])[:6]
    if blocks:
        parts += ["", "WHY NOT DIAMOND"] + [f"⚠️ {item}" for item in blocks]

    parts += [
        "",
        "Focus and Book are authoritative for execution readiness.",
        "Entry-quality score is not win probability.",
    ]
    return "\n".join(parts)


def keyboard(row):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔎 Analyze", callback_data=f"a:{sid(row)}")]]
    )


def _snapshot(path):
    if not path.exists():
        return "MISSING", None

    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except Exception:
        return "CORRUPT", None

    if not isinstance(payload, dict):
        return "CORRUPT", None
    if payload.get("schema_version") != 1:
        return "CORRUPT", payload
    if not payload.get("generated_at"):
        return "CORRUPT", payload
    if not fresh(payload.get("generated_at")):
        return "STALE", payload

    return "OK", payload


def focus_snapshot():
    return _snapshot(FOCUS)


def book_snapshot():
    return _snapshot(BOOK)


def paper_state_snapshot():
    if not PAPER_STATE.exists():
        return "MISSING", None

    try:
        with PAPER_STATE.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except Exception:
        return "CORRUPT", None

    if not isinstance(payload, dict):
        return "CORRUPT", None
    if payload.get("schema_version") != 1:
        return "CORRUPT", payload
    if not payload.get("updated_at"):
        return "CORRUPT", payload
    if not fresh(payload.get("updated_at")):
        return "STALE", payload
    if not isinstance(payload.get("positions"), list):
        return "CORRUPT", payload

    return "OK", payload


def execution_binding(focus_status, focus_payload, book_status, book_payload):
    if focus_status != "OK" or book_status != "OK":
        return False, "INPUT_NOT_OK"

    if focus_payload.get("input_status") != "OK":
        return False, "FOCUS_UPSTREAM_NOT_OK"
    if focus_payload.get("state") != "READY":
        return False, "FOCUS_NOT_READY"

    focus = focus_payload.get("focus")
    if not isinstance(focus, dict) or focus.get("status") != "READY":
        return False, "FOCUS_NOT_READY"

    if book_payload.get("status") != "OK":
        return False, "BOOK_STATUS_NOT_OK"
    if book_payload.get("book_ok") is not True:
        return False, "BOOK_BLOCK"

    token_id = str(focus.get("token_id") or "").strip()
    condition_id = str(focus.get("condition_id") or "").strip()
    evidence_id = str(focus.get("last_evidence_id") or "").strip()
    generation_id = str(focus_payload.get("source_generation_id") or "").strip()

    if not token_id or not condition_id or not evidence_id or not generation_id:
        return False, "FOCUS_BINDING_INCOMPLETE"

    if str(book_payload.get("token_id") or "").strip() != token_id:
        return False, "TOKEN_ID_MISMATCH"
    if str(book_payload.get("condition_id") or "").strip() != condition_id:
        return False, "CONDITION_ID_MISMATCH"
    if str(book_payload.get("source_evidence_id") or "").strip() != evidence_id:
        return False, "EVIDENCE_ID_MISMATCH"
    if str(book_payload.get("source_generation_id") or "").strip() != generation_id:
        return False, "GENERATION_ID_MISMATCH"

    return True, "READY_BOOK_PASS"


def focus_message(status, payload):
    if status != "OK":
        return f"🎯 Focus input: {status}"

    upstream = payload.get("input_status")
    if upstream != "OK":
        return f"🎯 Focus input: {upstream or 'UNKNOWN'}"

    state_label = payload.get("state") or "IDLE"
    focus = payload.get("focus")
    if not isinstance(focus, dict):
        return f"🎯 Focus: {state_label}"

    lines = [
        f"🎯 Focus: {state_label}",
        focus.get("question") or "Unknown market",
        (
            f"{focus.get('outcome') or '?'} | "
            f"price {num(focus.get('price')):.4f}"
        ),
    ]
    if "progress" in focus:
        lines.append(f"Evidence progress: {focus.get('progress')}/3")

    return "\n".join(lines)


def _quote_line(quote):
    amount = num(quote.get("notional_usd"))
    if quote.get("complete") is not True:
        return f"${amount:.0f}: insufficient depth"

    return (
        f"${amount:.0f}: VWAP {num(quote.get('vwap')):.4f} | "
        f"{num(quote.get('outcome_tokens')):.2f} tokens | "
        f"slippage {num(quote.get('slippage_bps')):.1f} bps"
    )


def book_message(status, payload):
    if status != "OK":
        return f"📖 Book input: {status}"

    book_status = payload.get("status") or "UNKNOWN"
    if book_status != "OK":
        reasons = ", ".join(payload.get("reason_codes") or []) or "-"
        return f"📖 Book: {book_status}\nReasons: {reasons}"

    if payload.get("book_ok") is not True:
        reasons = ", ".join(payload.get("reason_codes") or []) or "-"
        return f"📖 Book: BLOCK\nReasons: {reasons}"

    lines = [
        "📖 Book: PASS",
        (
            f"Bid {num(payload.get('best_bid')):.4f} | "
            f"Ask {num(payload.get('best_ask')):.4f} | "
            f"Spread {num(payload.get('spread_bps_mid')):.1f} bps"
        ),
    ]
    for quote in payload.get("quotes") or []:
        if isinstance(quote, dict):
            lines.append(_quote_line(quote))

    return "\n".join(lines)


def execution_message(focus_payload, book_payload):
    focus = focus_payload["focus"]
    lines = [
        "🟢 READY + BOOK PASS",
        "",
        focus.get("question") or "Unknown market",
        f"Outcome: {focus.get('outcome') or '?'}",
        (
            f"Best bid {num(book_payload.get('best_bid')):.4f} | "
            f"best ask {num(book_payload.get('best_ask')):.4f}"
        ),
        f"Spread: {num(book_payload.get('spread_bps_mid')):.1f} bps",
        "",
        "Execution checks:",
    ]

    for quote in book_payload.get("quotes") or []:
        if isinstance(quote, dict):
            lines.append(_quote_line(quote))

    lines += [
        "",
        "Choose a virtual amount below. Telegram only submits the request; "
        "the Paper Worker revalidates READY + Book and executes the simulation.",
    ]
    return "\n".join(lines)


def paper_open_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("$25 PAPER", callback_data="po:25"),
            InlineKeyboardButton("$50 PAPER", callback_data="po:50"),
            InlineKeyboardButton("$100 PAPER", callback_data="po:100"),
        ]]
    )


def paper_close_keyboard(paper_id):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Close PAPER", callback_data=f"pc:{paper_id}")]]
    )


def _write_paper_request(payload):
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("request id missing")
    PAPER_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    save_json_atomic(PAPER_REQUEST_DIR / f"{request_id}.json", payload)


def submit_open_request(amount, chat_id):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None, "Invalid paper amount"

    if amount not in (25.0, 50.0, 100.0):
        return None, "Invalid paper amount"

    focus_status, focus_payload = focus_snapshot()
    book_status, book_payload = book_snapshot()
    ready, reason = execution_binding(
        focus_status,
        focus_payload,
        book_status,
        book_payload,
    )
    if not ready:
        return None, f"Execution not ready: {reason}"

    focus = focus_payload["focus"]
    request_id = "REQ-" + uuid.uuid4().hex[:16]
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "action": "OPEN",
        "amount_usd": amount,
        "chat_id": str(chat_id),
        "token_id": focus.get("token_id"),
        "condition_id": focus.get("condition_id"),
        "source_generation_id": focus_payload.get("source_generation_id"),
        "source_evidence_id": focus.get("last_evidence_id"),
        "focus_locked_at": focus.get("locked_at"),
    }
    _write_paper_request(payload)
    return request_id, None


def submit_close_request(paper_id, chat_id):
    paper_id = str(paper_id or "").strip()
    if not paper_id:
        return None, "Paper position missing"

    status, payload = paper_state_snapshot()
    if status != "OK":
        return None, f"Paper input: {status}"

    position = next(
        (
            row
            for row in payload.get("positions", [])
            if isinstance(row, dict)
            and row.get("paper_id") == paper_id
            and row.get("status") == "OPEN"
        ),
        None,
    )
    if position is None:
        return None, "Open paper position not found"

    request_id = "REQ-" + uuid.uuid4().hex[:16]
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "action": "CLOSE",
        "paper_id": paper_id,
        "chat_id": str(chat_id),
    }
    _write_paper_request(request)
    return request_id, None


def paper_position_message(position):
    mark = position.get("mark") if isinstance(position.get("mark"), dict) else {}
    lines = [
        f"🧪 PAPER {position.get('status', '?')}",
        position.get("question") or "Unknown market",
        f"Outcome: {position.get('outcome') or '?'}",
        f"Investment: {money(position.get('investment_usd'))}",
        (
            f"Entry VWAP {num(position.get('entry', {}).get('vwap')):.4f} | "
            f"effective {num(position.get('entry', {}).get('effective_entry_price')):.4f}"
        ),
        f"Tokens: {num(position.get('tokens')):.4f}",
        f"Mark status: {position.get('mark_status') or 'UNKNOWN'}",
    ]

    if mark and position.get("status") != "SETTLED":
        lines += [
            (
                f"Exit-now VWAP {num(mark.get('exit_vwap')):.4f} | "
                f"effective {num(mark.get('effective_exit_price')):.4f}"
            ),
            (
                f"P/L {money(mark.get('pnl_usd'))} "
                f"({num(mark.get('return_pct')):.2f}%)"
            ),
        ]

    if position.get("status") == "SETTLED":
        settlement = (
            position.get("settlement")
            if isinstance(position.get("settlement"), dict)
            else {}
        )
        lines += [
            (
                f"CTF payout {num(settlement.get('payout_per_token')):.4f}/token | "
                f"value {money(settlement.get('settlement_value_usd'))}"
            ),
            f"Finalized block: {settlement.get('finalized_block_number') or '?'}",
        ]

    if position.get("status") in ("CLOSED", "SETTLED"):
        lines += [
            (
                f"Realized P/L {money(position.get('realized_pnl_usd'))} "
                f"({num(position.get('realized_return_pct')):.2f}%)"
            )
        ]

    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)
    await update.message.reply_text(
        "💎 Diamond Intelligence online.\n\n"
        "/diamonds /markets /focus /positions /status"
    )


async def status_cmd(update, context):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)

    focus_status, focus_payload = focus_snapshot()
    book_status, book_payload = book_snapshot()
    ready, reason = execution_binding(
        focus_status,
        focus_payload,
        book_status,
        book_payload,
    )

    analysis_status, analysis_rows = analyses_snapshot()
    diamond_status, diamond_rows = diamonds_snapshot()
    paper_status, paper_payload = paper_state_snapshot()
    open_papers = (
        len([
            row
            for row in paper_payload.get("positions", [])
            if isinstance(row, dict) and row.get("status") == "OPEN"
        ])
        if paper_status == "OK"
        else "-"
    )

    lines = [
        "SYSTEM STATUS",
        f"Analysis snapshot: {analysis_status}",
        f"Markets analyzed: {len(analysis_rows) if analysis_status == 'OK' else '-'}",
        f"Diamond snapshot: {diamond_status}",
        f"Fresh Diamonds: {len(diamond_rows) if diamond_status == 'OK' else '-'}",
        f"Focus snapshot: {focus_status}",
        f"Book snapshot: {book_status}",
        f"Paper snapshot: {paper_status}",
        f"Open paper positions: {open_papers}",
        f"Execution ready: {'YES' if ready else 'NO'} ({reason})",
    ]

    await update.message.reply_text("\n".join(lines))


async def focus_cmd(update, context):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)

    focus_status, focus_payload = focus_snapshot()
    book_status, book_payload = book_snapshot()

    lines = [
        focus_message(focus_status, focus_payload),
        "",
        book_message(book_status, book_payload),
    ]

    ready, _ = execution_binding(
        focus_status,
        focus_payload,
        book_status,
        book_payload,
    )
    markup = None
    if ready:
        lines += ["", execution_message(focus_payload, book_payload)]
        markup = paper_open_keyboard()

    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def positions_cmd(update, context):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)
    status, payload = paper_state_snapshot()
    if status != "OK":
        await update.message.reply_text(f"Paper input: {status}")
        return

    positions = [
        row
        for row in payload.get("positions", [])
        if isinstance(row, dict)
    ]
    opens = [row for row in positions if row.get("status") == "OPEN"]

    if not opens:
        await update.message.reply_text("No open paper positions.")
        return

    for position in opens:
        await update.message.reply_text(
            paper_position_message(position),
            reply_markup=paper_close_keyboard(position.get("paper_id")),
        )


async def diamonds_cmd(update, context):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)
    snapshot_status, snapshot_rows = diamonds_snapshot()
    if snapshot_status != "OK":
        await update.message.reply_text(
            f"Diamond input: {snapshot_status}. No result is not treated as an empty result."
        )
        return

    rows = [
        row
        for row in snapshot_rows
        if isinstance(row, dict)
        and fresh(row.get("source_updated_at"))
        and fresh(row.get("last_trade_at"))
    ]

    if not rows:
        await update.message.reply_text("No verified Diamonds right now.")
        return

    for row in rows[:5]:
        await update.message.reply_text(
            short_message(row),
            reply_markup=keyboard(row),
        )


async def markets_cmd(update, context):
    if not authorized(update):
        return

    register_chat(update.effective_chat.id)
    snapshot_status, snapshot_rows = analyses_snapshot()
    if snapshot_status != "OK":
        await update.message.reply_text(
            f"Market input: {snapshot_status}. No result is not treated as an empty result."
        )
        return

    rows = [
        row
        for row in snapshot_rows
        if isinstance(row, dict)
        and (
            row.get("classification") != "LOW"
            or row.get("cashflow_alert", {}).get("active")
        )
    ][:6]

    if not rows:
        await update.message.reply_text("No interesting markets right now.")
        return

    for row in rows:
        await update.message.reply_text(
            short_message(row),
            reply_markup=keyboard(row),
        )


async def button(update, context):
    if not authorized(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("a:"):
        row = find_row(data.split(":", 1)[1])
        await query.message.reply_text(
            analysis_message(row) if row else "Market no longer available.",
            reply_markup=keyboard(row) if row else None,
        )
        return

    if data.startswith("po:"):
        amount = data.split(":", 1)[1]
        request_id, err = submit_open_request(amount, query.message.chat.id)
        if err:
            await query.message.reply_text("❌ " + err)
            return
        await query.message.reply_text(
            f"🧪 PAPER OPEN REQUEST SUBMITTED\n{request_id}\n"
            "The Paper Worker will revalidate READY + Book and use a fresh CLOB book."
        )
        return

    if data.startswith("pc:"):
        paper_id = data.split(":", 1)[1]
        request_id, err = submit_close_request(paper_id, query.message.chat.id)
        if err:
            await query.message.reply_text("❌ " + err)
            return
        await query.message.reply_text(
            f"🧪 PAPER CLOSE REQUEST SUBMITTED\n{request_id}\n"
            "The Paper Worker will simulate the exit against current bids."
        )
        return

    # Old Telegram messages may still contain legacy paper callback buttons.
    if data.startswith("p:"):
        await query.message.reply_text(
            "Legacy paper action rejected. Use a current READY + Book PAPER button."
        )


def _focus_alert_key(payload):
    focus = payload.get("focus")
    if not isinstance(focus, dict):
        return None

    return "|".join(
        [
            str(focus.get("token_id") or ""),
            str(focus.get("locked_at") or ""),
            str(payload.get("state") or ""),
        ]
    )


def _invalidated_key(payload):
    invalidated = payload.get("last_invalidated")
    if not isinstance(invalidated, dict):
        return None

    stamp = str(invalidated.get("invalidated_at") or "").strip()
    token = str(invalidated.get("token_id") or "").strip()
    return f"{token}|{stamp}" if token and stamp else None


def _execution_key(focus_payload):
    focus = focus_payload.get("focus")
    if not isinstance(focus, dict):
        return None

    token = str(focus.get("token_id") or "").strip()
    locked_at = str(focus.get("locked_at") or "").strip()
    return f"{token}|{locked_at}" if token and locked_at else None


async def watcher(app):
    current_state = state()

    active = set(current_state.get("active_diamonds", []))
    if not active:
        active = {
            row.get("market_key")
            for row in diamonds()
            if row.get("market_key")
        }

    cash_levels = (
        current_state.get("cashflow_levels", {})
        if isinstance(current_state.get("cashflow_levels"), dict)
        else {}
    )

    rank = {
        None: 0,
        "LARGE": 1,
        "RELATIVE_SURGE": 1,
        "VERY_LARGE": 2,
        "EXTREME": 3,
    }

    while True:
        try:
            diamond_status, diamond_rows = diamonds_snapshot()
            if diamond_status == "OK":
                rows = [
                    row
                    for row in diamond_rows
                    if isinstance(row, dict)
                    and fresh(row.get("source_updated_at"))
                    and fresh(row.get("last_trade_at"))
                ]
                current = {
                    row.get("market_key")
                    for row in rows
                    if row.get("market_key")
                }

                for row in rows:
                    market_key = row.get("market_key")
                    if market_key not in active:
                        for chat_id in chat_ids():
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        "💎 NEW VERIFIED DIAMOND\n\n"
                                        + short_message(row)
                                        + "\n\nFocus/Risk still decide whether it can become READY."
                                    ),
                                )
                            except Exception as exc:
                                print("[TELEGRAM]", exc)

                active = current

            analysis_status, analysis_rows = analyses_snapshot()
            if analysis_status == "OK":
                current_cash = {}
                for row in analysis_rows:
                    if not isinstance(row, dict):
                        continue

                    cash = row.get("cashflow_alert", {})
                    tier = cash.get("tier")
                    market_key = row.get("market_key")

                    if (
                        fresh(row.get("source_updated_at"))
                        and fresh(row.get("last_trade_at"))
                        and cash.get("active")
                        and rank.get(tier, 0) >= 2
                        and cash.get("quality") != "WHALE_DOMINATED"
                        and num(row.get("scores", {}).get("signal_quality")) >= 65
                    ):
                        current_cash[market_key] = tier

                        if rank.get(tier, 0) > rank.get(cash_levels.get(market_key), 0):
                            for chat_id in chat_ids():
                                try:
                                    await app.bot.send_message(
                                        chat_id=chat_id,
                                        text=(
                                            f"💰 {tier} CASHFLOW WATCH\n\n"
                                            + short_message(row)
                                            + "\n\nNot automatically READY."
                                        ),
                                    )
                                except Exception as exc:
                                    print("[TELEGRAM]", exc)

                cash_levels = current_cash

            focus_status, focus_payload = focus_snapshot()
            book_status, book_payload = book_snapshot()

            # Bad Focus/Book inputs never erase dedupe state or fabricate lifecycle.
            if focus_status == "OK":
                focus_key = _focus_alert_key(focus_payload)
                if focus_key and focus_key != current_state.get("focus_alert_key"):
                    for chat_id in chat_ids():
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=focus_message("OK", focus_payload),
                            )
                        except Exception as exc:
                            print("[TELEGRAM]", exc)
                    current_state["focus_alert_key"] = focus_key

                invalidated_key = _invalidated_key(focus_payload)
                if (
                    invalidated_key
                    and invalidated_key != current_state.get("focus_invalidated_key")
                ):
                    invalidated = focus_payload.get("last_invalidated") or {}
                    reasons = ", ".join(
                        invalidated.get("invalidation_reasons") or []
                    ) or "-"
                    for chat_id in chat_ids():
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    "🔴 FOCUS INVALIDATED\n\n"
                                    f"{invalidated.get('question') or invalidated.get('token_id')}\n"
                                    f"Reasons: {reasons}"
                                ),
                            )
                        except Exception as exc:
                            print("[TELEGRAM]", exc)
                    current_state["focus_invalidated_key"] = invalidated_key

            ready, _ = execution_binding(
                focus_status,
                focus_payload,
                book_status,
                book_payload,
            )

            if ready:
                execution_key = _execution_key(focus_payload)
                if (
                    execution_key
                    and execution_key != current_state.get("execution_ready_key")
                ):
                    for chat_id in chat_ids():
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=execution_message(focus_payload, book_payload),
                                reply_markup=paper_open_keyboard(),
                            )
                        except Exception as exc:
                            print("[TELEGRAM]", exc)
                    current_state["execution_ready_key"] = execution_key

            paper_status, paper_payload = paper_state_snapshot()
            if paper_status == "OK":
                seen_open = set(current_state.get("paper_open_alerts") or [])
                seen_closed = set(current_state.get("paper_closed_alerts") or [])
                seen_settled = set(current_state.get("paper_settled_alerts") or [])

                for position in paper_payload.get("positions", []):
                    if not isinstance(position, dict):
                        continue

                    paper_id = str(position.get("paper_id") or "").strip()
                    if not paper_id:
                        continue

                    if position.get("status") == "OPEN" and paper_id not in seen_open:
                        for chat_id in chat_ids():
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text="🧪 PAPER OPENED\n\n" + paper_position_message(position),
                                    reply_markup=paper_close_keyboard(paper_id),
                                )
                            except Exception as exc:
                                print("[TELEGRAM]", exc)
                        seen_open.add(paper_id)

                    if position.get("status") == "CLOSED" and paper_id not in seen_closed:
                        for chat_id in chat_ids():
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text="🏁 PAPER CLOSED\n\n" + paper_position_message(position),
                                )
                            except Exception as exc:
                                print("[TELEGRAM]", exc)
                        seen_closed.add(paper_id)

                    if position.get("status") == "SETTLED" and paper_id not in seen_settled:
                        for chat_id in chat_ids():
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text="✅ PAPER SETTLED\n\n" + paper_position_message(position),
                                )
                            except Exception as exc:
                                print("[TELEGRAM]", exc)
                        seen_settled.add(paper_id)

                current_state["paper_open_alerts"] = sorted(seen_open)[-200:]
                current_state["paper_closed_alerts"] = sorted(seen_closed)[-200:]
                current_state["paper_settled_alerts"] = sorted(seen_settled)[-200:]

            current_state["active_diamonds"] = sorted(x for x in active if x)
            current_state["cashflow_levels"] = cash_levels
            current_state["chat_ids"] = sorted(chat_ids())
            save(STATE, current_state)

        except Exception as exc:
            print("[TELEGRAM WATCHER]", repr(exc))

        await asyncio.sleep(max(3, WATCH_INTERVAL))


async def post_init(app):
    app.create_task(watcher(app))


def main():
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    if not ENV_CHAT:
        raise SystemExit(
            "Missing TELEGRAM_CHAT_ID: required for private bot access"
        )

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    for name, fn in [
        ("start", start_cmd),
        ("status", status_cmd),
        ("diamonds", diamonds_cmd),
        ("markets", markets_cmd),
        ("focus", focus_cmd),
        ("positions", positions_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button))

    print("[TELEGRAM] Focus/Book/Paper interface starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
