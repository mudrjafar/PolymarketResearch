import json
import math
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from machine_common import fresh, save_json_atomic


TRADES_FILE = "data/live_trades.jsonl"
FLOW_STATE_FILE = "data/flow_state.json"
VERIFICATION_STATE_FILE = "data/verification_state.json"

# Token-level flow with event-time windows (consumed by diamond_filter_v3).
FLOW_SCHEMA_VERSION = 4


# ============================================================
# ADAPTIVE MARKET HORIZON / RETENTION
# ============================================================

# Upper bound on rows read from the collector's bounded live view.
MAX_TRADE_LINES = 50000

# The tracker wakes every few seconds, but each market has its own due time.
GLOBAL_TICK_SECONDS = 5

# Low-interest markets are scheduled according to time-to-resolution.
HORIZON_INTERVALS = {
    "ULTRA_FAR": 30 * 60,   # > 90 days
    "FAR": 15 * 60,         # 30-90 days
    "MID": 5 * 60,          # 7-30 days
    "NEAR": 60,             # 1-7 days
    "SOON": 30,             # 6-24 hours
    "URGENT": 15,           # <= 6 hours
    "UNKNOWN": 5 * 60,
}

# Far-future markets do not enter the expensive fast-analysis loop for
# tiny routine trades. They wake immediately only when recent activity is
# unusually large enough to deserve attention.
WAKE_THRESHOLDS = {
    "ULTRA_FAR": {
        "latest_trade_usd": 1000.0,
        "volume_15m": 2500.0,
        "trades_15m": 8,
    },
    "FAR": {
        "latest_trade_usd": 500.0,
        "volume_15m": 1500.0,
        "trades_15m": 6,
    },
    "MID": {
        "latest_trade_usd": 250.0,
        "volume_15m": 750.0,
        "trades_15m": 5,
    },
}

# Runtime scheduler only (keyed by token_id). Verification state itself
# remains persisted.
analysis_schedule = {}


# ============================================================
# ADAPTIVE INTERVALS
# ============================================================

LOW_INTERVAL = 60
CANDIDATE_INTERVAL = 15
VERIFY_INTERVAL = 10
VERIFIED_INTERVAL = 5


# ============================================================
# THRESHOLDS
# ============================================================

MIN_CANDIDATE_TRADES_5M = 5
MIN_CANDIDATE_VOLUME_5M = 250.0

MIN_VERIFY_TRADES_5M = 8
MIN_VERIFY_VOLUME_5M = 500.0

MIN_DIRECTIONAL_STRENGTH_5M = 0.35
MIN_DIRECTIONAL_STRENGTH_15M = 0.25

MAX_SINGLE_TRADE_RATIO = 0.70

REQUIRED_CONFIRMATIONS = 3


# ============================================================
# WINDOWS
# ============================================================

WINDOWS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
}


# ============================================================
# TIME / RESOLUTION
# ============================================================

def get_resolution_state(end_date):
    if not end_date:
        return "UNKNOWN"

    try:
        end = parse_time(end_date)
        now = datetime.now(timezone.utc)

        remaining = end - now
        seconds = remaining.total_seconds()

        if seconds <= 0:
            return "EXPIRED"

        if seconds <= 5 * 60:
            return "CRITICAL"

        if seconds <= 30 * 60:
            return "IMMINENT"

        if seconds <= 6 * 60 * 60:
            return "CLOSING_SOON"

        if seconds <= 24 * 60 * 60:
            return "ACTIVE"

        return "FAR"

    except Exception:
        return "UNKNOWN"


def get_remaining_seconds(end_date):
    if not end_date:
        return None

    try:
        end = parse_time(end_date)
        now = datetime.now(timezone.utc)

        return max(0, int((end - now).total_seconds()))

    except Exception:
        return None


def format_remaining(seconds):
    if seconds is None:
        return "UNKNOWN"

    if seconds <= 0:
        return "EXPIRED"

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60

    if days > 0:
        return f"{days}d {hours}h {minutes}m"

    if hours > 0:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


# ============================================================
# FILE LOADING
# ============================================================

CURRENT_COLLECTOR_VERSION = 4
SUPPORTED_LEGACY_COLLECTOR_VERSIONS = {3}


