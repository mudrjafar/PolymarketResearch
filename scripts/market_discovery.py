"""Small, one-shot Polymarket Gamma market discovery.

No RPC, wallets, trading authority, trade scoring, or resident processes.
A published market list is a bounded research sample, not an execution watchlist.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BASE / "data" / "market_candidates.json"
ENDPOINT = "https://gamma-api.polymarket.com/markets/keyset"
PAGE_SIZE = 100
MAX_CANDIDATES = 25
MAX_DAYS = 5
SHOW = 5
CONDITION_ID = re.compile(r"0x(?:[0-9a-fA-F]{62}|[0-9a-fA-F]{64})\Z")
DECIMAL_ID = re.compile(r"[1-9][0-9]*\Z")

# H1.1: only an explicitly recognized non-sport Gamma tag is sufficient.
# Missing, malformed or unfamiliar categories remain UNKNOWN, never non-sport.
NON_SPORT_TAGS = frozenset({
    "politics", "geopolitics", "world", "economics", "economy", "finance",
    "business", "crypto", "technology", "tech", "science", "climate",
    "weather", "entertainment", "artificial-intelligence", "ai",
})
SPORT_TAGS = frozenset({
    "sports", "sport", "soccer", "football", "american-football", "nfl",
    "nba", "wnba", "mlb", "nhl", "baseball", "basketball", "hockey",
    "college-football", "college-basketball", "ncaa", "tennis", "golf",
    "pga", "ufc", "mma", "boxing", "f1", "formula-1", "formula1",
    "cricket", "rugby", "olympics", "esports", "e-sports", "valorant",
    "counter-strike", "league-of-legends", "dota-2",
})


def sport_policy(raw):
    """SPORT / NON_SPORT / UNKNOWN from Gamma tags; never guess from title."""
    if raw.get("gameId") or raw.get("sportsMarketType") or raw.get("sport") or raw.get("isSport") is True:
        return "SPORT"
    events = raw.get("events", [])
    if events is None:
        events = []
    if not isinstance(events, list):
        return "UNKNOWN"
    group = [raw] + events
    slugs = set()
    for item in group:
        if not isinstance(item, dict):
            return "UNKNOWN"
        if item.get("gameId") or item.get("sportsMarketType") or item.get("sport") or item.get("isSport") is True:
            return "SPORT"
        tags = item.get("tags")
        if tags is None:
            continue
        if not isinstance(tags, list):
            return "UNKNOWN"
        for tag in tags:
            if not isinstance(tag, dict):
                return "UNKNOWN"
            for key in ("slug", "label"):
                value = tag.get(key)
                if isinstance(value, str) and value.strip():
                    slugs.add(value.strip().lower().replace(" ", "-"))
    if slugs & SPORT_TAGS:
        return "SPORT"
    if slugs & NON_SPORT_TAGS:
        return "NON_SPORT"
    return "UNKNOWN"



class DiscoveryError(RuntimeError):
    pass


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_end(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return value if isinstance(value, list) else None


def string_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def candidate(raw, now):
    if not isinstance(raw, dict):
        return None, "invalid_row"
    if raw.get("active") is not True or raw.get("closed") is not False or raw.get("acceptingOrders") is not True:
        return None, "inactive_or_not_trading"
    category = sport_policy(raw)
    if category == "SPORT":
        return None, "sports_excluded"
    if category != "NON_SPORT":
        return None, "unknown_category_excluded"
    end = parse_end(raw.get("endDate"))
    if end is None:
        return None, "invalid_end_date"
    seconds = (end - now).total_seconds()
    if seconds <= 0 or seconds > MAX_DAYS * 86400:
        return None, "outside_five_days"
    market_id = string_id(raw.get("id"))
    condition = raw.get("conditionId")
    question = raw.get("question")
    if not market_id or not isinstance(condition, str) or not CONDITION_ID.fullmatch(condition) or not isinstance(question, str) or not question.strip():
        return None, "invalid_market_identity"
    labels = parse_array(raw.get("outcomes"))
    assets = parse_array(raw.get("clobTokenIds"))
    if not labels or len(labels) != 2 or not all(isinstance(x, str) and x.strip() for x in labels):
        return None, "not_binary"
    if labels[0].strip().casefold() == labels[1].strip().casefold():
        return None, "not_binary"
    if not assets or len(assets) != 2 or not all(isinstance(x, str) and DECIMAL_ID.fullmatch(x) for x in assets) or assets[0] == assets[1]:
        return None, "missing_or_ambiguous_identity"

    # These are *Gamma-supplied asset IDs*, not attested legacy CTF token IDs.
    # V3 position identity must be validated by the downstream protocol gate.
    return {
        "market_id": market_id,
        "condition_id": condition.lower(),
        "question": question.strip(),
        "end_date": utc_iso(end),
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "protocol_identity_status": "UNVERIFIED",
        "execution_eligible": False,
        "outcomes": [
            {"name": labels[i].strip(), "asset_id": assets[i]}
            for i in range(2)
        ],
    }, None


def select_markets(markets, now):
    counts = Counter()
    by_market = {}
    by_condition = {}
    by_asset = {}
    for raw in markets:
        row, reason = candidate(raw, now)
        if row is None:
            counts[reason] += 1
            continue
        mid, cid = row["market_id"], row["condition_id"]
        previous = by_market.get(mid)
        if previous is not None:
            if previous != row:
                raise DiscoveryError("Conflicting duplicate market_id; refusing snapshot")
            counts["duplicate_identical"] += 1
            continue
        if cid in by_condition and by_condition[cid] != mid:
            raise DiscoveryError("Condition ID belongs to multiple markets; refusing snapshot")
        for outcome in row["outcomes"]:
            aid = outcome["asset_id"]
            if aid in by_asset and by_asset[aid] != (mid, outcome["name"]):
                raise DiscoveryError("Asset ID is ambiguously bound; refusing snapshot")
        by_market[mid] = row
        by_condition[cid] = mid
        for outcome in row["outcomes"]:
            by_asset[outcome["asset_id"]] = (mid, outcome["name"])
    accepted = sorted(by_market.values(), key=lambda m: (m["end_date"], m["market_id"]))
    return accepted[:MAX_CANDIDATES], dict(sorted(counts.items())), len(accepted)


def fetch_market_page(now, getter=None, sleeper=time.sleep):
    getter = getter or requests.get
    params = {
        "limit": PAGE_SIZE,
        "active": "true",
        "closed": "false",
        "include_tag": "true",  # include market and event category metadata
        "end_date_min": utc_iso(now),
        "end_date_max": utc_iso(now + timedelta(days=MAX_DAYS)),
    }
    for attempt in range(3):
        try:
            response = getter(ENDPOINT, params=params, timeout=15)
            status = response.status_code
            if status == 429 or 500 <= status <= 599:
                if attempt < 2:
                    sleeper(0.4 * (2 ** attempt))
                    continue
                raise DiscoveryError("Gamma temporary failure after bounded retries")
            if status != 200:
                raise DiscoveryError(f"Gamma HTTP {status}; snapshot unchanged")
            try:
                obj = response.json()
            except (ValueError, TypeError) as exc:
                raise DiscoveryError("Gamma returned invalid JSON") from exc
            if not isinstance(obj, dict) or not isinstance(obj.get("markets"), list):
                raise DiscoveryError("Invalid keyset response envelope")
            if len(obj["markets"]) > PAGE_SIZE:
                raise DiscoveryError("Gamma exceeded requested 100-market page")
            cursor = obj.get("next_cursor", obj.get("nextCursor"))
            if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
                raise DiscoveryError("Invalid keyset cursor")
            return obj["markets"], bool(cursor)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 2:
                raise DiscoveryError("Gamma connection failed after bounded retries") from exc
            sleeper(0.4 * (2 ** attempt))
        except requests.RequestException as exc:
            raise DiscoveryError("Gamma request failed; snapshot unchanged") from exc
    raise DiscoveryError("Gamma fetch failed")


def build_snapshot(now, markets, next_cursor_present):
    selected, exclusions, eligible = select_markets(markets, now)
    return {
        "schema_version": 1,
        "generated_at": utc_iso(now),
        "source": "POLYMARKET_GAMMA_KEYSET",
        "scope": "LIMITED_SAMPLE",
        "execution_authority": "NONE",
        "sports_policy": "SPORTS_EXCLUDED_UNKNOWN_EXCLUDED",
        "category_authority": "GAMMA_MARKET_OR_EVENT_TAGS",
        "filter": {
            "scheduled_end_after": utc_iso(now),
            "scheduled_end_at_or_before": utc_iso(now + timedelta(days=MAX_DAYS)),
            "max_days": MAX_DAYS,
            "required_active": True,
            "required_closed": False,
            "required_accepting_orders": True,
            "binary_only": True,
        },
        "limits": {
            "pages_requested": 1, "pages_received": 1,
            "page_size": PAGE_SIZE, "max_candidates": MAX_CANDIDATES,
            "next_cursor_present": next_cursor_present,
            "limit_reached": len(markets) >= PAGE_SIZE or eligible > MAX_CANDIDATES,
        },
        "counts": {
            "received": len(markets),
            "eligible_in_page": eligible,
            "saved": len(selected),
            "omitted_due_to_candidate_cap": max(0, eligible - len(selected)),
            "excluded": exclusions,
        },
        "markets": selected,
    }


def atomic_publish(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(snapshot, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(now=None, output=DEFAULT_OUTPUT, getter=None, sleeper=time.sleep, printer=print):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    markets, has_cursor = fetch_market_page(now, getter=getter, sleeper=sleeper)
    snapshot = build_snapshot(now, markets, has_cursor)
    atomic_publish(output, snapshot)
    counts = snapshot["counts"]
    printer(f"[MARKET COLLECTOR] UTC {snapshot['generated_at']} | received {counts['received']} | suitable {counts['eligible_in_page']} | saved {counts['saved']}")
    printer(f"[MARKET COLLECTOR] excluded {counts['excluded']} | next_cursor={has_cursor} | LIMITED SAMPLE, not complete market coverage")
    for m in snapshot["markets"][:SHOW]:
        remaining = int((parse_end(m["end_date"]) - now).total_seconds())
        printer(f"  {m['market_id']} | {m['question'][:75]} | planned end {m['end_date']} | {remaining // 3600}h remaining")
    printer(f"[MARKET COLLECTOR] saved {Path(output)} | finished")
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-shot, bounded Gamma market discovery (no RPC)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON output file")
    args = parser.parse_args(argv)
    try:
        run(output=args.output)
    except (DiscoveryError, OSError, ValueError) as exc:
        print(f"[MARKET COLLECTOR] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
