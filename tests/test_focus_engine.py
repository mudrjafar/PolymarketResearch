"""Regression tests for Unit 5 - Focus Engine V3."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts.focus_engine import (
    STATE_VERSION,
    MAX_ABSENT_SECONDS,
    MAX_FRESH_SILENCE_SECONDS,
    COOLDOWN_SECONDS,
    label,
    new_state,
    step,
)


T0 = datetime(
    2026, 10, 4, 12, 0, 0,
    tzinfo=timezone.utc,
)


def candidate(
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
        "evidence_id": f"{token}:{block}:{log_index}",
        "evidence_cursor": [block, log_index],
        "evidence_at": evidence_time.isoformat(),
    }


def lock_a():
    state, events = step(
        new_state(),
        [candidate("A", 100, 1, T0)],
        T0,
    )

    assert state["focus"]["token_id"] == "A"
    assert events[-1]["type"] == "LOCKED"

    return state


def make_ready():
    state = lock_a()

    for block, seconds in [
        (101, 60),
        (102, 120),
        (103, 180),
    ]:
        state, _ = step(
            state,
            [
                candidate(
                    "A",
                    block,
                    1,
                    T0 + timedelta(seconds=seconds),
                )
            ],
            T0 + timedelta(seconds=seconds),
        )

    assert label(state["focus"]) == "READY"
    return state


def test_lock_preserves_upstream_candidate_order_without_reranking():
    state, _ = step(
        new_state(),
        [
            candidate("B", 100, 1, T0, score=10),
            candidate("A", 100, 2, T0, score=99),
            candidate("C", 100, 3, T0, score=80),
        ],
        T0,
    )

    # Risk preserves authoritative Diamond order. Focus must not synthesize
    # or re-rank on a numeric score.
    assert state["focus"]["token_id"] == "B"
    assert "score" not in state["focus"]
    assert "score_at_lock" not in state["focus"]
    assert label(state["focus"]) == "LOCKED"


def test_duplicate_and_older_evidence_do_not_progress():
    state = lock_a()

    state, events = step(
        state,
        [candidate("A", 100, 1, T0)],
        T0 + timedelta(seconds=60),
    )

    assert label(state["focus"]) == "LOCKED"
    assert events == []

    state, events = step(
        state,
        [
            candidate(
                "A",
                99,
                99,
                T0 - timedelta(seconds=10),
            )
        ],
        T0 + timedelta(seconds=120),
    )

    assert label(state["focus"]) == "LOCKED"
    assert events == []


def test_confirmation_spacing_prevents_fast_ready():
    state = lock_a()

    # 20 sec: valid evidence, but too soon.
    state, _ = step(
        state,
        [
            candidate(
                "A",
                101,
                1,
                T0 + timedelta(seconds=20),
            )
        ],
        T0 + timedelta(seconds=20),
    )

    assert label(state["focus"]) == "LOCKED"

    # 40 sec: still too soon.
    state, _ = step(
        state,
        [
            candidate(
                "A",
                102,
                1,
                T0 + timedelta(seconds=40),
            )
        ],
        T0 + timedelta(seconds=40),
    )

    assert label(state["focus"]) == "LOCKED"

    # 60 sec from baseline: first actual confirmation.
    state, _ = step(
        state,
        [
            candidate(
                "A",
                103,
                1,
                T0 + timedelta(seconds=60),
            )
        ],
        T0 + timedelta(seconds=60),
    )

    assert label(state["focus"]) == "WAIT 1/3"


def test_three_spaced_confirmations_reach_ready():
    state = make_ready()

    assert state["focus"]["progress"] == 3
    assert state["focus"]["status"] == "READY"


def test_stronger_challenger_cannot_steal_focus():
    state = make_ready()

    state, events = step(
        state,
        [
            candidate(
                "A",
                103,
                1,
                T0 + timedelta(seconds=180),
                score=90,
            ),
            candidate(
                "B",
                200,
                1,
                T0 + timedelta(seconds=180),
                score=99,
            ),
        ],
        T0 + timedelta(seconds=190),
    )

    assert state["focus"]["token_id"] == "A"
    assert state["challenger"]["token_id"] == "B"
    assert events == []


def test_stale_flow_trade_is_neutral_and_does_not_consume_evidence():
    state = make_ready()
    previous_cursor = list(state["focus"]["last_evidence_cursor"])

    state, events = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=["STALE_FLOW_TRADE"],
            )
        ],
        T0 + timedelta(seconds=240),
    )

    assert state["focus"]["fail_count"] == 0
    assert label(state["focus"]) == "READY"
    assert state["focus"]["last_evidence_cursor"] == previous_cursor
    assert events == []


def test_stale_data_is_neutral_and_does_not_consume_evidence():
    state = make_ready()

    previous_cursor = list(
        state["focus"]["last_evidence_cursor"]
    )

    state, events = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=["STALE_FLOW"],
            )
        ],
        T0 + timedelta(seconds=240),
    )

    assert label(state["focus"]) == "READY"
    assert (
        state["focus"]["last_evidence_cursor"]
        == previous_cursor
    )
    assert events == []

    # Same blockchain evidence becomes usable once data recovers.
    state, _ = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
            )
        ],
        T0 + timedelta(seconds=245),
    )

    assert state["focus"]["last_evidence_cursor"] == [104, 1]


def test_mixed_stale_and_divergence_is_neutral():
    state = make_ready()

    state, events = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=[
                    "STALE_FLOW",
                    "FLOW_DIRECTION_DIVERGENT",
                ],
            )
        ],
        T0 + timedelta(seconds=240),
    )

    assert state["focus"]["fail_count"] == 0
    assert label(state["focus"]) == "READY"
    assert state["focus"]["last_evidence_cursor"] == [103, 1]
    assert events == []


def test_hard_block_invalidates_without_new_evidence():
    state = make_ready()

    state, events = step(
        state,
        [
            candidate(
                "A",
                103,
                1,
                T0 + timedelta(seconds=180),
                risk_ok=False,
                codes=["MARKET_EXPIRED"],
            )
        ],
        T0 + timedelta(seconds=200),
    )

    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert events[-1]["reasons"] == ["MARKET_EXPIRED"]


def test_two_consecutive_soft_fails_invalidate():
    state = make_ready()

    state, _ = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=["FLOW_DIRECTION_DIVERGENT"],
            )
        ],
        T0 + timedelta(seconds=240),
    )

    assert state["focus"]["fail_count"] == 1
    assert label(state["focus"]) == "WAIT 2/3 (FAIL 1)"

    state, events = step(
        state,
        [
            candidate(
                "A",
                105,
                1,
                T0 + timedelta(seconds=300),
                risk_ok=False,
                codes=["FLOW_DIRECTION_DIVERGENT"],
            )
        ],
        T0 + timedelta(seconds=300),
    )

    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"


def test_pass_between_soft_fails_resets_fail_streak():
    state = make_ready()

    state, _ = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=["FLOW_DIRECTION_DIVERGENT"],
            )
        ],
        T0 + timedelta(seconds=240),
    )

    assert state["focus"]["fail_count"] == 1

    state, _ = step(
        state,
        [
            candidate(
                "A",
                105,
                1,
                T0 + timedelta(seconds=300),
            )
        ],
        T0 + timedelta(seconds=300),
    )

    assert state["focus"]["fail_count"] == 0
    assert label(state["focus"]) == "READY"


def test_stale_pipeline_pauses_silence_timeout():
    state = lock_a()

    state, events = step(
        state,
        [
            candidate(
                "A",
                100,
                1,
                T0,
                risk_ok=False,
                codes=["STALE_FLOW"],
            )
        ],
        T0 + timedelta(minutes=30),
    )

    assert state["focus"] is not None
    assert (
        state["focus"]["fresh_silence_seconds"]
        == 0.0
    )
    assert events == []


def test_fresh_pipeline_without_new_evidence_times_out():
    state = lock_a()

    # Fresh row, same evidence.
    state, _ = step(
        state,
        [candidate("A", 100, 1, T0)],
        T0 + timedelta(
            seconds=MAX_FRESH_SILENCE_SECONDS
        ),
    )

    assert state["focus"] is None
    assert (
        state["last_invalidated"]["invalidation_reasons"]
        == ["NO_NEW_EVIDENCE"]
    )


def test_missing_focus_row_is_neutral_then_eventually_invalidates():
    state = lock_a()

    # First disappearance starts absent clock.
    state, events = step(
        state,
        [],
        T0 + timedelta(minutes=5),
    )

    assert state["focus"] is not None
    assert events == []

    # Still below absolute missing limit.
    state, events = step(
        state,
        [],
        T0
        + timedelta(minutes=5)
        + timedelta(
            seconds=MAX_ABSENT_SECONDS - 1
        ),
    )

    assert state["focus"] is not None
    assert events == []

    # Limit reached.
    state, events = step(
        state,
        [],
        T0
        + timedelta(minutes=5)
        + timedelta(
            seconds=MAX_ABSENT_SECONDS
        ),
    )

    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert events[-1]["reasons"] == [
        "FOCUS_ROW_MISSING"
    ]


def test_challenger_only_takes_over_on_next_tick():
    state = make_ready()

    state, events = step(
        state,
        [
            candidate(
                "A",
                104,
                1,
                T0 + timedelta(seconds=240),
                risk_ok=False,
                codes=["MARKET_EXPIRED"],
            ),
            candidate(
                "B",
                200,
                1,
                T0 + timedelta(seconds=240),
                score=99,
            ),
        ],
        T0 + timedelta(seconds=240),
    )

    # A invalidated. B does not lock in same tick.
    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"

    state, events = step(
        state,
        [
            candidate(
                "B",
                200,
                1,
                T0 + timedelta(seconds=240),
                score=99,
            )
        ],
        T0 + timedelta(seconds=255),
    )

    assert state["focus"]["token_id"] == "B"
    assert events[-1]["type"] == "LOCKED"


def test_invalidated_market_cannot_relock_during_cooldown():
    state = make_ready()

    state, _ = step(
        state,
        [
            candidate(
                "A",
                103,
                1,
                T0 + timedelta(seconds=180),
                risk_ok=False,
                codes=["MARKET_EXPIRED"],
            )
        ],
        T0 + timedelta(seconds=200),
    )

    assert state["focus"] is None
    assert "A" in state["cooldowns"]

    state, _ = step(
        state,
        [
            candidate(
                "A",
                200,
                1,
                T0 + timedelta(seconds=300),
            )
        ],
        T0 + timedelta(seconds=300),
    )

    assert state["focus"] is None

    # After cooldown expires, A may lock again.
    state, events = step(
        state,
        [
            candidate(
                "A",
                300,
                1,
                T0
                + timedelta(seconds=200)
                + timedelta(
                    seconds=COOLDOWN_SECONDS + 1
                ),
            )
        ],
        T0
        + timedelta(seconds=200)
        + timedelta(
            seconds=COOLDOWN_SECONDS + 1
        ),
    )

    assert state["focus"]["token_id"] == "A"
    assert events[-1]["type"] == "LOCKED"


def test_old_state_version_resets_fail_closed():
    state, events = step(
        {
            "version": 1,
            "focus": {
                "token_id": "OLD",
                "status": "READY",
            },
        },
        [],
        T0,
    )

    assert state["version"] == STATE_VERSION
    assert state["focus"] is None

    assert events == [{
        "type": "STATE_RESET_VERSION",
        "at": T0.isoformat(),
        "old_version": 1,
        "new_version": STATE_VERSION,
    }]