def _collector_version_class(trade):
    if not isinstance(trade, dict):
        return "UNKNOWN"
    if "collector_version" not in trade or trade.get("collector_version") is None:
        return "LEGACY"
    version = trade.get("collector_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return "UNKNOWN"
    if version == CURRENT_COLLECTOR_VERSION:
        return "V4"
    if version in SUPPORTED_LEGACY_COLLECTOR_VERSIONS:
        return "LEGACY"
    return "UNKNOWN"


def _is_v4_trade(trade):
    return _collector_version_class(trade) == "V4"


def _real_nonnegative_int(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _strict_aware_time(value):
    if value is None:
        raise ValueError("timestamp missing")
    if isinstance(value, datetime):
        dt = value
    else:
        text_value = str(value).strip()
        if not text_value:
            raise ValueError("timestamp missing")
        if text_value.endswith("Z"):
            text_value = text_value[:-1] + "+00:00"
        dt = datetime.fromisoformat(text_value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def event_time(trade):
    """
    Current collector V4 rows use canonical block time only. Legacy rows may
    still fall back to detected_at for backward-compatible offline handling.
    """
    if _is_v4_trade(trade):
        return _strict_aware_time(trade.get("block_timestamp"))
    return parse_time(trade.get("block_timestamp") or trade.get("detected_at"))


def evidence_identity(trade):
    """Stable blockchain identity of one trade event, or None if unknown."""
    tx = trade.get("transaction_hash")
    log_index = trade.get("log_index")

    if not isinstance(tx, str) or not tx.strip():
        return None
    if not _real_nonnegative_int(log_index):
        return None

    return f"{tx.strip().lower()}:{log_index}"


def is_flow_eligible_trade(trade):
    """
    Strict gate for current collector V4 rows before they can affect live
    flow mathematics. Only explicitly supported legacy schemas retain their
    historical compatibility path. Unknown/future versions fail closed.
    """
    version_class = _collector_version_class(trade)
    if version_class == "UNKNOWN":
        return False
    if version_class == "LEGACY":
        return True

    if not isinstance(trade.get("condition_id"), str) or not trade["condition_id"].strip():
        return False
    if not isinstance(trade.get("token_id"), str) or not trade["token_id"].strip():
        return False
    if not isinstance(trade.get("outcome"), str) or not trade["outcome"].strip():
        return False
    if evidence_identity(trade) is None:
        return False
    if not _real_nonnegative_int(trade.get("block")):
        return False
    try:
        event_time(trade)
    except (TypeError, ValueError):
        return False
    if get_trade_side(trade) not in {"BUY", "SELL"}:
        return False
    if get_trade_size(trade) is None:
        return False
    return True


def trade_order_key(trade):
    """Deterministic chain order: event time, block, log index."""
    block = trade.get("block")
    log_index = trade.get("log_index")

    return (
        event_time(trade),
        -1 if block is None else int(block),
        -1 if log_index is None else int(log_index),
    )


def load_trades():
    """
    Load token-identified trades whose event time lies in the 15m horizon.

    The collector atomically REPLACES live_trades.jsonl with a bounded
    20-minute view on every batch, so the file is re-read completely each
    cycle (a byte-offset cursor would skip rows after a replacement). Rows
    are deduplicated by stable blockchain identity.

    Returns None when the file cannot be read, so callers keep their state.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - WINDOWS["15m"]

    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            lines = deque(f, maxlen=MAX_TRADE_LINES)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return None

    trades = []
    seen = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            trade = json.loads(line)
            if not isinstance(trade, dict):
                continue
            if not trade.get("token_id") or not trade.get("condition_id"):
                continue
            if not is_flow_eligible_trade(trade):
                continue
            if not cutoff <= event_time(trade) <= now:
                continue
            trade_order_key(trade)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        identity = evidence_identity(trade)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)

        trades.append(trade)

    return trades


# ============================================================
# TIME PARSING
# ============================================================

def parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        value = str(value)

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


# ============================================================
# TRADE SIZE / SIDE
# ============================================================

def _finite_nonnegative_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def get_trade_size(trade):
    """
    Return USD value for one trade.

    UNKNOWN is None, never an invented $0. Current V4 collector rows must
    provide trade_usd directly. Legacy rows may use historical USD fields or
    reconstruct USD only from share count multiplied by a valid share price.
    """

    if _is_v4_trade(trade):
        return _finite_nonnegative_number(trade.get("trade_usd"))

    for value in (trade.get("trade_usd"), trade.get("usd_size")):
        size = _finite_nonnegative_number(value)
        if size is not None:
            return size

    shares = _finite_nonnegative_number(trade.get("token_amount"))
    try:
        price = float(trade.get("fill_price") or trade.get("price"))
    except (TypeError, ValueError):
        price = None
    if (
        shares is not None
        and price is not None
        and math.isfinite(price)
        and 0 < price < 1
    ):
        return shares * price

    return None


def get_trade_side(trade):
    """
    Returns BUY / SELL.

    Primary source:
        side_label

    Fallback:
        side
        taker_side
    """

    side = trade.get("side_label")

    if side is None:
        side = trade.get("side")

    if side is None:
        side = trade.get("taker_side")

    if side is None:
        return ""

    side = str(side).upper().strip()

    # Collector already provides BUY / SELL.
    if side in {"BUY", "SELL"}:
        return side

    # Some older data may encode side numerically.
    if side == "0":
        return "BUY"

    if side == "1":
        return "SELL"

    return ""


# ============================================================
# FLOW ANALYSIS
# ============================================================

def analyze_flow(trades, now, window):
    cutoff = now - WINDOWS[window]

    recent = []

    # Rolling event-time window: (now - duration) <= block time <= now.
    for trade in trades:
        try:
            if cutoff <= event_time(trade) <= now:
                recent.append(trade)

        except Exception:
            continue

    buy_volume = 0.0
    sell_volume = 0.0

    buy_trades = 0
    sell_trades = 0

    largest_trade = 0.0

    # IMPORTANT:
    # Only count trades that have a valid BUY/SELL side.
    valid_trades = 0

    for trade in recent:
        if not is_flow_eligible_trade(trade):
            continue

        size = get_trade_size(trade)
        side = get_trade_side(trade)

        if size is None or side not in {"BUY", "SELL"}:
            continue

        valid_trades += 1

        if size > largest_trade:
            largest_trade = size

        if side == "BUY":
            buy_volume += size
            buy_trades += 1

        elif side == "SELL":
            sell_volume += size
            sell_trades += 1

    total_volume = buy_volume + sell_volume

    net_flow = buy_volume - sell_volume

    trade_count = valid_trades

    if total_volume > 0:
        directional_strength = (
            abs(net_flow) / total_volume
        )
    else:
        directional_strength = 0.0

    if net_flow > 0:
        direction = "BUY"

    elif net_flow < 0:
        direction = "SELL"

    else:
        direction = "NEUTRAL"

    if total_volume > 0:
        largest_trade_ratio = (
            largest_trade / total_volume
        )
    else:
        largest_trade_ratio = 0.0

    if trade_count > 0:
        average_trade = (
            total_volume / trade_count
        )
    else:
        average_trade = 0.0

    return {
        "window": window,
        "trade_count": trade_count,
        "buy_trades": buy_trades,
        "sell_trades": sell_trades,
        "buy_volume": round(buy_volume, 2),
        "sell_volume": round(sell_volume, 2),
        "total_volume": round(total_volume, 2),
        "net_flow": round(net_flow, 2),
        "direction": direction,
        "directional_strength": round(
            directional_strength,
            4,
        ),
        "average_trade": round(
            average_trade,
            2,
        ),
        "largest_trade": round(
            largest_trade,
            2,
        ),
        "largest_trade_ratio": round(
            largest_trade_ratio,
            4,
        ),
    }


# ============================================================
# INTEREST SCORE
# ============================================================

def calculate_interest_score(
    flow_1m,
    flow_5m,
    flow_15m,
):
    score = 0.0

    # 5m directional strength
    score += (
        flow_5m["directional_strength"] * 30
    )

    # 15m directional strength
    score += (
        flow_15m["directional_strength"] * 20
    )

    # Direction agreement
    directions = [
        flow_1m["direction"],
        flow_5m["direction"],
        flow_15m["direction"],
    ]

    non_neutral = [
        direction
        for direction in directions
        if direction != "NEUTRAL"
    ]

    if (
        len(non_neutral) == 3
        and len(set(non_neutral)) == 1
    ):
        score += 25

    elif len(non_neutral) >= 2:
        if len(set(non_neutral)) == 1:
            score += 15

    # Trade count
    trade_count = flow_5m["trade_count"]

    if trade_count >= 20:
        score += 15

    elif trade_count >= 10:
        score += 10

    elif trade_count >= 5:
        score += 5

    # Large trade
    if flow_5m["largest_trade"] >= 1000:
        score += 5

    # Acceleration
    if flow_5m["total_volume"] > 0:

        expected_1m_volume = (
            flow_5m["total_volume"] / 5
        )

        if (
            flow_1m["total_volume"]
            > expected_1m_volume * 2
        ):
            score += 5

    return round(
        min(score, 100),
        2,
    )


# ============================================================
# DATA CONFIDENCE
# ============================================================

def calculate_data_confidence(
    flow_5m,
    flow_1m,
    flow_15m,
):
    score = 0.0

    # Trade count
    trade_count = flow_5m["trade_count"]

    if trade_count >= 20:
        score += 30

    elif trade_count >= 10:
        score += 20

    elif trade_count >= 5:
        score += 10

    # Volume
    volume = flow_5m["total_volume"]

    if volume >= 1000:
        score += 25

    elif volume >= 500:
        score += 15

    elif volume >= 250:
        score += 8

    # Directional windows
    directional_windows = 0

    for flow in [
        flow_1m,
        flow_5m,
        flow_15m,
    ]:
        if flow["direction"] != "NEUTRAL":
            directional_windows += 1

    if directional_windows == 3:
        score += 30

    elif directional_windows == 2:
        score += 20

    elif directional_windows == 1:
        score += 10

    # Concentration
    #
    # Do not give concentration points when
    # there is no volume / no valid trades.
    if flow_5m["total_volume"] > 0:

        if flow_5m["largest_trade_ratio"] <= 0.40:
            score += 15

        elif flow_5m["largest_trade_ratio"] <= 0.70:
            score += 8

    return round(
        min(score, 100),
        2,
    )


# ============================================================
# CANDIDATE CHECK
# ============================================================

def candidate_check(
    flow_5m,
    flow_15m,
):
    reasons = []

    if (
        flow_5m["trade_count"]
        < MIN_CANDIDATE_TRADES_5M
    ):
        reasons.append(
            f"5m trades < "
            f"{MIN_CANDIDATE_TRADES_5M}"
        )

    if (
        flow_5m["total_volume"]
        < MIN_CANDIDATE_VOLUME_5M
    ):
        reasons.append(
            f"5m volume < "
            f"${MIN_CANDIDATE_VOLUME_5M:.0f}"
        )

    if (
        flow_5m["directional_strength"]
        < MIN_DIRECTIONAL_STRENGTH_5M
    ):
        reasons.append(
            f"5m direction < "
            f"{MIN_DIRECTIONAL_STRENGTH_5M:.2f}"
        )

    if (
        flow_15m["directional_strength"]
        < MIN_DIRECTIONAL_STRENGTH_15M
    ):
        reasons.append(
            f"15m direction < "
            f"{MIN_DIRECTIONAL_STRENGTH_15M:.2f}"
        )

    if flow_5m["direction"] == "NEUTRAL":
        reasons.append(
            "5m direction neutral"
        )

    passed = len(reasons) == 0

    return passed, reasons


# ============================================================
# VERIFICATION CHECK
# ============================================================

def verification_check(
    flow_1m,
    flow_5m,
    flow_15m,
):
    reasons = []

    if (
        flow_5m["trade_count"]
        < MIN_VERIFY_TRADES_5M
    ):
        reasons.append(
            f"5m trades < "
            f"{MIN_VERIFY_TRADES_5M}"
        )

    if (
        flow_5m["total_volume"]
        < MIN_VERIFY_VOLUME_5M
    ):
        reasons.append(
            f"5m volume < "
            f"${MIN_VERIFY_VOLUME_5M:.0f}"
        )

    if (
        flow_5m["directional_strength"]
        < MIN_DIRECTIONAL_STRENGTH_5M
    ):
        reasons.append(
            f"5m direction < "
            f"{MIN_DIRECTIONAL_STRENGTH_5M:.2f}"
        )

    if (
        flow_15m["directional_strength"]
        < MIN_DIRECTIONAL_STRENGTH_15M
    ):
        reasons.append(
            f"15m direction < "
            f"{MIN_DIRECTIONAL_STRENGTH_15M:.2f}"
        )

    directions = [
        flow_1m["direction"],
        flow_5m["direction"],
        flow_15m["direction"],
    ]

    if any(
        direction == "NEUTRAL"
        for direction in directions
    ):
        reasons.append(
            "not all windows directional"
        )

    elif len(set(directions)) != 1:
        reasons.append(
            "window directions disagree"
        )

    passed = len(reasons) == 0

    return passed, reasons


# ============================================================
# ADAPTIVE HORIZON / WAKE LOGIC
# ============================================================

def get_analysis_profile(remaining_seconds):
    if remaining_seconds is None:
        return "UNKNOWN"

    days = remaining_seconds / 86400.0
    hours = remaining_seconds / 3600.0

    if days > 90:
        return "ULTRA_FAR"

    if days > 30:
        return "FAR"

    if days > 7:
        return "MID"

    if days > 1:
        return "NEAR"

    if hours > 6:
        return "SOON"

    return "URGENT"


def get_adaptive_interval(profile, state):
    """
    Candidate/verification states always override the slow horizon schedule.
    A far-future market can therefore instantly escalate to rapid monitoring
    once it produces a meaningful signal.
    """
    if state == "VERIFIED":
        return VERIFIED_INTERVAL

    if state == "VERIFYING":
        return VERIFY_INTERVAL

    if state == "CANDIDATE":
        return CANDIDATE_INTERVAL

    return HORIZON_INTERVALS.get(
        profile,
        HORIZON_INTERVALS["UNKNOWN"],
    )


def market_evidence_id(latest_trade):
    identity = evidence_identity(latest_trade)

    if identity is not None:
        return identity

    # Legacy rows without chain identity.
    return (
        f"legacy:{latest_trade.get('token_id','')}:"
        f"{latest_trade.get('detected_at','')}"
    )


def confirmation_evidence_id(latest_trade, evidence_id, new_evidence, flow_direction):
    """Return confirmation evidence only for a fresh canonical chain event supporting the flow."""
    if evidence_identity(latest_trade) is None:
        return None
    latest_side = get_trade_side(latest_trade)
    if (
        new_evidence
        and evidence_id
        and flow_direction in {"BUY", "SELL"}
        and latest_side == flow_direction
    ):
        return evidence_id
    return None


def confirmation_evidence_triple(latest_trade, confirming_evidence):
    """
    Flow is the producer authority for the evidence triple. All three fields
    are derived together from the exact same confirming trade event.
    """
    if not confirming_evidence:
        return None
    identity = evidence_identity(latest_trade)
    if identity != confirming_evidence:
        return None
    block = latest_trade.get("block")
    log_index = latest_trade.get("log_index")
    if not _real_nonnegative_int(block) or not _real_nonnegative_int(log_index):
        return None
    try:
        evidence_at = event_time(latest_trade).isoformat()
    except (TypeError, ValueError):
        return None
    return {
        "evidence_id": identity,
        "evidence_cursor": [block, log_index],
        "evidence_at": evidence_at,
    }



def far_market_wake_trigger(
    profile,
    latest_trade,
    flow_15m,
):
    """
    Cheap wake-up gate for distant markets.

    Near/urgent markets are always allowed to wake on fresh evidence.
    Far markets wake early only for notable activity; otherwise they wait
    for their scheduled low-frequency review.
    """
    if profile not in WAKE_THRESHOLDS:
        return True

    threshold = WAKE_THRESHOLDS[profile]
    latest_trade_usd = get_trade_size(latest_trade)

    return (
        (
            latest_trade_usd is not None
            and latest_trade_usd >= threshold["latest_trade_usd"]
        )
        or flow_15m["total_volume"] >= threshold["volume_15m"]
        or flow_15m["trade_count"] >= threshold["trades_15m"]
    )


def scheduler_entry(token_id):
    if token_id not in analysis_schedule:
        analysis_schedule[token_id] = {
            "next_due": 0.0,
            "last_seen_evidence": None,
            "last_analysis_evidence": None,
        }

    return analysis_schedule[token_id]


# ============================================================
# MARKET STATE
# ============================================================

def load_market_states():
    try:
        with open(VERIFICATION_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_json_atomic(path, data):
    # Accepts str or pathlib.Path; unique temp file + replace with retry.
    save_json_atomic(path, data)


def persist_market_states():
    try:
        _save_json_atomic(VERIFICATION_STATE_FILE, market_states)
    except OSError as exc:
        print(f"[WARNING] Could not persist verification state: {exc}")


market_states = load_market_states()


def update_market_state(
    market_key,
    candidate,
    verification_passed,
    evidence_id,
    direction=None,
    evidence_cursor=None,
    evidence_at=None,
):
    """
    market_key is the token_id: confirmations never cross between outcomes.
    """
    if market_key not in market_states:
        market_states[market_key] = {
            "state": "LOW",
            "confirmations": 0,
            "last_confirmation_evidence": None,
        }

    state_data = market_states[market_key]

    # Confirmations belong to one direction and one continuous observation:
    # a reversal, or a gap since the last check (restart/downtime), restarts
    # the count. The consumed evidence id is kept so it cannot count again.
    if (
        direction != state_data.get("direction")
        or not fresh(state_data.get("last_checked_at"))
    ):
        state_data["confirmations"] = 0

    state_data["direction"] = direction
    state_data["last_checked_at"] = datetime.now(timezone.utc).isoformat()

    # A confirmation must contain NEW trade evidence. Re-scanning the same
    # unchanged 5-minute window does not count as another confirmation.
    if verification_passed:
        if evidence_id and evidence_id != state_data.get("last_confirmation_evidence"):
            state_data["confirmations"] = min(
                REQUIRED_CONFIRMATIONS,
                int(state_data.get("confirmations", 0)) + 1,
            )
            state_data["last_confirmation_evidence"] = evidence_id
            state_data["last_confirmation_cursor"] = evidence_cursor
            state_data["last_confirmation_at"] = evidence_at
    else:
        state_data["confirmations"] = 0

    if state_data["confirmations"] >= REQUIRED_CONFIRMATIONS:
        state_data["state"] = "VERIFIED"
    elif candidate:
        state_data["state"] = "VERIFYING"
    else:
        state_data["state"] = "LOW"

    # Persist once per tracker cycle, not once per market.
    return state_data


def get_interval(state):
    if state == "VERIFIED":
        return VERIFIED_INTERVAL

    if state == "VERIFYING":
        return VERIFY_INTERVAL

    if state == "CANDIDATE":
        return CANDIDATE_INTERVAL

    return LOW_INTERVAL


# ============================================================
# SAVE STRUCTURED FLOW STATE
# ============================================================

def load_flow_states():
    try:
        with open(FLOW_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


flow_states = load_flow_states()


def persist_flow_states():
    try:
        _save_json_atomic(FLOW_STATE_FILE, flow_states)
    except OSError as exc:
        print(f"[WARNING] Could not save flow state: {exc}")


def save_flow_state(
    token_id,
    condition_id,
    market,
    flow_1m,
    flow_5m,
    flow_15m,
    interest_score,
    data_confidence,
    resolution_state,
    remaining_seconds,
    analysis_profile,
    analysis_interval_seconds,
    state_data,
    candidate,
    candidate_reasons,
    verified,
    verification_reasons,
):
    # Update RAM only. The whole state is flushed once after the market loop.
    # One entry per token_id (outcome); condition_id links the parent market.
    updated_at = datetime.now(timezone.utc).isoformat()

    flow_states[token_id] = {
        "schema_version": FLOW_SCHEMA_VERSION,
        "token_id": token_id,
        "condition_id": condition_id,
        "outcome": market.get("outcome"),
        "direction": flow_5m["direction"],
        "last_trade_at": market.get("last_trade_at"),
        "evidence_id": state_data.get("last_confirmation_evidence"),
        "evidence_cursor": state_data.get("last_confirmation_cursor"),
        "evidence_at": state_data.get("last_confirmation_at"),
        "source_updated_at": updated_at,
        "market": market,
        "flow_1m": flow_1m,
        "flow_5m": flow_5m,
        "flow_15m": flow_15m,
        "interest_score": interest_score,
        "data_confidence": data_confidence,
        "resolution_state": resolution_state,
        "remaining_seconds": remaining_seconds,
        "analysis_profile": analysis_profile,
        "analysis_interval_seconds": analysis_interval_seconds,
        "state": state_data["state"],
        "confirmations": state_data["confirmations"],
        "candidate": candidate,
        "candidate_reasons": candidate_reasons,
        "verified": state_data["state"] == "VERIFIED",
        "verification_check_passed": verified,
        "verification_reasons": verification_reasons,
        "updated_at": updated_at,
    }


# ============================================================
# PRINT MARKET
# ============================================================

def print_market(
    condition_id,
    question,
    outcome,
    end_date,
    resolution_state,
    remaining_seconds,
    analysis_profile,
    analysis_interval_seconds,
    flow_1m,
    flow_5m,
    flow_15m,
    interest_score,
    data_confidence,
    state_data,
    candidate,
    candidate_reasons,
    verified,
    verification_reasons,
):
    print()
    print("=" * 70)

    print(f"MARKET: {question}")
    print(f"CONDITION ID: {condition_id}")
    print(f"OUTCOME: {outcome}")
    print(f"END DATE: {end_date}")

    print(
        f"TIME TO RESOLUTION: "
        f"{format_remaining(remaining_seconds)}"
    )

    print(
        f"RESOLUTION STATE: {resolution_state}"
    )

    print(
        f"ANALYSIS PROFILE: {analysis_profile}"
    )

    print(
        f"BASE ANALYSIS INTERVAL: "
        f"{analysis_interval_seconds} sec"
    )

    print()

    print(
        f"Interest score: "
        f"{interest_score:.1f} / 100"
    )

    print(
        f"Data confidence: "
        f"{data_confidence:.1f} / 100"
    )

    print()

    print(
        f"STATE: {state_data['state']}"
    )

    print(
        f"Confirmations: "
        f"{state_data['confirmations']}"
    )

    for flow in [
        flow_1m,
        flow_5m,
        flow_15m,
    ]:
        print()
        print(
            f"{flow['window'].upper()} FLOW"
        )

        print(
            f"Trades: "
            f"{flow['trade_count']}"
        )

        print(
            f"Volume: "
            f"${flow['total_volume']:.2f}"
        )

        print(
            f"BUY: "
            f"${flow['buy_volume']:.2f}"
        )

        print(
            f"SELL: "
            f"${flow['sell_volume']:.2f}"
        )

        print(
            f"Net flow: "
            f"${flow['net_flow']:.2f}"
        )

        print(
            f"Direction: "
            f"{flow['direction']}"
        )

        print(
            f"Strength: "
            f"{flow['directional_strength']:.3f}"
        )

        print(
            f"Average trade: "
            f"${flow['average_trade']:.2f}"
        )

        print(
            f"Largest trade: "
            f"${flow['largest_trade']:.2f}"
        )

        print(
            f"Largest trade ratio: "
            f"{flow['largest_trade_ratio']:.3f}"
        )

    print()

    print(
        f"Candidate: {candidate}"
    )

    if candidate_reasons:
        print(
            "Candidate reasons: "
            + ", ".join(candidate_reasons)
        )

    print(
        f"Verification: {verified}"
    )

    if verification_reasons:
        print(
            "Verification reasons: "
            + ", ".join(
                verification_reasons
            )
        )

    print()

    print(
        f"Next analysis: "
        f"{analysis_interval_seconds} sec"
    )

    print("=" * 70)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print()
    print("=" * 70)
    print("POLYMARKET FLOW TRACKER V3.2 - ADAPTIVE HORIZON")
    print("=" * 70)
    print(
        "Far-future markets stay dormant unless meaningful new flow wakes them."
    )
    print()

    while True:
        cycle_start = time.time()
        trades = load_trades()

        if trades is None:
            print("[WARNING] Could not read live trades; keeping previous state.")
            time.sleep(GLOBAL_TICK_SECONDS)
            continue

        if not trades:
            # No token has evidence inside the 15m horizon: nothing is current.
            if market_states or flow_states:
                market_states.clear()
                flow_states.clear()
                analysis_schedule.clear()
                persist_market_states()
                persist_flow_states()
            print("[INFO] No recent trades available yet.")
            time.sleep(GLOBAL_TICK_SECONDS)
            continue

        now = datetime.now(timezone.utc)
        now_ts = time.time()

        # Authoritative market identity: token_id (one outcome of one
        # condition). YES and NO never share flow or verification state.
        markets = defaultdict(list)

        for trade in trades:
            markets[str(trade["token_id"])].append(trade)

        # Tokens without evidence in the 15m horizon are no longer tracked.
        pruned = 0
        for store in (market_states, flow_states, analysis_schedule):
            for old_key in set(store) - set(markets):
                del store[old_key]
                pruned += 1

        analyzed_count = 0
        dormant_skipped = 0
        wakeups = 0

        for token_id, market_trades in markets.items():
            latest_trade = max(
                market_trades,
                key=trade_order_key,
            )

            condition_id = str(latest_trade.get("condition_id"))

            question = latest_trade.get(
                "question",
                "Unknown market",
            )

            outcome = latest_trade.get(
                "outcome",
                "Unknown",
            )

            end_date = latest_trade.get("end_date")

            resolution_state = get_resolution_state(
                end_date
            )

            remaining_seconds = get_remaining_seconds(
                end_date
            )

            profile = get_analysis_profile(
                remaining_seconds
            )

            schedule = scheduler_entry(
                token_id
            )

            evidence_id = market_evidence_id(
                latest_trade
            )

            new_evidence = (
                evidence_id
                and evidence_id
                != schedule.get("last_seen_evidence")
            )

            existing_state = market_states.get(
                token_id,
                {},
            ).get("state", "LOW")

            due = now_ts >= float(
                schedule.get("next_due", 0.0)
            )

            wake = False
            flow_15m_prefilter = None

            # Fast path: unchanged + not due means zero flow-window work.
            if not due and not new_evidence:
                dormant_skipped += 1
                continue

            if new_evidence:
                if existing_state in {
                    "VERIFYING",
                    "VERIFIED",
                }:
                    wake = True
                else:
                    # Only compute the 15m wake prefilter when fresh evidence
                    # actually needs to be judged.
                    flow_15m_prefilter = analyze_flow(
                        market_trades,
                        now,
                        "15m",
                    )
                    wake = far_market_wake_trigger(
                        profile,
                        latest_trade,
                        flow_15m_prefilter,
                    )

            should_analyze = due or wake

            # We have seen this trade even if we intentionally leave the
            # distant market dormant.
            if new_evidence:
                schedule["last_seen_evidence"] = evidence_id

            if not should_analyze:
                dormant_skipped += 1
                continue

            if wake and not due:
                wakeups += 1

            flow_1m = analyze_flow(
                market_trades,
                now,
                "1m",
            )

            flow_5m = analyze_flow(
                market_trades,
                now,
                "5m",
            )

            flow_15m = (
                flow_15m_prefilter
                if flow_15m_prefilter is not None
                else analyze_flow(market_trades, now, "15m")
            )

            interest_score = calculate_interest_score(
                flow_1m,
                flow_5m,
                flow_15m,
            )

            data_confidence = calculate_data_confidence(
                flow_5m,
                flow_1m,
                flow_15m,
            )

            candidate, candidate_reasons = candidate_check(
                flow_5m,
                flow_15m,
            )

            verified, verification_reasons = verification_check(
                flow_1m,
                flow_5m,
                flow_15m,
            )

            # Verification is token-specific. A fresh blockchain event only
            # confirms the current flow direction when that event itself points
            # in the same direction as the 5m flow. Opposing evidence may still
            # trigger analysis, but it cannot advance confirmations.
            confirming_evidence = confirmation_evidence_id(
                latest_trade,
                evidence_id,
                new_evidence,
                flow_5m["direction"],
            )

            evidence_triple = confirmation_evidence_triple(
                latest_trade,
                confirming_evidence,
            )

            state_data = update_market_state(
                token_id,
                candidate,
                verified,
                (
                    evidence_triple["evidence_id"]
                    if evidence_triple else None
                ),
                flow_5m["direction"],
                evidence_cursor=(
                    evidence_triple["evidence_cursor"]
                    if evidence_triple else None
                ),
                evidence_at=(
                    evidence_triple["evidence_at"]
                    if evidence_triple else None
                ),
            )

            analysis_interval = get_adaptive_interval(
                profile,
                state_data["state"],
            )

            schedule["next_due"] = (
                time.time() + analysis_interval
            )

            schedule["last_analysis_evidence"] = (
                evidence_id
            )

            save_flow_state(
                token_id=latest_trade.get("token_id"),
                condition_id=condition_id,
                market={
                    "question": question,
                    "outcome": outcome,
                    "end_date": end_date,
                    "start_date": latest_trade.get("start_date"),
                    "price": (
                        latest_trade.get("fill_price")
                        or latest_trade.get("price")
                    ),
                    "token_id": latest_trade.get("token_id"),
                    "market_id": latest_trade.get("market_id"),
                    "active": latest_trade.get("active"),
                    "closed": latest_trade.get("closed"),
                    "accepting_orders": latest_trade.get(
                        "accepting_orders"
                    ),
                    "volume24hr": latest_trade.get("volume24hr"),
                    # Canonical trade freshness for downstream Diamond/Risk.
                    "last_trade_at": (
                        latest_trade.get("block_timestamp")
                        or latest_trade.get("detected_at")
                    ),
                },
                flow_1m=flow_1m,
                flow_5m=flow_5m,
                flow_15m=flow_15m,
                interest_score=interest_score,
                data_confidence=data_confidence,
                resolution_state=resolution_state,
                remaining_seconds=remaining_seconds,
                analysis_profile=profile,
                analysis_interval_seconds=analysis_interval,
                state_data=state_data,
                candidate=candidate,
                candidate_reasons=candidate_reasons,
                verified=verified,
                verification_reasons=verification_reasons,
            )

            analyzed_count += 1

            # Keep the console selective. Far low-activity markets are still
            # maintained in flow_state, but do not flood the terminal.
            important_for_console = (
                state_data["state"]
                in {"VERIFYING", "VERIFIED"}
                or candidate
                or flow_5m["total_volume"] >= 500
                or profile
                in {"NEAR", "SOON", "URGENT"}
            )

            if important_for_console:
                print_market(
                    condition_id=condition_id,
                    question=question,
                    outcome=outcome,
                    end_date=end_date,
                    resolution_state=resolution_state,
                    remaining_seconds=remaining_seconds,
                    analysis_profile=profile,
                    analysis_interval_seconds=analysis_interval,
                    flow_1m=flow_1m,
                    flow_5m=flow_5m,
                    flow_15m=flow_15m,
                    interest_score=interest_score,
                    data_confidence=data_confidence,
                    state_data=state_data,
                    candidate=candidate,
                    candidate_reasons=candidate_reasons,
                    verified=verified,
                    verification_reasons=verification_reasons,
                )

        if analyzed_count > 0:
            persist_market_states()
            persist_flow_states()

        elapsed = time.time() - cycle_start

        print()
        print(
            f"Recent active markets: {len(markets)} | "
            f"Analyzed this tick: {analyzed_count} | "
            f"Dormant skipped: {dormant_skipped} | "
            f"Flow wakeups: {wakeups}"
        )

        sleep_time = max(
            1,
            GLOBAL_TICK_SECONDS - elapsed,
        )

        time.sleep(sleep_time)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()