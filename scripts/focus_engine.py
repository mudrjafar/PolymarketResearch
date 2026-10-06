"""Unit 5 - Focus Engine V3.

Pure deterministic state machine.
No file I/O, no network, no Telegram, no trading.

step(state, candidates, now) -> (new_state, events)
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone


STATE_VERSION = 3

REQUIRED_EVIDENCE = 3
MAX_FAILS = 2

MIN_CONFIRM_SPACING_SECONDS = 60
MAX_FRESH_SILENCE_SECONDS = 15 * 60
MAX_ABSENT_SECONDS = 60 * 60
COOLDOWN_SECONDS = 30 * 60


HARD_INVALIDATE_CODES = frozenset({
    "MARKET_EXPIRED",
    "MARKET_NOT_ACTIVE",
    "MARKET_CLOSED_UNSAFE",
    "MARKET_NOT_ACCEPTING",
    "IDENTITY_MISMATCH",
})


NEUTRAL_CODES = frozenset({
    "STALE_DIAMOND",
    "STALE_TRADE",
    "STALE_FLOW",
    "FLOW_MISSING",
})


# ============================================================
# STATE
# ============================================================

def new_state():
    return {
        "version": STATE_VERSION,
        "focus": None,
        "challenger": None,
        "cooldowns": {},
        "last_invalidated": None,
    }


# ============================================================
# HELPERS
# ============================================================

def _text(value):
    return "" if value is None else str(value).strip()


def _score(candidate):
    try:
        return float(candidate.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            return None

        return parsed

    except (TypeError, ValueError):
        return None


def _ensure_now(now):
    if now is None:
        return datetime.now(timezone.utc)

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    return now


def _cursor(candidate):
    """
    Strict blockchain ordering cursor.

    Preferred:
        evidence_cursor = [block_number, log_index]

    Fallback:
        block_number
        log_index
    """

    if not isinstance(candidate, dict):
        return None

    raw = candidate.get("evidence_cursor")

    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            return None

    try:
        return (
            int(candidate["block_number"]),
            int(candidate["log_index"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _evidence_at(candidate):
    if not isinstance(candidate, dict):
        return None

    return _parse_time(candidate.get("evidence_at"))


def _is_newer(new_cursor, old_cursor):
    if new_cursor is None:
        return False

    if old_cursor is None:
        return True

    try:
        old = tuple(int(x) for x in old_cursor)
    except (TypeError, ValueError):
        return False

    return tuple(new_cursor) > old


# ============================================================
# LABELS / EVENTS
# ============================================================

def label(focus):
    if focus is None:
        return "IDLE"

    status = focus.get("status")

    if status == "READY":
        return "READY"

    if status == "LOCKED":
        return "LOCKED"

    progress = int(focus.get("progress", 0))
    fail_count = int(focus.get("fail_count", 0))

    suffix = " (FAIL 1)" if fail_count else ""

    return f"WAIT {progress}/{REQUIRED_EVIDENCE}{suffix}"


def _snapshot(candidate):
    return {
        "token_id": _text(candidate.get("token_id")),
        "condition_id": _text(candidate.get("condition_id")),
        "outcome": _text(candidate.get("outcome")),
        "question": candidate.get("question"),
        "direction": _text(candidate.get("direction")).upper(),
        "price": candidate.get("price"),
        "score": _score(candidate),
    }


def _event(kind, focus, now, **extra):
    event = {
        "type": kind,
        "at": now.isoformat(),
        "token_id": focus.get("token_id"),
        "condition_id": focus.get("condition_id"),
        "question": focus.get("question"),
        "outcome": focus.get("outcome"),
        "price": focus.get("price"),
        "state": (
            "INVALIDATED"
            if kind == "INVALIDATED"
            else label(focus)
        ),
    }

    event.update(extra)
    return event


# ============================================================
# COOLDOWN
# ============================================================

def _cleanup_cooldowns(state, now):
    cooldowns = state.setdefault("cooldowns", {})
    expired = []

    for token_id, until_raw in cooldowns.items():
        until = _parse_time(until_raw)

        if until is None or now >= until:
            expired.append(token_id)

    for token_id in expired:
        cooldowns.pop(token_id, None)


# ============================================================
# CANDIDATE SELECTION
# ============================================================

def _eligible(candidate, state):
    if not isinstance(candidate, dict):
        return False

    token_id = _text(candidate.get("token_id"))

    if not token_id:
        return False

    if token_id in state.get("cooldowns", {}):
        return False

    if candidate.get("risk_ok") is not True:
        return False

    if not _text(candidate.get("evidence_id")):
        return False

    if _cursor(candidate) is None:
        return False

    if _evidence_at(candidate) is None:
        return False

    return True


def _best_candidate(candidates, state, exclude_token=None):
    eligible = []

    for candidate in candidates:
        token_id = _text(candidate.get("token_id"))

        if token_id == _text(exclude_token):
            continue

        if _eligible(candidate, state):
            eligible.append(candidate)

    if not eligible:
        return None

    # Highest score wins.
    # Tie -> token_id ascending.
    eligible.sort(
        key=lambda candidate: (
            -_score(candidate),
            _text(candidate.get("token_id")),
        )
    )

    return eligible[0]


def _build_current_by_token(candidates):
    """
    Keep exactly one current row per token.
    Newest blockchain cursor wins.
    """

    result = {}

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        token_id = _text(candidate.get("token_id"))

        if not token_id:
            continue

        previous = result.get(token_id)

        if previous is None:
            result[token_id] = candidate
            continue

        new_cursor = _cursor(candidate)
        old_cursor = _cursor(previous)

        if new_cursor is not None and (
            old_cursor is None
            or new_cursor > old_cursor
        ):
            result[token_id] = candidate

    return result


# ============================================================
# LOCK / INVALIDATE
# ============================================================

def _lock(state, candidate, now):
    cursor = _cursor(candidate)
    evidence_at = _evidence_at(candidate)

    focus = _snapshot(candidate)

    focus.update({
        "status": "LOCKED",

        "progress": 0,
        "fail_count": 0,

        # Evidence present when lock is created is baseline only.
        "last_evidence_id": _text(
            candidate.get("evidence_id")
        ),

        "last_evidence_cursor": list(cursor),

        # Baseline also starts spacing clock.
        "last_counted_evidence_at": (
            evidence_at.isoformat()
        ),

        "locked_at": now.isoformat(),
        "last_tick_at": now.isoformat(),

        # Counts only time during which pipeline data is usable.
        "fresh_silence_seconds": 0.0,

        # Completely missing row has separate protection.
        "absent_since": None,

        "price_at_lock": candidate.get("price"),
        "score_at_lock": _score(candidate),
    })

    state["focus"] = focus

    return focus


def _invalidate(
    state,
    focus,
    reasons,
    now,
    events,
):
    invalidated = deepcopy(focus)

    invalidated["status"] = "INVALIDATED"
    invalidated["invalidated_at"] = now.isoformat()
    invalidated["invalidation_reasons"] = list(reasons)

    state["last_invalidated"] = invalidated

    token_id = _text(focus.get("token_id"))

    state.setdefault("cooldowns", {})[token_id] = (
        now + timedelta(seconds=COOLDOWN_SECONDS)
    ).isoformat()

    events.append(
        _event(
            "INVALIDATED",
            invalidated,
            now,
            reasons=list(reasons),
        )
    )

    state["focus"] = None
    state["challenger"] = None


# ============================================================
# MAIN STATE MACHINE
# ============================================================

def step(state, candidates, now=None):
    """
    Advance Focus by exactly one tick.

    Rules:
    - no timer/restart duplicate confirmations
    - blockchain cursor must strictly increase
    - confirmations require >=60 sec event-time spacing
    - stale/missing pipeline data is neutral
    - hard safety codes invalidate immediately
    - two consecutive soft fails invalidate
    - challenger cannot steal active Focus
    """

    now = _ensure_now(now)

    events = []

    # --------------------------------------------------------
    # FAIL-CLOSED STATE VERSION
    # --------------------------------------------------------

    if not isinstance(state, dict):
        state = new_state()

    elif state.get("version") != STATE_VERSION:
        old_version = state.get("version")

        state = new_state()

        events.append({
            "type": "STATE_RESET_VERSION",
            "at": now.isoformat(),
            "old_version": old_version,
            "new_version": STATE_VERSION,
        })

        return state, events

    else:
        state = deepcopy(state)

    state.setdefault("focus", None)
    state.setdefault("challenger", None)
    state.setdefault("cooldowns", {})
    state.setdefault("last_invalidated", None)

    candidates = [
        candidate
        for candidate in (candidates or [])
        if isinstance(candidate, dict)
    ]

    _cleanup_cooldowns(state, now)

    by_token = _build_current_by_token(candidates)

    # ========================================================
    # NO ACTIVE FOCUS
    # ========================================================

    if not isinstance(state.get("focus"), dict):
        state["focus"] = None
        state["challenger"] = None

        best = _best_candidate(
            candidates,
            state,
        )

        if best is None:
            return state, events

        focus = _lock(
            state,
            best,
            now,
        )

        events.append(
            _event(
                "LOCKED",
                focus,
                now,
            )
        )

        return state, events

    # ========================================================
    # ACTIVE FOCUS
    # ========================================================

    focus = state["focus"]

    token_id = _text(
        focus.get("token_id")
    )

    # --------------------------------------------------------
    # Challenger is informational only.
    # It cannot steal Focus.
    # --------------------------------------------------------

    challenger = _best_candidate(
        candidates,
        state,
        exclude_token=token_id,
    )

    state["challenger"] = (
        _snapshot(challenger)
        if challenger is not None
        else None
    )

    current = by_token.get(token_id)

    # --------------------------------------------------------
    # Tick timing
    # --------------------------------------------------------

    last_tick = (
        _parse_time(
            focus.get("last_tick_at")
        )
        or now
    )

    delta_seconds = max(
        0.0,
        (now - last_tick).total_seconds(),
    )

    focus["last_tick_at"] = now.isoformat()

    # ========================================================
    # FOCUS ROW COMPLETELY MISSING
    # ========================================================

    if current is None:
        absent_since = _parse_time(
            focus.get("absent_since")
        )

        if absent_since is None:
            focus["absent_since"] = now.isoformat()
            return state, events

        absent_seconds = (
            now - absent_since
        ).total_seconds()

        if absent_seconds >= MAX_ABSENT_SECONDS:
            _invalidate(
                state,
                focus,
                ["FOCUS_ROW_MISSING"],
                now,
                events,
            )

        return state, events

    focus["absent_since"] = None

    codes = set(
        current.get("reason_codes")
        or []
    )

    # ========================================================
    # HARD INVALIDATION
    # ========================================================

    hard_codes = (
        codes
        & HARD_INVALIDATE_CODES
    )

    if hard_codes:
        _invalidate(
            state,
            focus,
            sorted(hard_codes),
            now,
            events,
        )

        # No challenger takeover in same tick.
        return state, events

    # ========================================================
    # NEUTRAL / STALE PIPELINE
    # ========================================================

    # Any neutral code makes soft conclusions unreliable.
    # Evidence is NOT consumed.
    # Silence clock is paused.
    if codes & NEUTRAL_CODES:
        return state, events

    # ========================================================
    # PIPELINE IS USABLE
    # ========================================================

    focus["fresh_silence_seconds"] = (
        float(
            focus.get(
                "fresh_silence_seconds",
                0.0,
            )
        )
        + delta_seconds
    )

    evidence_id = _text(
        current.get("evidence_id")
    )

    evidence_cursor = _cursor(current)

    evidence_at = _evidence_at(current)

    last_id = _text(
        focus.get("last_evidence_id")
    )

    last_cursor = focus.get(
        "last_evidence_cursor"
    )

    new_evidence = (
        bool(evidence_id)
        and evidence_id != last_id
        and _is_newer(
            evidence_cursor,
            last_cursor,
        )
    )

    # ========================================================
    # NO NEW BLOCKCHAIN EVIDENCE
    # ========================================================

    if not new_evidence:
        if (
            focus["fresh_silence_seconds"]
            >= MAX_FRESH_SILENCE_SECONDS
        ):
            _invalidate(
                state,
                focus,
                ["NO_NEW_EVIDENCE"],
                now,
                events,
            )

        return state, events

    # ========================================================
    # GENUINELY NEW BLOCKCHAIN EVIDENCE
    # ========================================================

    focus["last_evidence_id"] = evidence_id

    focus["last_evidence_cursor"] = list(
        evidence_cursor
    )

    focus["fresh_silence_seconds"] = 0.0

    focus["price"] = current.get("price")
    focus["score"] = _score(current)

    # ========================================================
    # PASS
    # ========================================================

    if current.get("risk_ok") is True:
        # PASS breaks soft-failure streak.
        focus["fail_count"] = 0

        # Evidence has to be separated in blockchain EVENT TIME.
        if evidence_at is None:
            return state, events

        last_counted = _parse_time(
            focus.get(
                "last_counted_evidence_at"
            )
        )

        if last_counted is not None:
            spacing = (
                evidence_at
                - last_counted
            ).total_seconds()

            if spacing < MIN_CONFIRM_SPACING_SECONDS:
                # Valid new trade, but too close together.
                # Consume evidence, do not advance readiness.
                return state, events

        previous_label = label(focus)

        focus["last_counted_evidence_at"] = (
            evidence_at.isoformat()
        )

        focus["progress"] = min(
            REQUIRED_EVIDENCE,
            int(
                focus.get(
                    "progress",
                    0,
                )
            )
            + 1,
        )

        if (
            focus["progress"]
            >= REQUIRED_EVIDENCE
        ):
            focus["status"] = "READY"

        else:
            focus["status"] = "WAIT"

        new_label = label(focus)

        if new_label != previous_label:
            events.append(
                _event(
                    (
                        "READY"
                        if focus["status"] == "READY"
                        else "WAIT"
                    ),
                    focus,
                    now,
                )
            )

        return state, events

    # ========================================================
    # SOFT FAIL
    # ========================================================

    previous_label = label(focus)

    focus["fail_count"] = (
        int(
            focus.get(
                "fail_count",
                0,
            )
        )
        + 1
    )

    if focus["fail_count"] >= MAX_FAILS:
        _invalidate(
            state,
            focus,
            (
                sorted(codes)
                if codes
                else ["RISK_BLOCK"]
            ),
            now,
            events,
        )

        return state, events

    # First soft fail removes one readiness step.
    focus["progress"] = max(
        0,
        int(
            focus.get(
                "progress",
                0,
            )
        )
        - 1,
    )

    focus["status"] = "WAIT"

    new_label = label(focus)

    if new_label != previous_label:
        events.append(
            _event(
                "WAIT",
                focus,
                now,
                reasons=sorted(codes),
            )
        )

    return state, events


# ============================================================
# SELF TEST
# ============================================================

def _candidate(
    token,
    block,
    log_index,
    evidence_time,
    *,
    score=90,
    risk_ok=True,
    codes=None,
):
    return {
        "token_id": token,
        "condition_id": f"condition-{token}",
        "outcome": "Yes",
        "question": f"Market {token}",
        "direction": "BUY",
        "price": 0.42,

        "risk_ok": risk_ok,
        "reason_codes": list(codes or []),

        "score": score,

        "evidence_id": (
            f"{token}:{block}:{log_index}"
        ),

        "evidence_cursor": [
            block,
            log_index,
        ],

        "evidence_at": (
            evidence_time.isoformat()
        ),
    }


def self_test():
    t0 = datetime(
        2026,
        10,
        4,
        12,
        0,
        0,
        tzinfo=timezone.utc,
    )

    # --------------------------------------------------------
    # 1. Lock strongest candidate
    # --------------------------------------------------------

    state, events = step(
        new_state(),
        [
            _candidate(
                "A",
                100,
                1,
                t0,
                score=90,
            )
        ],
        t0,
    )

    assert state["focus"]["token_id"] == "A"
    assert label(state["focus"]) == "LOCKED"
    assert events[-1]["type"] == "LOCKED"

    # --------------------------------------------------------
    # 2. Same evidence does nothing
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                100,
                1,
                t0,
            )
        ],
        t0 + timedelta(seconds=15),
    )

    assert label(state["focus"]) == "LOCKED"
    assert events == []

    # --------------------------------------------------------
    # 3. Older evidence does nothing
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                99,
                99,
                t0 - timedelta(seconds=10),
            )
        ],
        t0 + timedelta(seconds=30),
    )

    assert label(state["focus"]) == "LOCKED"
    assert events == []

    # --------------------------------------------------------
    # 4. New evidence after only 30 sec:
    # consumed but does not count.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                101,
                1,
                t0 + timedelta(seconds=30),
            )
        ],
        t0 + timedelta(seconds=30),
    )

    assert label(state["focus"]) == "LOCKED"
    assert state["focus"]["last_evidence_cursor"] == [101, 1]
    assert events == []

    # --------------------------------------------------------
    # 5. 60 sec event spacing -> WAIT 1/3
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                102,
                1,
                t0 + timedelta(seconds=60),
            )
        ],
        t0 + timedelta(seconds=60),
    )

    assert label(state["focus"]) == "WAIT 1/3"

    # --------------------------------------------------------
    # 6. WAIT 2/3
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                103,
                1,
                t0 + timedelta(seconds=120),
            )
        ],
        t0 + timedelta(seconds=120),
    )

    assert label(state["focus"]) == "WAIT 2/3"

    # --------------------------------------------------------
    # 7. READY
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                104,
                1,
                t0 + timedelta(seconds=180),
            )
        ],
        t0 + timedelta(seconds=180),
    )

    assert label(state["focus"]) == "READY"
    assert events[-1]["type"] == "READY"

    # --------------------------------------------------------
    # 8. Stronger B cannot steal Focus
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                104,
                1,
                t0 + timedelta(seconds=180),
                score=90,
            ),
            _candidate(
                "B",
                200,
                1,
                t0 + timedelta(seconds=180),
                score=99,
            ),
        ],
        t0 + timedelta(seconds=190),
    )

    assert state["focus"]["token_id"] == "A"
    assert state["challenger"]["token_id"] == "B"

    # --------------------------------------------------------
    # 9. Mixed neutral + divergence = NEUTRAL
    # Evidence must NOT be consumed.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                105,
                1,
                t0 + timedelta(seconds=240),
                risk_ok=False,
                codes=[
                    "STALE_FLOW",
                    "FLOW_DIRECTION_DIVERGENT",
                ],
            )
        ],
        t0 + timedelta(seconds=240),
    )

    assert label(state["focus"]) == "READY"
    assert state["focus"]["last_evidence_cursor"] == [104, 1]
    assert events == []

    # --------------------------------------------------------
    # 10. Same evidence becomes usable after recovery.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                105,
                1,
                t0 + timedelta(seconds=240),
            )
        ],
        t0 + timedelta(seconds=245),
    )

    assert label(state["focus"]) == "READY"
    assert state["focus"]["last_evidence_cursor"] == [105, 1]

    # --------------------------------------------------------
    # 11. First soft fail:
    # READY 3/3 -> WAIT 2/3 FAIL 1
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                106,
                1,
                t0 + timedelta(seconds=300),
                risk_ok=False,
                codes=[
                    "FLOW_DIRECTION_DIVERGENT"
                ],
            )
        ],
        t0 + timedelta(seconds=300),
    )

    assert label(state["focus"]) == "WAIT 2/3 (FAIL 1)"

    # --------------------------------------------------------
    # 12. PASS resets fail streak.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                107,
                1,
                t0 + timedelta(seconds=360),
            )
        ],
        t0 + timedelta(seconds=360),
    )

    assert label(state["focus"]) == "READY"
    assert state["focus"]["fail_count"] == 0

    # --------------------------------------------------------
    # 13. Two consecutive soft failures invalidate.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "A",
                108,
                1,
                t0 + timedelta(seconds=420),
                risk_ok=False,
                codes=[
                    "FLOW_DIRECTION_DIVERGENT"
                ],
            )
        ],
        t0 + timedelta(seconds=420),
    )

    assert state["focus"]["fail_count"] == 1

    state, events = step(
        state,
        [
            _candidate(
                "A",
                109,
                1,
                t0 + timedelta(seconds=480),
                risk_ok=False,
                codes=[
                    "FLOW_DIRECTION_DIVERGENT"
                ],
            ),
            _candidate(
                "B",
                201,
                1,
                t0 + timedelta(seconds=480),
                score=99,
            ),
        ],
        t0 + timedelta(seconds=480),
    )

    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert "A" in state["cooldowns"]

    # B must NOT take over during same invalidation tick.
    assert state["focus"] is None

    # --------------------------------------------------------
    # 14. Next tick B may lock.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "B",
                201,
                1,
                t0 + timedelta(seconds=480),
                score=99,
            )
        ],
        t0 + timedelta(seconds=495),
    )

    assert state["focus"]["token_id"] == "B"

    # --------------------------------------------------------
    # 15. Hard block immediately invalidates.
    # No newer blockchain evidence required.
    # --------------------------------------------------------

    state, events = step(
        state,
        [
            _candidate(
                "B",
                201,
                1,
                t0 + timedelta(seconds=480),
                score=99,
                risk_ok=False,
                codes=["MARKET_EXPIRED"],
            )
        ],
        t0 + timedelta(seconds=500),
    )

    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert (
        "MARKET_EXPIRED"
        in events[-1]["reasons"]
    )

    # --------------------------------------------------------
    # 16. Stale pipeline pauses timeout.
    # --------------------------------------------------------

    timeout_state, _ = step(
        new_state(),
        [
            _candidate(
                "C",
                300,
                1,
                t0,
            )
        ],
        t0,
    )

    timeout_state, events = step(
        timeout_state,
        [
            _candidate(
                "C",
                300,
                1,
                t0,
                risk_ok=False,
                codes=["STALE_FLOW"],
            )
        ],
        t0 + timedelta(minutes=30),
    )

    assert timeout_state["focus"] is not None
    assert events == []

    # --------------------------------------------------------
    # 17. Fresh pipeline + no new evidence:
    # fresh-silence clock eventually invalidates.
    # --------------------------------------------------------

    timeout_state, events = step(
        timeout_state,
        [
            _candidate(
                "C",
                300,
                1,
                t0,
            )
        ],
        t0 + timedelta(minutes=30),
    )

    assert timeout_state["focus"] is not None

    timeout_state, events = step(
        timeout_state,
        [
            _candidate(
                "C",
                300,
                1,
                t0,
            )
        ],
        t0 + timedelta(minutes=45),
    )

    assert timeout_state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert events[-1]["reasons"] == [
        "NO_NEW_EVIDENCE"
    ]

    # --------------------------------------------------------
    # 18. Old state version resets safely.
    # --------------------------------------------------------

    reset_state, events = step(
        {
            "version": 1,
            "focus": {
                "token_id": "OLD"
            },
        },
        [],
        t0,
    )

    assert reset_state["version"] == STATE_VERSION
    assert reset_state["focus"] is None
    assert events[-1]["type"] == "STATE_RESET_VERSION"

    print("FOCUS ENGINE V3 SELF-TEST OK")


if __name__ == "__main__":
    self_test()